"""候选人物图的远程生成：走 OpenAI 兼容的图片接口（用户自备的国内中转站）。

为什么需要它：本账号在官方 API 有 `gpt-5.6-luna` 的计划内额度，但**没有**
`gpt-image-*` 的余额（`credit_balance_exhausted`），而本地 8GB 卡上的 Krea2
turbo 出图既慢（单图 74.6 秒）又达不到用户的画质要求。所以出图交给中转站。

凭据只从环境读取，绝不写入前端、数据库或仓库。文本分析与出图是两套独立配置：
`H3_BATCH_IMAGE_*` 与 `H3_BATCH_TEXT_*` 互不影响。

尺寸约束（按中转站文档）：最大边 ≤3840、宽高均为 16 的倍数、长宽比 ≤3:1、
总像素 655,360~8,294,400。因此用精确比例而不是常见的 1536x1024（那是 3:2）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from PIL import Image, ImageFilter

from . import netproxy
from .settings import env_value

OFFICIAL_URL = "https://api.openai.com/v1"
REQUEST_TIMEOUT = 900.0

# 真 4:3 = 1536x1152、真 9:16 = 1152x2048、真 3:4 = 1152x1536
# （都是 16 的倍数、长宽比 ≤3:1、总像素落在中转站允许区间，落在 2K 计费档）。
# 3:4 是给**抖音封面**用的（1086×1448）—— 用户 2026-09-19：「封面也用api生成 不需要本地」。
IMAGE_SIZES = {"4:3": "1536x1152", "9:16": "1152x2048", "3:4": "1152x1536"}
ProgressCallback = Callable[[float, str], Awaitable[None]]

# 人物图仍按 70 分展示自检结果，但默认只生成一次。图片接口按次计费，低分后自动重做
# 会让「点击一次」产生 2~3 笔费用；需要重做时由用户在图片工作台明确点击。
PERSON_MIN_SIMILARITY = 70
PERSON_MAX_ATTEMPTS = 1


def base_url() -> str:
    return (
        env_value("H3_BATCH_IMAGE_BASE_URL")
        or env_value("OPENAI_BASE_URL")
        or OFFICIAL_URL
    ).rstrip("/")


def api_key() -> str:
    return env_value("H3_BATCH_IMAGE_API_KEY") or env_value("OPENAI_API_KEY")


def model() -> str:
    return env_value("H3_BATCH_IMAGE_MODEL") or "gpt-image-2.5-sunburst"


def configured() -> bool:
    """只有用户显式配了中转站才认为可用，避免误把官方 key 打到没有余额的官方图片接口。"""
    return bool(env_value("H3_BATCH_IMAGE_BASE_URL") and api_key())


def describe() -> dict[str, str]:
    return {
        "baseUrl": base_url(),
        "model": model(),
        "configured": "1" if configured() else "0",
        "mode": env_value("H3_BATCH_IMAGE_MODE", "multi"),
        # 中转站直连不通时必须走代理，否则出图会静默退回"源视频取帧"（2026-09-22 事故）。
        "proxy": netproxy.describe(),
    }


def request_plans() -> list[tuple[str, str]]:
    """返回本次唯一的请求格式；计费图片请求不拿来做兼容性探测。

    当前中转站文档采用单数字段 ``image``，因此默认固定为 multi/image。其它部署可用
    ``H3_BATCH_IMAGE_FIELD=image[]`` 或 ``H3_BATCH_IMAGE_MODE=composite`` 显式切换。
    兼容旧配置：mode=auto 也只选默认格式，不再依次真实生成三遍。
    """
    explicit = env_value("H3_BATCH_IMAGE_FIELD")
    mode = (env_value("H3_BATCH_IMAGE_MODE") or "multi").strip().lower()
    if mode in {"auto", "multi"}:
        return [("multi", explicit or "image")]
    if mode == "composite":
        return [("composite", explicit or "image")]
    return []


def _api_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error") or body
        message = error.get("message") if isinstance(error, dict) else None
        return f"图片接口错误 {response.status_code}：{message or json.dumps(error, ensure_ascii=False)[:300]}"
    except Exception:
        return f"图片接口错误 {response.status_code}：{response.text[:300]}"


def compose_reference(scene_image: Path, identity_image: Path, target: Path) -> Path:
    """把两张参考图左右拼成一张，供只接受单图的中转站使用。

    不叠任何文字标注（避免被画进成图），靠提示词说明左右半幅各自的角色。
    """
    with Image.open(scene_image) as opened:
        scene = opened.convert("RGB")
    with Image.open(identity_image) as opened:
        identity = opened.convert("RGB")
    height = max(scene.height, identity.height)
    gap = max(8, height // 80)

    def fit(image: Image.Image) -> Image.Image:
        scale = height / image.height
        return image.resize((max(1, round(image.width * scale)), height), Image.Resampling.LANCZOS)

    left, right = fit(scene), fit(identity)
    sheet = Image.new("RGB", (left.width + gap + right.width, height), "#101018")
    sheet.paste(left, (0, 0))
    sheet.paste(right, (left.width + gap, 0))
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target, format="PNG")
    return target


def _build_request(
    plan: str,
    field: str,
    *,
    scene_image: Path,
    identity_image: Path | None = None,
    prompt: str,
    ratio: str,
    work_dir: Path,
) -> tuple[dict[str, str], list[tuple[str, tuple[str, bytes, str]]], str]:
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    text = prompt
    if plan == "composite":
        if identity_image is None:
            raise RuntimeError("composite 模式必须同时给场景图与身份图")
        combined = compose_reference(scene_image, identity_image, work_dir / "reference-combined.png")
        text = (
            "输入图是一张左右拼合的参考图：左半部分是「图一」（造型、服装、场景、灯光参考），"
            "右半部分是「图二」（唯一的人物身份与面部参考）。请把它当作两张独立参考图理解，"
            "不要输出这种左右拼接的画面。\n\n" + prompt
        )
        paths = [(combined, f"image[]")]
    else:
        # **单参考**（identity_image=None）是给**封面**用的：封面以该条 `人物图.png` 为准，
        # 只发一张。early 版本封面误传了「人物图 + 本人固定参考」两张**不同服装**的人像，
        # 模型直接把两个人一起画进封面（用户 2026-09-19 截图确认）—— 封面必须只发一张。
        paths = [(Path(scene_image), field)]
        if identity_image is not None:
            paths.append((Path(identity_image), field))
    for path, name in paths:
        if not path.is_file():
            raise RuntimeError(f"候选人物图缺少输入图片：{path}")
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        files.append((name, (path.name, path.read_bytes(), mime)))

    data = {
        "model": model(),
        "prompt": text,
        "size": IMAGE_SIZES[ratio],
        "n": "1",
        "quality": env_value("H3_BATCH_IMAGE_QUALITY", "high"),
    }
    fidelity = env_value("H3_BATCH_IMAGE_FIDELITY", "high")
    if fidelity:
        data["input_fidelity"] = fidelity
    return data, files, text


def _blur_region(image: Image.Image, box: dict[str, float]) -> Image.Image:
    width, height = image.size
    x0 = max(0, min(width - 1, int(box["x0"] * width)))
    y0 = max(0, min(height - 1, int(box["y0"] * height)))
    x1 = max(x0 + 1, min(width, int(box["x1"] * width)))
    y1 = max(y0 + 1, min(height, int(box["y1"] * height)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return image
    region = image.crop((x0, y0, x1, y1)).filter(
        ImageFilter.GaussianBlur(radius=max(14.0, (x1 - x0) / 5.0))
    )
    image.paste(region, (x0, y0))
    return image


def _locate_face_local(image_path: Path) -> dict[str, float] | None:
    """用本机 OpenCV Haar 定位最大人脸；没有依赖或没识别到就返回 None，不调用计费 API。"""
    try:
        import cv2  # type: ignore[import-not-found]

        frame = cv2.imread(str(image_path))
        if frame is None:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=4, minSize=(40, 40))
        if len(faces) == 0:
            return None
        x, y, width, height = max(faces, key=lambda row: int(row[2]) * int(row[3]))
        image_height, image_width = gray.shape[:2]
        # 稍微扩框，确保眉眼、双颊和下巴都被模糊掉。
        pad_x, pad_y = int(width * 0.12), int(height * 0.12)
        return {
            "x0": float(max(0, x - pad_x) / image_width),
            "y0": float(max(0, y - pad_y) / image_height),
            "x1": float(min(image_width, x + width + pad_x) / image_width),
            "y1": float(min(image_height, y + height + pad_y) / image_height),
        }
    except (ImportError, AttributeError, OSError, RuntimeError):
        return None


async def defocus_reference_face(scene_image: Path, work_dir: Path) -> tuple[Path, bool]:
    """把参考帧（图一）里的人脸糊掉，返回 (文件路径, 是否成功糊掉)。

    图一里是**源视频那个人**的脸。直接送进去，模型会把那个人的五官混进成图，
    而用户要求「五官必须和原型图一致」。把这张脸糊掉之后，图一仍然提供发型轮廓、
    服装、场景和灯光，但不再提供任何可用于"换脸"的面部信息。

    定位失败时不阻断流程：返回原图并标记未处理，由上层写进日志。
    """
    scene_image = Path(scene_image)
    target = Path(work_dir) / f"{scene_image.stem}-noface.png"
    if target.is_file():
        return target, True

    with Image.open(scene_image) as opened:
        image = opened.convert("RGB")
    # 过去这里每条作品都调用一次 DS 视觉模型，只为取一个矩形框。改成本机检测；失败时沿用
    # 原图并在上层提示，不为一个可选优化额外产生 API 费用。
    box = await asyncio.to_thread(_locate_face_local, scene_image)
    if not box:
        return scene_image, False
    target.parent.mkdir(parents=True, exist_ok=True)
    _blur_region(image, box).save(target, format="PNG")
    return target, True


async def swap_face_with_prototype(
    *,
    generated: Path,
    prototype: Path,
    output_path: Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """用本地 ReActor 把原型图的脸换到生成图上，锁定身份。

    为什么必须做这一步：中转站用的是图像编辑模型，它本质上是**重新合成**一张脸，
    `input_fidelity` 只能做到"尽量像"，做不到"就是同一个人"（用户实测判定五官不对）。
    换脸是确定性的身份迁移，才是「五官必须和原型图一致」这条硬要求的正确实现。

    造型、服装、场景、构图仍由中转站出的那张图决定，换脸只替换面部区域，
    并接一个 GFPGAN 修复让边缘自然。
    """
    from .batch_portrait import run_api_prompt, stage_input
    from .pipeline import pipeline_lock, resources

    target_name = await asyncio.to_thread(stage_input, Path(generated), "swap_target")
    source_name = await asyncio.to_thread(stage_input, Path(prototype), "swap_source")
    prompt: dict[str, Any] = {
        "1": {"class_type": "LoadImage", "inputs": {"image": target_name}},
        "2": {"class_type": "LoadImage", "inputs": {"image": source_name}},
        "3": {
            "class_type": "ReActorFaceSwap",
            "inputs": {
                "enabled": True,
                "input_image": ["1", 0],
                "source_image": ["2", 0],
                "swap_model": "reswapper_128.onnx",
                "facedetection": "retinaface_resnet50",
                "face_restore_model": "GFPGANv1.4.pth",
                "face_restore_visibility": 1.0,
                "codeformer_weight": 0.5,
                "detect_gender_input": "no",
                "detect_gender_source": "no",
                "input_faces_index": "0",
                "source_faces_index": "0",
                "console_log_level": 1,
            },
            "_meta": {"title": "ReActor 换脸锁定身份"},
        },
        "4": {
            "class_type": "SaveImage",
            "inputs": {"images": ["3", 0], "filename_prefix": "batch_faceswap"},
        },
    }
    async with pipeline_lock:
        await resources.ensure_comfy()
        produced = await run_api_prompt(prompt, on_progress)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copy2, produced, output_path)
    return output_path


_ROUTE_KEY = "relay"
_route_proxy: str | None = None
_route_note: str = ""


def _route(key: str = _ROUTE_KEY) -> str | None:
    """当前这次出图该用的代理（None = 直连）。"""

    return _route_proxy


async def _prepare_route() -> str:
    """出图之前判定一次出网路线：**直连优先，直连不通才走代理**。

    路线说明记进 `_route_note`，调用方写进日志 —— 用户能看到这次到底走没走代理。
    """
    global _route_proxy, _route_note
    proxy, note = await netproxy.probe_route_async(f"{base_url()}/models")
    _route_proxy = proxy or None
    _route_note = note
    return note


def route_note() -> str:
    """本次出图**实际**用的出网方式（给日志/报错用）。"""
    return _route_note or netproxy.describe()


async def _post(
    data: dict[str, str], files: list[tuple[str, tuple[str, bytes, str]]]
) -> httpx.Response:
    # 出网路线由 `generate_candidate_image` 事先判定好（`_prepare_route`）。
    # 2026-09-22 事故：直连被解析到无效 IP、全部超时，出图失败后 `batch_worker`
    # 按设计退回"源视频取帧"，用户看到的就是「人物图 = 抽帧图」。
    url = f"{base_url()}/images/edits"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, proxy=_route()) as client:
        return await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key()}"},
            data=data,
            files=files,
        )


def _store(response: httpx.Response, output_path: Path) -> Path:
    payload = response.json()
    entry = (payload.get("data") or [{}])[0]
    encoded = entry.get("b64_json")
    if encoded:
        output_path.write_bytes(base64.b64decode(encoded))
    elif entry.get("url"):
        # 图片地址由中转站给出（可能是境外对象存储/COS），沿用**这次出图用的那条路线**。
        image_url = str(entry["url"])
        with httpx.Client(timeout=REQUEST_TIMEOUT, proxy=_route()) as client:
            download = client.get(image_url)
        if download.is_error:
            raise RuntimeError(f"图片已生成但下载失败：HTTP {download.status_code}")
        output_path.write_bytes(download.content)
    else:
        raise RuntimeError("图片接口没有返回图像数据")
    return output_path


async def generate_candidate_image(
    *,
    scene_image: Path,
    identity_image: Path | None = None,
    prompt: str,
    ratio: str,
    output_path: Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """用「图像-1 造型场景 + 图像-2 唯一身份」做图片编辑，产出候选人物图。

    `identity_image=None` 时**只发一张参考图** —— 封面就是这么用的（见 `_build_request`
    里那段说明：两张不同服装的人像会让模型画出两个人）。
    """
    if ratio not in IMAGE_SIZES:
        raise RuntimeError(f"候选人物图不支持的比例：{ratio}")
    if not api_key():
        raise RuntimeError("尚未配置中转站的图片 API key（H3_BATCH_IMAGE_API_KEY）")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    work_dir = output_path.parent

    # **先判定出网路线**（直连优先、直连不通才走代理）：中转站直连被墙时，
    # 没有这一步就会全线连接超时 → `batch_worker` 退回"源视频取帧"，用户看到的
    # 就是「人物图 = 抽帧图」（2026-09-22）。判定只花一次廉价只读请求。
    await _prepare_route()

    started = time.monotonic()
    state = {"stop": False}

    async def ticker() -> None:
        """中转站没有节点进度，按耗时如实显示，不编造百分比。"""
        while not state["stop"]:
            await asyncio.sleep(5)
            if state["stop"] or on_progress is None:
                continue
            minutes, seconds = divmod(int(time.monotonic() - started), 60)
            await on_progress(0.0, f"正在通过中转站生成候选人物图 · 已用 {minutes:02d}:{seconds:02d}")

    ticker_task = asyncio.create_task(ticker()) if on_progress else None
    failures: list[str] = []
    try:
        plans = request_plans()
        if not plans:
            raise RuntimeError(
                "H3_BATCH_IMAGE_MODE 配置不正确；可选 auto / multi（多图编辑）/ composite（拼合成单图）"
            )
        for plan, field in plans:
            # ⚠️ **组请求也要放进 try 里**：`_build_request` 在 `composite` 模式下要求两张参考图，
            # 而**封面只发一张**（`identity_image=None`，见 `_generate_cover` 的注释）—— 这个
            # RuntimeError 以前会直接抛穿整个请求，`multi` 那套（单图编辑）根本没机会试，
            # 用户看到的就是「composite 模式必须同时给场景图与身份图」、两张封面全不出
            # （2026-09-22 实测：人物图 132 秒出好了，两张封面各 42 秒报这个错）。
            # 现在把它当成"这一套方案不可用"，继续试下一套 —— 单参考请求照样能出封面。
            data: dict[str, str] = {}
            files: list[tuple[str, tuple[str, bytes, str]]] = []
            try:
                data, files, _ = _build_request(
                    plan,
                    field,
                    scene_image=Path(scene_image),
                    # 注意**不能**写 Path(identity_image)：封面走单参考时它是 None，Path(None) 会抛
                    # TypeError（"argument should be a str or an os.PathLike ... not 'NoneType'"）。
                    identity_image=Path(identity_image) if identity_image is not None else None,
                    prompt=prompt,
                    ratio=ratio,
                    work_dir=work_dir,
                )
                response = await _post(data, files)
                if response.is_error:
                    failures.append(f"[{plan}/{field}] {_api_error(response)}")
                    continue
                _store(response, output_path)
            except httpx.HTTPError as error:
                # 请求可能已被中转站接收并计费，只是响应返回途中超时/断线。
                # 不自动换线路再发一遍，避免同一张图产生第二笔费用。
                failures.append(f"[{plan}/{field}] 连接失败：{type(error).__name__}: {str(error)[:160]}")
                netproxy.note_failure()
                break
            except RuntimeError as error:
                # 这套方案的**输入组装**不适用（最典型：composite 需要两张图、而封面只有一张）。
                # 用 `continue` 而不是 re-raise —— 否则会一直炸到用户面前。
                failures.append(f"[{plan}/{field}] 不适用：{error}")
                break
            if on_progress:
                await on_progress(1.0, "候选人物图已生成")
            return output_path
    finally:
        state["stop"] = True
        if ticker_task is not None:
            ticker_task.cancel()
            try:
                await ticker_task
            except (asyncio.CancelledError, Exception):
                pass

    detail = "；".join(failures) or "未知错误"
    hint = ""
    if failures:
        hint = "。为避免重复扣费，本次没有自动改格式或换线路重试；请确认中转站后台后再手动重做"
    raise RuntimeError(f"中转站出图失败：{detail}{hint}")


async def generate_person_image(
    *,
    scene_image: Path,
    identity_image: Path | None,
    prompt: str,
    ratio: str,
    output_path: Path,
    on_progress: ProgressCallback | None = None,
    min_similarity: int = PERSON_MIN_SIMILARITY,
    max_attempts: int = PERSON_MAX_ATTEMPTS,
    compliance: dict[str, Any] | None = None,
) -> tuple[Path, int]:
    """出人物图并做身份/着装自检；默认只生成一次，避免自动重复扣费。

    批量备料与 `/images` 页面的生成按钮共用这一份实现。低于门槛时保留本次结果并展示
    分数，由用户决定是否明确重做；调用方仍可显式传 max_attempts>1 做离线标定。

    身份与着装现在由 `batch_ai.judge_person_image` 在同一次 DS 调用里完成。默认只生成一张；
    命中暴露服装或身份低分时如实提示并保留本次结果，不在后台自动再产生图片费用。
    调用方显式传 max_attempts>1 时仍会在多张里优先保留合规且分数最高的一张。
    ⚠️ 口径 = 用户 2026-09-20「**只要衣服不暴露就可以了；动作都不用处理，流程也不要动**」：
    这里**只判衣服**，不看动作、不看构图，也不改任何流程开关。

    可选 `compliance` 是调用方传进来的 dict，函数会写入 `compliant` / `issues`，供页面显示。
    返回 `(output_path, 相似度)`；判据本身失败时返回 -1。
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output_path.parent
    best_bytes: bytes | None = None
    best_score = -1
    best_compliant: bool | None = None
    best_issues: list[str] = []
    last_compliant: bool | None = None
    last_issues: list[str] = []
    for attempt in range(1, max(1, max_attempts) + 1):
        candidate = work_dir / f"{output_path.stem}_attempt{attempt}{output_path.suffix}"
        await generate_candidate_image(
            scene_image=Path(scene_image),
            identity_image=Path(identity_image) if identity_image is not None else None,
            prompt=prompt,
            ratio=ratio,
            output_path=candidate,
            on_progress=on_progress,
        )
        try:
            from . import batch_ai

            result = await batch_ai.judge_person_image(Path(identity_image), candidate)
            score = max(0, min(100, int(result.get("similarity") or 0)))
        except Exception:  # noqa: BLE001 - 自检失败不该把出图判失败
            candidate.replace(output_path)
            if compliance is not None:
                compliance.update({"compliant": None, "issues": []})
            return output_path, -1
        compliant = bool(result.get("compliant", True))
        issues = [str(item) for item in (result.get("issues") or [])]
        last_compliant, last_issues = compliant, issues
        # 选图口径：合规优先，其次相似度。`best_compliant is None` 表示还没选过任何一张。
        better = (
            best_bytes is None
            or (compliant and not best_compliant)
            or (compliant == best_compliant and score > best_score)
        )
        if better:
            best_bytes = candidate.read_bytes()
            best_score = score
            best_compliant = compliant
            best_issues = issues
        candidate.unlink(missing_ok=True)
        if score >= min_similarity and compliant:
            break
    output_path.write_bytes(best_bytes or b"")
    if compliance is not None:
        # 出图循环跑满仍然不合规时，页面要能看到原因（保留最后一张的判据结果更贴近现状）
        compliance.update(
            {
                "compliant": best_compliant,
                "issues": best_issues,
                "last_compliant": last_compliant,
                "last_issues": last_issues,
            }
        )
    return output_path, max(0, best_score)


async def judge_compliance_safe(candidate: Path) -> tuple[bool, list[str]]:
    """着装合规自检：**判据失败按合规放行**（自检只是质量门槛，不能卡住出图链路）。

    单独抽成模块级函数是为了单测能直接替换掉它（不打真网络）。
    """
    from . import batch_ai

    try:
        result = await batch_ai.judge_compliance(candidate)
    except Exception:  # noqa: BLE001
        return True, []
    issues = [str(item) for item in (result.get("issues") or [])]
    return bool(result.get("compliant", True)), issues


async def generate_single_edit(
    *,
    source_image: Path,
    prompt: str,
    size: str,
    output_path: Path,
) -> Path:
    """Edit one reference image through the configured relay (used for publish covers)."""
    if not configured():
        raise RuntimeError("尚未配置中转站图片接口")
    source_image = Path(source_image)
    if not source_image.is_file():
        raise RuntimeError(f"缺少参考图片：{source_image}")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mime = mimetypes.guess_type(source_image.name)[0] or "image/png"
    data = {
        "model": model(),
        "prompt": prompt,
        "size": size,
        "n": "1",
        "quality": env_value("H3_BATCH_IMAGE_QUALITY", "high"),
        "input_fidelity": env_value("H3_BATCH_IMAGE_FIDELITY", "high"),
    }
    image_bytes = source_image.read_bytes()
    field = env_value("H3_BATCH_IMAGE_FIELD") or "image"
    files = [(field, (source_image.name, image_bytes, mime))]
    try:
        response = await _post(data, files)
    except httpx.HTTPError as exc:
        netproxy.note_failure()
        raise RuntimeError(
            f"封面生成请求未确认成功：{type(exc).__name__}: {str(exc)[:160]}。"
            "为避免重复扣费，本次没有自动重试"
        ) from exc
    if response.is_error:
        raise RuntimeError(f"封面生成失败：{_api_error(response)}（未自动改格式重试）")
    return _store(response, output_path)
