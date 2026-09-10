"""候选人物图的远程生成：走 OpenAI 兼容的图片接口（用户自备的国内中转站）。

为什么需要它：本账号在官方 API 有 `gpt-5.6-luna` 的计划内额度，但**没有**
`gpt-image-*` 的余额（`credit_balance_exhausted`），而本地 8GB 卡上的 Krea2
turbo 出图质量不达标。所以出图交给中转站。

凭据只从环境读取，绝不写入前端、数据库或仓库。文本分析与出图是两套独立配置：
中转站一般没有 luna，官方账号又没有图片余额，`H3_BATCH_IMAGE_*` 与
`H3_BATCH_TEXT_*` 互不影响。
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

from .settings import env_value

OFFICIAL_URL = "https://api.openai.com/v1"
REQUEST_TIMEOUT = 900.0
# gpt-image 系列支持的固定画布尺寸：唱歌 4:3 横版、跳舞 9:16 竖版。
IMAGE_SIZES = {"4:3": "1536x1024", "9:16": "1024x1536"}

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
    return env_value("H3_BATCH_IMAGE_MODEL") or "gpt-image-2"


def configured() -> bool:
    """只有用户显式配了中转站才认为可用，避免误把官方 key 打到没有余额的官方图片接口。"""
    return bool(env_value("H3_BATCH_IMAGE_BASE_URL") and api_key())


def describe() -> dict[str, str]:
    return {"baseUrl": base_url(), "model": model(), "configured": "1" if configured() else "0"}


def _api_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error") or body
        message = error.get("message") if isinstance(error, dict) else None
        return f"图片接口错误 {response.status_code}：{message or json.dumps(error, ensure_ascii=False)[:300]}"
    except Exception:
        return f"图片接口错误 {response.status_code}：{response.text[:300]}"


async def generate_candidate_image(
    *,
    scene_image: Path,
    identity_image: Path,
    prompt: str,
    ratio: str,
    output_path: Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """用「图像-1 造型场景 + 图像-2 唯一身份」两张参考图做图片编辑，产出候选人物图。"""
    if ratio not in IMAGE_SIZES:
        raise RuntimeError(f"候选人物图不支持的比例：{ratio}")
    key = api_key()
    if not key:
        raise RuntimeError("尚未配置中转站的图片 API key（H3_BATCH_IMAGE_API_KEY）")

    size = IMAGE_SIZES[ratio]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)

    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for field, path in (("image[]", Path(scene_image)), ("image[]", Path(identity_image))):
        if not path.is_file():
            raise RuntimeError(f"候选人物图缺少输入图片：{path}")
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        files.append((field, (path.name, path.read_bytes(), mime)))
    data = {"model": model(), "prompt": prompt, "size": size, "n": "1"}

    started = time.monotonic()
    stop = False

    async def ticker() -> None:
        """中转站没有节点进度，按耗时如实显示，不编造百分比。"""
        while not stop:
            await asyncio.sleep(5)
            if stop or on_progress is None:
                continue
            minutes, seconds = divmod(int(time.monotonic() - started), 60)
            await on_progress(0.0, f"正在通过中转站生成候选人物图 · 已用 {minutes:02d}:{seconds:02d}")

    ticker_task = asyncio.create_task(ticker()) if on_progress else None
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{base_url()}/images/edits",
                headers={"Authorization": f"Bearer {key}"},
                data=data,
                files=files,
            )
    finally:
        stop = True
        if ticker_task is not None:
            ticker_task.cancel()
            try:
                await ticker_task
            except (asyncio.CancelledError, Exception):
                pass

    if response.is_error:
        raise RuntimeError(_api_error(response))

    payload = response.json()
    entry = (payload.get("data") or [{}])[0]
    encoded = entry.get("b64_json")
    if encoded:
        output_path.write_bytes(base64.b64decode(encoded))
    elif entry.get("url"):
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            download = await client.get(str(entry["url"]))
        if download.is_error:
            raise RuntimeError(f"图片已生成但下载失败：HTTP {download.status_code}")
        output_path.write_bytes(download.content)
    else:
        raise RuntimeError("图片接口没有返回图像数据")
    if on_progress:
        await on_progress(1.0, "候选人物图已生成")
    return output_path


def fallback_notice(reason: Any) -> str:
    return f"中转站出图不可用，已退回源视频取帧：{reason}"
