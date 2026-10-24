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
import hashlib
import io
import json
import logging
import mimetypes
import shutil
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import quote, urlparse
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from PIL import Image, ImageFilter, ImageOps

from . import netproxy
from .settings import (
    IMAGE_UPSCALE_SCALE,
    IMAGE_UPSCALE_SETTINGS,
    env_value,
    image_identity_crop,
    image_upscale_engine,
)

logger = logging.getLogger(__name__)

OFFICIAL_URL = "https://api.openai.com/v1"
REQUEST_TIMEOUT = 900.0
# 异步接口只在提交阶段等待接单回执；同步出图及已接单任务仍有独立的长等待。
SUBMIT_TIMEOUT = 120.0
_STATUS_REPORTER: ContextVar[Callable[[str], None] | None] = ContextVar("image_status_reporter", default=None)
_REQUEST_MODEL: ContextVar[str] = ContextVar("image_request_model", default="")


@contextmanager
def report_request_status(callback: Callable[[str], None]):
    """按异步任务隔离状态通知，避免并行作品的接单消息串到其他条目。"""
    token = _STATUS_REPORTER.set(callback)
    try:
        yield
    finally:
        _STATUS_REPORTER.reset(token)


def _report_request_status(message: str) -> None:
    logger.info("%s", message)
    callback = _STATUS_REPORTER.get()
    if callback is not None:
        try:
            callback(message)
        except Exception:
            # 展示失败不能改变已付费任务的执行结果。
            logger.exception("图片请求状态通知失败")

# Sunburst 文档：宽高为 16 的倍数，最大边 <=3840，长宽比 <=3:1，
# 总像素在 655,360~8,294,400 之间。请求显式传像素尺寸，不使用 1k/2k 字符串。
# 旧尺寸表保留供历史任务恢复及低分辨率补放大使用；新请求以 image_sizes() 为准。
IMAGE_SIZES_1K = {"4:3": "1024x768", "9:16": "720x1280", "3:4": "768x1024"}
# 改造前的 2k 档尺寸：`H3_IMAGE_UPSCALE=off` 时整体退回这一套（那时不做本地放大）。
IMAGE_SIZES_2K = {"4:3": "1536x1152", "9:16": "1152x2048", "3:4": "1152x1536"}
# 与 image_workbench.save_upload() 的短边下限保持一致。
MIN_IMAGE_SIDE = 640
# 本地放大**一律等 VOSR2**（用户 2026-09-23：「跑完当前视频后优先把所有的放大做完再开始
# 下一条视频」「VOSR2 2× 这个补」）。放大排的是 pipeline_lock 的 FIFO 队列：视频一让出
# GPU 就先轮到它，下一条视频排在它后面 —— 所以"等"是安全的，退化才是不可接受的。
# lanczos 只在 VOSR2 **真的报错**时兜底，保证这条链路不至于因为放大失败而废掉。
# 「内容政策」这种**明确 400** 的拒绝实测**不计费**（2026-09-23 账单：00:35 被拒那笔在明细里
# 没有任何记录），而它又是随机的 —— 同一份提示词重发常能过。所以只对这种情况自动重发。
# 其它失败（超时/断连）可能已经计费，一律不重发。
POLICY_RETRIES = 2
POLICY_RETRY_DELAY = 5.0


IMAGE_SIZES_4K = {"4:3": "3264x2448", "9:16": "2160x3840", "3:4": "2448x3264"}


def image_sizes() -> dict[str, str]:
    """新请求使用中转站原生4K；既有任务按原尺寸恢复。"""
    return IMAGE_SIZES_4K
ProgressCallback = Callable[[float, str], Awaitable[None]]
_BILLED_INFLIGHT: dict[str, asyncio.Future[tuple[bytes | None, str]]] = {}

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


def is_yeelo() -> bool:
    return urlparse(base_url()).hostname == "img.yeelo.fun"


def async_tasks() -> bool:
    """中转站是否用「提交拿 task_id + GET 轮询」的异步接口（`H3_BATCH_IMAGE_ASYNC` 可覆盖）。

    默认按站点判断（当前的 YeeMagine 就是这套接口）。**不要只靠主机名**：用户已经换过
    一次中转站，主机名写死会让异步适配在新站点上静默失效 —— 对异步站点按同步读响应，
    每次都是「接口没有返回图像数据」。换站点时加一个环境变量即可，不必改代码。
    """
    raw = (env_value("H3_BATCH_IMAGE_ASYNC") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return is_yeelo()


def policy_retry_allowed() -> bool:
    """内容政策 400 是否允许自动重发：只有**实测不计费**的渠道才开。

    旧中转站实测这种 400 不产生记录也不扣费，所以那时自动重发是安全的；换到新站点后
    这个前提没有验证过，默认不开（宁可让这一条失败，也不冒重复扣费的风险）。
    """
    raw = (env_value("H3_BATCH_IMAGE_POLICY_RETRY") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return not async_tasks()


def api_key() -> str:
    return env_value("H3_BATCH_IMAGE_API_KEY") or env_value("OPENAI_API_KEY")


def model() -> str:
    from .image_model_settings import selected_model

    return (
        _REQUEST_MODEL.get() or selected_model() or env_value("H3_BATCH_IMAGE_MODEL")
        or ("gpt-image-2" if is_yeelo() else "gpt-image-2.5-sunburst")
    )


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


def _identity_for_relay(identity_image: Path, work_dir: Path) -> Path:
    """身份图先裁成「头+肩」再送中转站（默认开，`H3_IMAGE_IDENTITY_CROP=off` 可关）。

    为什么：身份原图（`本人固定参考.png`）是带事业线的深 V 照片；中转站的审核按图片判，
    「抽帧 + 身份图」这条人物图请求就比只发抽帧的封面更容易被 400 拦掉（2026-09-23 实测：
    人物图被拦、同一时刻封面放行）。提示词里图二只负责脸型五官（造型/发型/妆容/灯光都由图一
    给），所以裁掉胸口不损失身份，顺带降低"把深 V 抄进成图"的合规风险。

    定位复用本机 Haar；先把原图复制成 ASCII 名再检测，规避 `cv2.imread` 读不了中文路径。
    检测失败/没有 OpenCV 时**原样返回**，不阻断出图。
    """
    if not image_identity_crop():
        return identity_image
    identity_image = Path(identity_image)
    try:
        digest = hashlib.sha1(identity_image.read_bytes()).hexdigest()[:10]
        staged = work_dir / f"identity-src-{digest}.png"
        if not staged.is_file():
            shutil.copy2(identity_image, staged)
        box = _locate_face_local(staged)
        if not box:
            return identity_image
        with Image.open(identity_image) as opened:
            image = opened.convert("RGB")
        width, height = image.size
        x0, y0 = box["x0"] * width, box["y0"] * height
        x1, y1 = box["x1"] * width, box["y1"] * height
        box_w, box_h = x1 - x0, y1 - y0
        centre = (x0 + x1) / 2
        # 左右各留 1.4 倍脸宽（带全头发），上方留 1.1 倍脸高（含发顶），
        # 下方只留 0.2 倍脸高 —— 到下巴/颈部为止，**明确不含胸口与事业线**（审核触发点）。
        crop = (
            int(max(0, centre - box_w * 1.4)), int(max(0, y0 - box_h * 1.1)),
            int(min(width, centre + box_w * 1.4)), int(min(height, y1 + box_h * 0.2)),
        )
        # 文件名里带上裁剪矩形：以后调裁剪参数会自动失效旧缓存，不会一直复用旧裁剪。
        key = hashlib.sha1(f"{digest}:{crop}".encode()).hexdigest()[:10]
        target = work_dir / f"identity-face-{key}.png"
        if target.is_file():
            return target
        image.crop(crop).save(target, format="PNG")
        logger.info("身份图已裁为头肩构图（规避审核、保留五官）：%s", target.name)
        return target
    except Exception as error:  # noqa: BLE001 - 裁剪只是优化，失败就用原图
        logger.warning("身份图裁剪失败（按原图发送）：%s", error)
        return identity_image


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
    # 身份图统一走「头肩裁剪」：审核更容易放行，身份信息不受影响（见 _identity_for_relay）。
    relay_identity = _identity_for_relay(Path(identity_image), work_dir) if identity_image is not None else None
    if plan == "composite":
        if relay_identity is None:
            raise RuntimeError("composite 模式必须同时给场景图与身份图")
        combined = compose_reference(scene_image, relay_identity, work_dir / "reference-combined.png")
        text = (
            "输入图是一张左右拼合的参考图：左半部分是「图一」（人物造型与服装参考；背景按正文判断保留或调整），"
            "右半部分是「图二」（唯一的人物身份与面部参考）。请把它当作两张独立参考图理解，"
            "不要输出这种左右拼接的画面。\n\n" + prompt
        )
        paths = [(combined, f"image[]")]
    else:
        # 人物图使用双参考；新封面仅提交原人物图，旧任务保留其原始参考以便续取。
        paths = [(Path(scene_image), field)]
        if relay_identity is not None:
            paths.append((relay_identity, field))
    for path, name in paths:
        if not path.is_file():
            raise RuntimeError(f"候选人物图缺少输入图片：{path}")
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        files.append((name, (path.name, path.read_bytes(), mime)))

    data = {
        "model": model(),
        "prompt": text,
        "size": image_sizes()[ratio],
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
    """用本机 OpenCV Haar 定位可信人脸；没有依赖或没识别到就返回 None，不调用计费 API。

    ⚠️ 必须用 `np.fromfile + cv2.imdecode` 读图：`cv2.imread` 在 Windows 上**读不了带中文的
    路径**，而发布目录/中间目录全是中文名（`发布成品\\singing\\011_唱歌_爱情转移_...\\.h3-person-work`）
    —— 实测 `cv2.imread` 会静默返回 None，于是糊脸与身份图裁脸都悄悄失效（日志里只有一行
    `[WARN] imread_ ... can't open/read file`）。
    """
    try:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

        frame = cv2.imdecode(np.fromfile(str(image_path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=4, minSize=(40, 40))
        if len(faces) == 0:
            return None
        image_height, image_width = gray.shape[:2]
        eyes = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
        candidates = []
        for x, y, width, height in faces:
            # Haar 会把衣服或胸前的纹理误判成更大的脸。先核对眼睛，再按画面位置选框。
            upper_face = gray[y:y + int(height * 0.6), x:x + width]
            eye_boxes = eyes.detectMultiScale(upper_face, scaleFactor=1.08, minNeighbors=4, minSize=(12, 12))
            eye_centers = sorted(int(ex + ew / 2) for ex, ey, ew, eh in eye_boxes)
            has_eye_pair = any(
                width * 0.15 <= right - left <= width * 0.8
                for i, left in enumerate(eye_centers) for right in eye_centers[i + 1:]
            )
            if has_eye_pair:
                candidates.append((int(x), int(y), int(width), int(height)))
        if not candidates:
            return None
        x, y, width, height = max(
            candidates,
            key=lambda row: (row[2] * row[3]) / (1 + row[1] / image_height),
        )
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


def _locate_source_faces(image_path: Path) -> list[dict[str, float]]:
    """复用 ComfyUI 的 ONNX Runtime，在独立 CPU 进程定位全部人脸。"""
    import subprocess
    from .settings import COMFY_ROOT, COMFY_PYTHON
    model = COMFY_ROOT / "models/insightface/models/buffalo_l/det_10g.onnx"
    if not model.is_file() or not COMFY_PYTHON.is_file():
        logger.error("缺少本地 SCRFD 模型或检测运行环境，停止使用未处理抽帧")
        return []
    script = Path(__file__).resolve().parents[1] / "scripts/locate_source_faces.py"
    try:
        result = subprocess.run(
            [str(COMFY_PYTHON), str(script), str(image_path), str(model)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        logger.exception("本地 SCRFD 人脸定位失败，停止使用未处理抽帧")
        return []


def prepare_defocused_reference(scene_image: Path, work_dir: Path) -> Path:
    """强制屏蔽图一全部检测到的脸；定位失败不允许原图进入生图。"""
    scene_image = Path(scene_image)
    boxes = _locate_source_faces(scene_image)
    if not boxes:
        raise ValueError("抽帧人脸定位失败，已停止提交人物图生成，避免融合原视频人物的脸。请换一张脸部清楚的抽帧。")
    with Image.open(scene_image) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    for box in boxes:
        _blur_region(image, box)
    target = Path(work_dir) / f"{scene_image.stem}-noface.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    # 复制/下载与生图可能同时请求图一；原子替换避免读到半写入 PNG。
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".png", delete=False) as temporary:
        staging = Path(temporary.name)
    try:
        image.save(staging, format="PNG")
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return target


async def defocus_reference_face(scene_image: Path, work_dir: Path) -> tuple[Path, bool]:
    """批量与图片页共用强制糊脸；失败抛错，不返回原图。"""
    target = await asyncio.to_thread(prepare_defocused_reference, scene_image, work_dir)
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
# 自动切换过的线路（代理, 说明）：切成功之后钉住，避免每次出图又被配置里的坏线路覆盖。
_route_override: tuple[str | None, str] | None = None


def _route(key: str = _ROUTE_KEY) -> str | None:
    """当前这次出图该用的代理（None = 直连）。"""

    return _route_proxy


async def _prepare_route() -> str:
    """出图之前判定一次出网路线：**直连优先，直连不通才走代理**。

    路线说明记进 `_route_note`，调用方写进日志 —— 用户能看到这次到底走没走代理。

    ⚠️ 已经**自动切换过**的线路不再被覆盖（`_route_override`）：`netproxy` 对
    `H3_IMAGE_PROXY` 是"钉死、不探测"，而启动脚本只要发现本机有代理端口就会把它设上
    （2026-09-23 实测：代理 SSL 直接 EOF，直连 0.4s 正常）。若每次出图都重新按配置覆盖，
    自愈结果会被冲掉、每次都要先白等一次连接失败。
    """
    global _route_proxy, _route_note
    if _route_override is not None:
        _route_proxy, _route_note = _route_override
        return _route_note
    proxy, note = await netproxy.probe_route_async(f"{base_url()}/models")
    _route_proxy = proxy or None
    _route_note = note
    return note


def route_note() -> str:
    """本次出图**实际**用的出网方式（给日志/报错用）。"""
    return _route_note or netproxy.describe()


_KEEP_ROUTE = object()          # `_post(proxy=...)` 的哨兵：没传就用当前判定好的路线


class RelayUnreachable(RuntimeError):
    """两条线路都**没能建立连接**：请求没送到中转站 → **一定没有计费**。

    与"可能已计费"的失败（读超时、连接中途断开、内容政策 400 之外的服务端错误）区分开：
    只有这种情况可以安全地作废 `.request-pending` 标记（那个标记是"可能已计费、别再盲发"
    的保护）。2026-09-23 实测：坏代理连续失败 3 条，留下的标记把后面的条目**永久钉死**，
    而批准备料这条路没有手动出口 —— 所以确定没计费时必须把标记删掉。
    """


class PolicyRejected(RuntimeError):
    """图片请求明确收到内容政策 400，结果已确定为拒绝。"""


def _is_not_sent(failure: str) -> bool:
    """失败记录是不是"连接根本没建立"（请求没送出）。"""
    return "连接失败：ConnectError" in failure or "连接失败：ConnectTimeout" in failure


async def _post(
    data: dict[str, str],
    files: list[tuple[str, tuple[str, bytes, str]]],
    *,
    proxy: object = _KEEP_ROUTE,
) -> httpx.Response:
    # 出网路线由 `generate_candidate_image` 事先判定好（`_prepare_route`）。
    # 2026-09-22 事故：直连被解析到无效 IP、全部超时，出图失败后 `batch_worker`
    # 按设计退回"源视频取帧"，用户看到的就是「人物图 = 抽帧图」。
    url = f"{base_url()}/images/edits"
    route = _route() if proxy is _KEEP_ROUTE else proxy
    asynchronous = async_tasks()
    if asynchronous:
        data = {k: v for k, v in data.items() if k != "input_fidelity"}
        data.update({"async": "true", "response_format": "url"})
    timeout = httpx.Timeout(SUBMIT_TIMEOUT, connect=30.0) if asynchronous else REQUEST_TIMEOUT
    _report_request_status("正在提交图片请求，等待中转站接单回执（尚未确认受理）")
    async with httpx.AsyncClient(timeout=timeout, proxy=route) as client:
        try:
            return await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key()}"},
                data=data,
                files=files,
            )
        except httpx.ReadTimeout as error:
            netproxy.note_failure()
            raise RuntimeError(
                "图片提交阶段等待响应超时（ReadTimeout），未收到任务 ID 或图片，"
                "无法确认中转站是否受理；保留未知请求保护，不自动重发。请核对渠道后台"
            ) from error


def _alternative_route(current: str | None) -> tuple[bool, str | None]:
    """当前线路之外的**另一条**线路：走代理时给直连，直连时给配置/自动发现的代理。"""
    if current:
        return True, None                      # 现在走代理 → 换成直连
    proxy = netproxy.proxy_url()
    return (True, proxy) if proxy else (False, None)


async def _post_with_route_fallback(
    data: dict[str, str], files: list[tuple[str, tuple[str, bytes, str]]]
) -> httpx.Response:
    """发出图请求；**只在"连接根本没建立"时**换另一条线路重试一次。

    为什么只在这种情况重试：`ConnectError` / `ConnectTimeout` 表示 TCP/TLS 都还没建起来，
    请求根本没送到中转站 → **一定没有计费**，换条路重发是安全的。用户 2026-09-23 实测：
    启动脚本按端口探测把出图钉死在代理上（`H3_IMAGE_PROXY` 一设，`netproxy` 就跳过探测），
    代理一坏，**所有出图全挂**、批次跟着一条条失败。

    其它 `httpx.HTTPError`（读超时、连接中途断开）**可能已经出图计费**，仍然一律不重发。
    切过去成功之后把线路**钉在本进程**，后续请求不再走坏掉的那条（自愈）。
    """
    global _route_proxy, _route_note, _route_override

    try:
        return await _post(data, files)
    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
        current = _route()
        has_other, other = _alternative_route(current)
        if not has_other:
            raise
        logger.warning(
            "出图线路（%s）连接失败：%s —— 请求未送出、不会计费，改用另一条线路重试一次",
            route_note(), str(error)[:120],
        )
        response = await _post(data, files, proxy=other)
        _route_proxy = other
        _route_note = (
            f"{'直连' if not other else f'代理 {other}'}（原线路连不上，已自动切换）"
        )
        # 钉住这条能用的线路：`_prepare_route()` 下次不会再按配置覆盖回坏掉的那条。
        _route_override = (other, _route_note)
        return response


def _task_checkpoint(output_path: Path) -> Path:
    return output_path.with_name(output_path.name + ".relay-task.json")


def _result_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Accept OpenAI results and task wrappers without mistaking a task ID for an image."""
    for key in ("result", "response", "output"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            result = _result_payload(nested)
            if result:
                return result
    data = payload.get("data")
    if isinstance(data, dict):
        return _result_payload(data)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if data[0].get("url") or data[0].get("b64_json"):
            return {"data": data}
    images = payload.get("images")
    if isinstance(images, list) and images:
        entries = [{"url": item} if isinstance(item, str) else item for item in images]
        if all(isinstance(item, dict) and (item.get("url") or item.get("b64_json")) for item in entries):
            return {"data": entries}
    if payload.get("image_url") or payload.get("result_url") or payload.get("b64_json"):
        return {"data": [{"url": payload.get("image_url") or payload.get("result_url"), "b64_json": payload.get("b64_json")} ]}
    return None


def _save_task_record(checkpoint: Path, record: dict[str, Any]) -> None:
    temporary = checkpoint.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    temporary.replace(checkpoint)


def _clear_task_checkpoint(output_path: Path) -> None:
    """图片已经落盘 → 记录完成使命，删掉。

    留着有两个坏处：① 同样的输入再跑一次时会直接拿旧结果，用户明确点「重新生成」也
    不会真的重出图；② 记录里的下载地址有时效，过期后只会一直报「图片已生成但下载失败」，
    只能靠人工找到这个隐藏文件删掉才能恢复。
    """
    _task_checkpoint(output_path).unlink(missing_ok=True)


async def _poll_task_once(task_id: str) -> dict[str, Any] | None:
    """GET 一次任务（**不计费**）：读不到就返回 None，绝不触发新的出图请求。"""
    try:
        async with httpx.AsyncClient(timeout=60, proxy=_route()) as client:
            polled = await client.get(
                f"{base_url()}/images/tasks/{quote(str(task_id), safe='')}",
                headers={"Authorization": f"Bearer {api_key()}"},
            )
    except httpx.HTTPError:
        return None
    if polled.is_error:
        return None
    try:
        return polled.json()
    except ValueError:
        return None


async def _refresh_result(record: dict[str, Any], checkpoint: Path) -> dict[str, Any] | None:
    """存下的结果地址可能会过期（私有对象存储有时效）：重读任务换一个新地址，仍然不计费。"""
    task_id = str(record.get("taskId") or "")
    if not task_id:
        return None
    body = await _poll_task_once(task_id)
    if not body:
        return None
    result = _result_payload(body)
    if not result:
        return None
    record["result"] = result
    _save_task_record(checkpoint, record)
    return result


async def _resolve_task(response: httpx.Response, output_path: Path, fingerprint: str = "") -> httpx.Response:
    # 不按主机名判断：任何返回「task_id + status」的中转站都按异步任务处理，
    # 已经是图片结果的就原样交回 `_store`。未知形态一律 fail closed（不自动重发）。
    if response.is_error:
        return response
    payload = response.json()
    result = _result_payload(payload)
    if result:
        return httpx.Response(200, json=result)
    task = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    task_id = task.get("task_id") or task.get("id")
    if not task_id:
        raise RuntimeError("图片接口未返回图片或任务 ID；保留未知请求保护，不自动重发")
    checkpoint = _task_checkpoint(output_path)
    record = {"baseUrl": base_url(), "model": model(), "taskId": str(task_id), "fingerprint": fingerprint}
    # 先落盘再轮询：中途被打断时下次只会 GET 同一条任务，绝不重新提交（不重复计费）。
    _save_task_record(checkpoint, record)
    _report_request_status(f"中转站已受理图片任务（任务 ID：{task_id}），任务记录已保存，正在查询结果")
    label = f"（任务 {task_id}）"
    deadline = time.monotonic() + REQUEST_TIMEOUT
    async with httpx.AsyncClient(timeout=60, proxy=_route()) as client:
        while time.monotonic() < deadline:
            try:
                polled = await client.get(
                    f"{base_url()}/images/tasks/{quote(str(task_id), safe='')}",
                    headers={"Authorization": f"Bearer {api_key()}"},
                )
            except httpx.HTTPError:
                await asyncio.sleep(5)
                continue
            if polled.status_code == 429 or polled.status_code >= 500:
                await asyncio.sleep(5)
                continue
            if polled.is_error:
                raise RuntimeError(f"图片任务查询失败 HTTP {polled.status_code}{label}；已保存任务 ID，不自动重新生成")
            body = polled.json()
            task = body.get("data") if isinstance(body.get("data"), dict) else body
            status = str(task.get("status", "")).lower()
            if status in {"failed", "failure", "error", "cancelled", "canceled"}:
                record["terminalFailure"] = status
                _save_task_record(checkpoint, record)
                raise RuntimeError(f"图片任务失败{label}；保留任务记录，请核对渠道后台后手动处理")
            result = _result_payload(body)
            if result:
                record["result"] = result
                _save_task_record(checkpoint, record)  # 下载还能重试（GET），不必重新出图
                return httpx.Response(200, json=result)
            if status in {"done", "completed", "succeeded", "success"}:
                raise RuntimeError(f"图片任务已结束但未识别到图像数据{label}；已保存任务 ID，不自动重发")
            try:
                delay = max(5, min(60, float(polled.headers.get("Retry-After", "5"))))
            except ValueError:
                delay = 5
            await asyncio.sleep(delay)
    raise RuntimeError(f"图片任务等待超时{label}；已保存任务 ID，不自动重新生成")


async def _submit_or_resume(data: dict[str, str], files: list, output_path: Path) -> httpx.Response:
    # 已付费任务只续取原模型；异步隔离也避免保存设置影响正在提交的请求。
    checkpoint = _task_checkpoint(output_path)
    request_model = data["model"]
    if checkpoint.is_file():
        record = json.loads(checkpoint.read_text(encoding="utf-8"))
        request_model = record.get("model") or request_model
    token = _REQUEST_MODEL.set(request_model)
    try:
        return await _submit_or_resume_request(dict(data, model=request_model), files, output_path)
    finally:
        _REQUEST_MODEL.reset(token)


async def _submit_or_resume_request(data: dict[str, str], files: list, output_path: Path) -> httpx.Response:
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode())
    for field, (name, content, mime) in files:
        digest.update(field.encode())
        digest.update(content)
    fingerprint = digest.hexdigest()
    checkpoint = _task_checkpoint(output_path)
    if checkpoint.is_file():
        record = json.loads(checkpoint.read_text(encoding="utf-8"))
        if record.get("baseUrl") != base_url() or record.get("model") != model():
            raise RuntimeError("存在其他渠道的任务记录，请先人工核对，不自动重发")
        if record.get("fingerprint") != fingerprint:
            # Resolution migration: accept only an otherwise identical legacy request.
            legacy_match = False
            for sizes in (IMAGE_SIZES_1K, IMAGE_SIZES_2K):
                for ratio, size in sizes.items():
                    if data.get("size") != IMAGE_SIZES_4K[ratio]:
                        continue
                    legacy_data = dict(data, size=size)
                    legacy_digest = hashlib.sha256(json.dumps(legacy_data, sort_keys=True).encode())
                    for field, (name, content, mime) in files:
                        legacy_digest.update(field.encode())
                        legacy_digest.update(content)
                    if legacy_digest.hexdigest() == record.get("fingerprint"):
                        fingerprint = record["fingerprint"]
                        legacy_match = True
                        break
                if legacy_match:
                    break
            if not legacy_match:
                raise RuntimeError("任务记录与当前输入不同，请先人工核对旧任务，不自动重发")
        if record.get("result"):
            fresh = await _refresh_result(record, checkpoint)
            return httpx.Response(200, json=fresh or record["result"])
        response = httpx.Response(200, json={"task_id": record["taskId"]})
    else:
        response = await _post_with_route_fallback(data, files)
    return await _resolve_task(response, output_path, fingerprint)


def _store(response: httpx.Response, output_path: Path) -> Path:
    payload = response.json()
    result = _result_payload(payload)
    if not result:
        raise RuntimeError("图片接口未返回图片，仅文字或未知响应不能作为生成成功；不自动重发")
    entry = result["data"][0]
    encoded = entry.get("b64_json")
    if encoded:
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise RuntimeError("图片接口返回的图片编码无效；保留任务记录，不自动重发") from error
    elif entry.get("url"):
        # 图片地址由中转站给出（可能是境外对象存储/COS），沿用**这次出图用的那条路线**。
        # 这张图**已经花钱出好了**，下载失败就白花 —— 所以连接没建立时换另一条线路再下一次
        # （同一个地址重复 GET 不会产生费用）。
        image_url = str(entry["url"])
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT, proxy=_route()) as client:
                download = client.get(image_url)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            has_other, other = _alternative_route(_route())
            if not has_other:
                raise
            logger.warning("图片下载线路连不上，改用另一条线路重下（同一张图，不额外计费）")
            with httpx.Client(timeout=REQUEST_TIMEOUT, proxy=other) as client:
                download = client.get(image_url)
        if download.is_error:
            raise RuntimeError(f"图片已生成但下载失败：HTTP {download.status_code}")
        content = download.content
    else:
        raise RuntimeError("图片接口没有返回图像数据")
    # HTTP 200 / URL / base64 都不能证明内容是图像；校验后才覆盖文件和清理任务记录。
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise RuntimeError("图片接口返回内容不是可用图片（可能只有文字或错误页）；保留任务记录，不自动重发") from error
    output_path.write_bytes(content)
    return output_path


def _is_policy_rejection(response: httpx.Response) -> bool:
    """是否是「内容政策」这类**明确 400** 拒绝 —— 只有这一种允许重发同一请求。

    实测（2026-09-23 账单）这种 400 **不产生任何计费**；而超时/断连有可能已经出图计费，
    重发就是双重扣费，所以那些一律不重发。
    """
    if not policy_retry_allowed() or response.status_code != 400:
        return False
    text = _api_error(response).lower()
    return "内容政策" in text or "content policy" in text


_RATIO_BY_ASPECT = ((4 / 3, "4:3"), (3 / 4, "3:4"), (9 / 16, "9:16"))


def ratio_of_size(width: int, height: int) -> str | None:
    """按宽高比认出这是哪个画布档（容差 3.5%，与 `image_workbench` 的比例校验同口径）。"""
    if not height:
        return None
    actual = width / height
    for expected, name in _RATIO_BY_ASPECT:
        if abs(actual - expected) / expected <= 0.035:
            return name
    return None


def ensure_deliverable_image(
    image: Image.Image, ratio: str | None = None
) -> tuple[Image.Image, str | None]:
    """交付前的最后一道保险：把「放大没跑成」的 1k 图本地补到改造前的交付尺寸。

    什么时候会没跑成：本地放大是内存里的任务，后端在排队期间被杀/重启就没了；此时中转站那张
    1k 图还在（9:16 只有 576×1024，短边低于 `save_upload()` 的 640 下限、也低于交付尺寸）。
    补法是纯本地 lanczos：**不花钱、不需要 ComfyUI**，尺寸回到改造前的水准，画质略软但可交付。
    已经够大就原样返回。返回 `(图, 说明)`，说明为 None 表示没动过。
    """
    width, height = image.size
    name = ratio or ratio_of_size(width, height)
    if name is None:
        return image, None
    target = tuple(int(part) for part in IMAGE_SIZES_2K[name].split("x"))
    if width >= target[0] and height >= target[1]:
        return image, None
    return image.resize(target, Image.LANCZOS), f"{width}x{height} → {target[0]}x{target[1]}"


def ensure_deliverable_file(path: Path, ratio: str | None = None) -> Path:
    """同 `ensure_deliverable_image()`，但对文件就地处理（发布落盘前用）。

    原图先留一份 `-relay1k` 备份再覆盖，便于排查；失败只记日志、按原图交付。
    """
    path = Path(path)
    try:
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        fixed, note = ensure_deliverable_image(image, ratio)
        if note is None:
            return path
        backup = path.with_name(f"{path.stem}-relay1k{path.suffix}")
        if not backup.is_file():
            shutil.copy2(path, backup)
        fixed.save(path, format="JPEG" if path.suffix.lower() in {".jpg", ".jpeg"} else "PNG")
        logger.warning("交付前尺寸保险：%s %s（本地 lanczos 补）", path.name, note)
    except Exception as error:  # noqa: BLE001 - 保险失败不该阻断交付
        logger.warning("交付前尺寸保险失败（按原图交付）：%s", error)
    return path


def needs_local_upscale(ratio: str) -> bool:
    """只要请求尺寸小于交付尺寸，就必须执行本地放大。"""
    try:
        width, height = (int(part) for part in image_sizes()[ratio].split("x"))
        target_width, target_height = (int(part) for part in IMAGE_SIZES_2K[ratio].split("x"))
    except (KeyError, ValueError):
        return False
    return width < target_width or height < target_height


def _lanczos_upscale_to_2k(path: Path, ratio: str, force: bool = False) -> Path:
    """放大不可用时的退化路径：lanczos 放大到 2k 档尺寸。

    `force=False` 只在短边低于 `save_upload()` 的 640 下限时才动手（9:16 的 1k 档是
    576x1024）；`force=True` 用于"VOSR2 没跑成"（ComfyUI 忙/报错）——这时统一补到 2k 档
    尺寸，免得封面拿着一张 1024 宽的图去被 `TARGETS` 硬拉大到 1448，发虚。
    """
    with Image.open(path) as opened:
        image = opened.convert("RGB")
        size = image.size
    if not force and min(size) >= MIN_IMAGE_SIDE:
        return path
    target = tuple(int(part) for part in IMAGE_SIZES_2K[ratio].split("x"))
    if size == target:
        return path
    backup = path.with_name(f"{path.stem}-relay1k{path.suffix}")
    if not backup.is_file():
        shutil.copy2(path, backup)
    image.resize(target, Image.LANCZOS).save(
        path, format="JPEG" if path.suffix.lower() in {".jpg", ".jpeg"} else "PNG")
    logger.warning("本地放大不可用，已退化为 lanczos 到 %sx%s：%s", *target, path.name)
    return path


async def _upscale_after_relay(
    path: Path,
    ratio: str,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """低分辨率图片用本地 VOSR2 补放大；封面原图直存不调用此步骤。

    **排队等 GPU，不设上限**：放大要 ComfyUI，而 ComfyUI 被 pipeline_lock 串行（H3 采样
    一跑就是几十分钟）。放大是 FIFO 排队的，视频一让出显卡就先轮到它、下一条视频排在它
    后面 —— 这正是用户要的「跑完当前视频后优先把所有放大做完，再开始下一条视频」。

    只有 VOSR2 **真的报错**时才退化为 lanczos（不花钱、纯本地），保证这条链路不废。
    """
    if image_upscale_engine() != "vosr2" or not needs_local_upscale(ratio):
        return path
    from .batch_portrait import run_api_prompt, stage_input
    from .pipeline import pipeline_lock, resources

    path = Path(path)
    try:
        # 兜底：以**实际拿到的尺寸**为准。中转站一直按请求尺寸返回，但它若哪天改成
        # 自己取整/放大，这里能保证不会把一张 2k 图再放大成 4k。
        with Image.open(path) as probe:
            returned = probe.size
        target_size = tuple(int(part) for part in IMAGE_SIZES_2K[ratio].split("x"))
        if returned[0] >= target_size[0] and returned[1] >= target_size[1]:
            logger.info("中转站返回 %sx%s（非 1k 档），跳过本地放大", *returned)
        else:
            name = await asyncio.to_thread(stage_input, path, "upscale_src")
            prompt = {
                "1": {"class_type": "LoadImage", "inputs": {"image": name}},
                "2": {"class_type": "TESpeedVOSR2Loader", "inputs": {
                    "model_bundle": "VOSR2", "precision": "auto", "memory_policy": "auto",
                    "torch_compile": False, "vae_encode_amp": False}},
                "3": {"class_type": "TESpeedVOSR2Settings", "inputs": dict(IMAGE_UPSCALE_SETTINGS)},
                "4": {"class_type": "TESpeedVOSR2Image", "inputs": {
                    "model": ["2", 0], "images": ["1", 0], "settings": ["3", 0],
                    "scale": IMAGE_UPSCALE_SCALE, "seed": 666}},
                "5": {"class_type": "SaveImage", "inputs": {
                    "images": ["4", 0], "filename_prefix": "h3_image_upscale"}},
            }
            if on_progress:
                # 说清楚"卡在哪"：GPU 被别的任务占着时，这里是在**排队**，不是出图慢
                # （2026-09-23 用户看到「已用 06:10」以为卡死，实际图早在 08:17 就出好了）。
                waiting = "：排队等 GPU（有视频在跑，视频结束后先做放大）" if pipeline_lock.locked() else ""
                await on_progress(1.0, f"图片已生成，正在本地放大（VOSR2）{waiting}")
            if pipeline_lock.locked():
                logger.info("GPU 正忙，本地放大排队等 VOSR2（视频跑完后优先执行）：%s", path.name)
            async with pipeline_lock:
                await resources.ensure_comfy()
                produced = await run_api_prompt(prompt)
            # 中转站那张 1k 原图留一份（`-relay1k` 后缀）便于对比排查，主文件换成放大后的版本。
            backup = path.with_name(f"{path.stem}-relay1k{path.suffix}")
            if not backup.is_file():
                await asyncio.to_thread(shutil.copy2, path, backup)
            await asyncio.to_thread(shutil.copy2, produced, path)
            logger.info("出图后 VOSR2 放大完成：%s", path.name)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 本地放大失败不该拖垮出图链路
        logger.warning("出图后 VOSR2 放大失败，退化为本地 lanczos：%s", error)
        return await asyncio.to_thread(_lanczos_upscale_to_2k, path, ratio, True)
    return path


async def _generate_candidate_image_once(
    *,
    scene_image: Path,
    identity_image: Path | None = None,
    prompt: str,
    ratio: str,
    output_path: Path,
    on_progress: ProgressCallback | None = None,
    upscale: bool = True,
    retain_checkpoint: bool = False,
) -> Path:
    """用「图像-1 造型场景 + 图像-2 唯一身份」做图片编辑，产出候选人物图。

    `identity_image=None` 时**只发一张参考图** —— 封面就是这么用的（见 `_build_request`
    里那段说明：两张不同服装的人像会让模型画出两个人）。
    """
    if ratio not in image_sizes():
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
    policy_rejected = False
    try:
        plans = request_plans()
        if not plans:
            raise RuntimeError(
                "H3_BATCH_IMAGE_MODE 配置不正确；可选 auto / multi（多图编辑）/ composite（拼合成单图）"
            )
        for plan, field in plans:
            # 组请求也放进 try 里，以便把 composite 缺第二张参考图之类的配置错误翻译成
            # 可读提示。`request_plans()` 现在始终只返回一套显式方案，任何失败都不会再拿
            # 真实出图请求试下一种格式。
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
                response = await _submit_or_resume(data, files, output_path)
                # 「内容政策」这种明确 400 是**随机且不计费**的（2026-09-23 账单实测），
                # 同一条请求重发常能过；其它失败一律不重发（可能已经出图计费）。
                policy_attempt = 0
                while (
                    response.is_error
                    and policy_attempt < POLICY_RETRIES
                    and _is_policy_rejection(response)
                ):
                    policy_attempt += 1
                    logger.warning(
                        "中转站以内容政策拒绝（第 %s 次，不计费），重发同一请求：%s",
                        policy_attempt, _api_error(response)[:120],
                    )
                    if on_progress:
                        await on_progress(0.0, f"中转站以内容政策拒绝，正在重试（{policy_attempt}/{POLICY_RETRIES}）")
                    await asyncio.sleep(POLICY_RETRY_DELAY * policy_attempt)
                    response = await _submit_or_resume(data, files, output_path)
                if response.is_error:
                    failures.append(f"[{plan}/{field}] {_api_error(response)}")
                    if _is_policy_rejection(response):
                        policy_rejected = True
                    continue
                _store(response, output_path)
                if not retain_checkpoint:
                    _clear_task_checkpoint(output_path)
            except httpx.HTTPError as error:
                # 请求可能已被中转站接收并计费，只是响应返回途中超时/断线。
                # 不自动换线路再发一遍，避免同一张图产生第二笔费用。
                failures.append(f"[{plan}/{field}] 连接失败：{type(error).__name__}: {str(error)[:160]}")
                netproxy.note_failure()
                break
            except RuntimeError as error:
                # 包括输入不适用、提交结果未知和已接单任务失败；均不盲目换方案。
                failures.append(f"[{plan}/{field}] 请求失败：{error}")
                break
            # 仅在需要时补做本地放大，达到目标尺寸的图片跳过。
            # 先停掉"正在通过中转站生成…"的计时器：接下来是**本地放大排队等 GPU**，
            # 视频采样期间可能要排几十分钟，继续显示中转站计时会让人以为卡在出图那一步
            # （2026-09-23 用户：「我看到你一动不动」——其实是排在视频后面等显卡）。
            state["stop"] = True
            if ticker_task is not None:
                ticker_task.cancel()
            if upscale:
                await _upscale_after_relay(output_path, ratio, on_progress)
            if on_progress:
                await on_progress(
                    1.0 if upscale else 0.0,
                    "候选人物图已生成" if upscale else "原始图片已生成，等待放大前检查",
                )
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
        if policy_rejected:
            hint = (
                f"。内容政策这类拒绝已自动重发过 {POLICY_RETRIES} 次（实测这种 400 不计费），"
                "仍未通过；请核对中转站后台后再手动重做"
            )
        else:
            hint = "。为避免重复扣费，本次没有自动改格式或换线路重试；请确认中转站后台后再手动重做"
    # 全是"连接根本没建立"（两条线路都试过）→ 请求没送到中转站、**一定没计费**。
    # 用专门的异常类型报上去：调用方能据此安全作废 `.request-pending` 标记。
    if failures and all(_is_not_sent(failure) for failure in failures):
        raise RelayUnreachable(
            f"中转站出图失败：{detail}。请求**没有送到中转站**（一定没有计费）："
            "换另一条线路也没连上，请检查网络或代理后重试"
        )
    if policy_rejected:
        raise PolicyRejected(f"中转站出图失败：{detail}{hint}")
    raise RuntimeError(f"中转站出图失败：{detail}{hint}")


async def generate_candidate_image(
    *,
    scene_image: Path,
    identity_image: Path | None = None,
    prompt: str,
    ratio: str,
    output_path: Path,
    on_progress: ProgressCallback | None = None,
    request_key: str | None = None,
    upscale: bool = True,
    retain_checkpoint: bool = False,
) -> Path:
    """计费出图入口；相同 request_key 正在执行时共享结果，不重复提交。

    retain_checkpoint=True 时由调用方在发布文件可靠保存后清理任务记录（封面使用）。
    """
    output_path = Path(output_path)
    key = str(request_key or "").strip()
    if not key:
        return await _generate_candidate_image_once(
            scene_image=scene_image, identity_image=identity_image, prompt=prompt, ratio=ratio,
            output_path=output_path, on_progress=on_progress, upscale=upscale,
            retain_checkpoint=retain_checkpoint,
        )
    existing = _BILLED_INFLIGHT.get(key)
    if existing is not None:
        payload, error = await asyncio.shield(existing)
        if payload is None:
            raise RuntimeError(error)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(payload)
        return output_path

    completion: asyncio.Future[tuple[bytes | None, str]] = asyncio.get_running_loop().create_future()
    _BILLED_INFLIGHT[key] = completion
    try:
        result = await _generate_candidate_image_once(
            scene_image=scene_image, identity_image=identity_image, prompt=prompt, ratio=ratio,
            output_path=output_path, on_progress=on_progress, upscale=upscale,
            retain_checkpoint=retain_checkpoint,
        )
        completion.set_result((result.read_bytes(), ""))
        return result
    except BaseException as exc:
        if not completion.done():
            completion.set_result((None, str(exc)))
        raise
    finally:
        _BILLED_INFLIGHT.pop(key, None)


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
    request_key: str | None = None,
) -> tuple[Path, int]:
    """直接采用生成结果，不调用 DS 自检；保留图片请求检查点和本地放大。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = output_path.with_name(f"{output_path.stem}_attempt1{output_path.suffix}")
    pending = candidate.with_name(candidate.name + ".request-pending")
    if not output_path.is_file() or not output_path.stat().st_size:
        if not candidate.is_file() or not candidate.stat().st_size:
            # 有中转站任务记录时**允许续跑**：那条记录说明请求已经提交过，续跑只会 GET
            # 同一条任务（或直接取已存的结果），不会产生第二笔费用 —— 比卡住等人处理安全。
            if pending.is_file() and not _task_checkpoint(candidate).is_file():
                raise RuntimeError(
                    "上一次人物图请求已经提交但结果未确认；为避免重复扣费，本次没有自动重发。"
                    "请先核对中转站后台，再到图片工作台明确重做"
                )
            pending.write_text(
                json.dumps({"submittedAt": time.time(), "model": model(), "ratio": ratio}),
                encoding="utf-8",
            )
            try:
                await generate_candidate_image(
                    scene_image=Path(scene_image),
                    identity_image=Path(identity_image) if identity_image is not None else None,
                    prompt=prompt, ratio=ratio, output_path=candidate,
                    on_progress=on_progress, request_key=request_key, upscale=False,
                )
            except BaseException as error:
                if isinstance(error, (RelayUnreachable, PolicyRejected)):
                    pending.unlink(missing_ok=True)
                raise
            else:
                pending.unlink(missing_ok=True)
        output_path.write_bytes(candidate.read_bytes())
    if compliance is not None:
        compliance.update({"compliant": None, "issues": []})
    await _upscale_after_relay(output_path, ratio, on_progress)
    return output_path, -1


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
        response = await _submit_or_resume(data, files, output_path)
    except httpx.HTTPError as exc:
        netproxy.note_failure()
        raise RuntimeError(
            f"封面生成请求未确认成功：{type(exc).__name__}: {str(exc)[:160]}。"
            "为避免重复扣费，本次没有自动重试"
        ) from exc
    if response.is_error:
        raise RuntimeError(f"封面生成失败：{_api_error(response)}（未自动改格式重试）")
    stored = _store(response, output_path)
    _clear_task_checkpoint(output_path)
    return stored
