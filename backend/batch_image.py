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

from .settings import env_value

OFFICIAL_URL = "https://api.openai.com/v1"
REQUEST_TIMEOUT = 900.0

# 真 4:3 = 1536x1152、真 9:16 = 1152x2048、真 3:4 = 1152x1536
# （都是 16 的倍数、长宽比 ≤3:1、总像素落在中转站允许区间，落在 2K 计费档）。
# 3:4 是给**抖音封面**用的（1086×1448）—— 用户 2026-09-19：「封面也用api生成 不需要本地」。
IMAGE_SIZES = {"4:3": "1536x1152", "9:16": "1152x2048", "3:4": "1152x1536"}
ProgressCallback = Callable[[float, str], Awaitable[None]]

# 人物图身份自检：相似度必须 ≥70，不够就重做，最多 3 次，留分数最高的那张
# （用户 2026-09-19：「人物相似度 必须70%以上」）。批量备料与 /images 页面共用。
PERSON_MIN_SIMILARITY = 70
PERSON_MAX_ATTEMPTS = 3


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
        "mode": env_value("H3_BATCH_IMAGE_MODE", "auto"),
    }


def request_plans() -> list[tuple[str, str]]:
    """(方案, 图片字段名) 候选，按可靠性排序，失败自动试下一个。

    官方 OpenAI 多图编辑用 `image[]`，但中转站文档只写了单个 `image`，
    所以先试数组、再试单数；`H3_BATCH_IMAGE_FIELD` 可把字段名钉死。
    """
    explicit = env_value("H3_BATCH_IMAGE_FIELD")
    mode = (env_value("H3_BATCH_IMAGE_MODE") or "auto").strip().lower()
    plans: list[tuple[str, str]] = []
    if mode in {"auto", "multi"}:
        plans.append(("multi", explicit or "image[]"))
        if not explicit:
            plans.append(("multi", "image"))
    if mode in {"auto", "composite"}:
        plans.append(("composite", explicit or "image"))
    return plans


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


async def defocus_reference_face(scene_image: Path, work_dir: Path) -> tuple[Path, bool]:
    """把参考帧（图一）里的人脸糊掉，返回 (文件路径, 是否成功糊掉)。

    图一里是**源视频那个人**的脸。直接送进去，模型会把那个人的五官混进成图，
    而用户要求「五官必须和原型图一致」。把这张脸糊掉之后，图一仍然提供发型轮廓、
    服装、场景和灯光，但不再提供任何可用于"换脸"的面部信息。

    定位失败时不阻断流程：返回原图并标记未处理，由上层写进日志。
    """
    from . import batch_ai

    scene_image = Path(scene_image)
    target = Path(work_dir) / f"{scene_image.stem}-noface.png"
    if target.is_file():
        return target, True

    with Image.open(scene_image) as opened:
        image = opened.convert("RGB")
    box = await batch_ai.locate_face(scene_image)
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


async def _post(
    data: dict[str, str], files: list[tuple[str, tuple[str, bytes, str]]]
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        return await client.post(
            f"{base_url()}/images/edits",
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
        download = httpx.get(str(entry["url"]), timeout=REQUEST_TIMEOUT)
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
            try:
                response = await _post(data, files)
                if response.is_error:
                    failures.append(f"[{plan}/{field}] {_api_error(response)}")
                    continue
                _store(response, output_path)
            except httpx.HTTPError as error:
                # 传输层异常（连接失败、超时、TLS 等）不是 HTTP 错误响应，必须一起回退，
                # 否则换个地址/字段名的机会都没有就直接抛穿了。
                failures.append(f"[{plan}/{field}] 连接失败：{type(error).__name__}: {str(error)[:160]}")
                continue
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
    if len(failures) >= 2:
        hint = "。若中转站只支持单图编辑，请设置 H3_BATCH_IMAGE_MODE=composite"
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
    """出人物图并**自带身份自检**：不够门槛就重做，最后留下分数最高的那张。

    用户 2026-09-19：「人物相似度 必须70%以上」。批量备料（全自动链路）与 `/images`
    页面的「生成/重新生成这一步」以前各写一份循环，现在共用这一份，行为不会跑偏。

    2026-09-20 追加**着装自检**（小红书把账号判成「敏感部位裸露凸显、性诱惑内容」之后）：
    每一张生成图都过一次 `batch_ai.judge_compliance`，命中暴露服装 / 紧身透轮廓就
    继续重做；**优先保留合规且分数最高的那张**——如果所有尝试都不合规，仍然收下分数最高的
    那张（自检是质量门槛，不是出片前提，绝不能把整条链路卡死在这儿）。判据本身失败按合规放行。
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

            result = await batch_ai.judge_identity(Path(identity_image), candidate)
            score = max(0, min(100, int(result.get("similarity") or 0)))
        except Exception:  # noqa: BLE001 - 自检失败不该把出图判失败
            candidate.replace(output_path)
            if compliance is not None:
                compliance.update({"compliant": None, "issues": []})
            return output_path, -1
        try:
            compliant, issues = await judge_compliance_safe(candidate)
        except Exception:  # noqa: BLE001 - 自检绝不能把出图判失败（判据的错不是图的错）
            compliant, issues = True, []
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
    failures: list[str] = []
    image_bytes = source_image.read_bytes()
    attempts = [
        (env_value("H3_BATCH_IMAGE_FIELD") or "image", False),
        ("image[]", False),
        # TeamoRouter's gpt-image-2.5 edit route accepts the same multi-image
        # shape used by person generation, but may reject a single reference.
        # Repeating the identical reference preserves identity while satisfying
        # that compatibility requirement.
        ("image[]", True),
    ]
    for field, duplicate in attempts:
        files = [(field, (source_image.name, image_bytes, mime))]
        if duplicate:
            files.append((field, (f"reference-{source_image.name}", image_bytes, mime)))
        try:
            response = await _post(data, files)
            if response.is_error:
                label = f"{field}/double" if duplicate else field
                failures.append(f"[{label}] {_api_error(response)}")
                continue
            return _store(response, output_path)
        except httpx.HTTPError as exc:
            failures.append(f"[{field}] {type(exc).__name__}: {str(exc)[:160]}")
    raise RuntimeError("封面生成失败：" + "；".join(failures))
