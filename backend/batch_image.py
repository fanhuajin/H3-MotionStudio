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
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from PIL import Image

from .settings import env_value

OFFICIAL_URL = "https://api.openai.com/v1"
REQUEST_TIMEOUT = 900.0

# 真 4:3 = 1536x1152、真 9:16 = 1152x2048（都是 16 的倍数，落在 2K 计费档）。
IMAGE_SIZES = {"4:3": "1536x1152", "9:16": "1152x2048"}

ProgressCallback = Callable[[float, str], Awaitable[None]]


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
    identity_image: Path,
    prompt: str,
    ratio: str,
    work_dir: Path,
) -> tuple[dict[str, str], list[tuple[str, tuple[str, bytes, str]]], str]:
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    text = prompt
    if plan == "composite":
        combined = compose_reference(scene_image, identity_image, work_dir / "reference-combined.png")
        text = (
            "输入图是一张左右拼合的参考图：左半部分是「图一」（造型、服装、场景、灯光参考），"
            "右半部分是「图二」（唯一的人物身份与面部参考）。请把它当作两张独立参考图理解，"
            "不要输出这种左右拼接的画面。\n\n" + prompt
        )
        paths = [(combined, f"image[]")]
    else:
        paths = [(Path(scene_image), field), (Path(identity_image), field)]
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
    identity_image: Path,
    prompt: str,
    ratio: str,
    output_path: Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """用「图像-1 造型场景 + 图像-2 唯一身份」做图片编辑，产出候选人物图。"""
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
                identity_image=Path(identity_image),
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
