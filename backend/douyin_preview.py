"""Make local video inputs and Douyin downloads browser-playable.

Upload previews use a temporary H.264 cache while preserving the pipeline's
original input. Completed Douyin downloads are different: they are converted
to H.264/AAC MP4 and atomically replace the downloaded source file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .settings import DATA_DIR

logger = logging.getLogger("h3.douyin_preview")

WEB_CACHE_DIR = DATA_DIR / "douyin-web"

# Codecs every Chromium/Firefox build on Windows can decode from MP4/WebM.
_BROWSER_SAFE_CODECS = {"h264", "avc1", "vp8", "vp9", "av01"}
# Container brands the browser demuxers are guaranteed to like after our pass.
_PREVIEW_SUFFIX = ".mp4"

_tool_cache: dict[str, str | None] = {}
_inflight: dict[Path, asyncio.Task] = {}
_download_inflight: dict[Path, asyncio.Task] = {}
_schedule_lock = asyncio.Lock()
# Targets whose conversion failed; retried only after a cooldown so a broken
# source does not trigger an endless encode loop from the background sweep.
_failed_at: dict[Path, float] = {}
_FAIL_RETRY_SECONDS = 600.0


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _tool(name: str) -> str | None:
    if name not in _tool_cache:
        found: str | None = None
        override = os.getenv("H3_FFMPEG_BIN_DIR")
        if override:
            candidate = Path(override) / (name + (".exe" if sys.platform == "win32" else ""))
            if candidate.is_file():
                found = str(candidate)
        if not found:
            found = shutil.which(name)
        _tool_cache[name] = found
    return _tool_cache[name]


def _tools_available() -> bool:
    return bool(_tool("ffprobe") and _tool("ffmpeg"))


def _probe_video_codec(path: Path) -> str | None:
    """Return the first video stream codec name, or None when unreadable."""
    ffprobe = _tool("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        streams = json.loads(result.stdout).get("streams") or []
    except json.JSONDecodeError:
        return None
    codec = streams[0].get("codec_name") if streams else None
    return str(codec) if codec else None


def _probe_audio_codec(path: Path) -> str | None:
    """Return the first audio stream codec, or None for silent/unreadable media."""
    ffprobe = _tool("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        streams = json.loads(result.stdout).get("streams") or []
    except json.JSONDecodeError:
        return None
    codec = streams[0].get("codec_name") if streams else None
    return str(codec) if codec else None


def _cache_target(aweme_id: str) -> Path:
    safe_id = re.sub(r"[^\w-]", "_", str(aweme_id))
    return WEB_CACHE_DIR / f"{safe_id}{_PREVIEW_SUFFIX}"


def download_conversion_target(source: Path) -> Path:
    """Final browser-playable path; non-MP4 downloads become MP4."""
    return source if source.suffix.lower() == ".mp4" else source.with_suffix(".mp4")


def _recently_failed(target: Path) -> bool:
    """True when a conversion for this target failed within the cooldown."""
    failed_at = _failed_at.get(target)
    if failed_at is None:
        return False
    if time.monotonic() - failed_at < _FAIL_RETRY_SECONDS:
        return True
    _failed_at.pop(target, None)
    return False


def _sidecar(target: Path) -> Path:
    return target.with_suffix(".json")


def _cache_matches(target: Path, source: Path) -> bool:
    """True when the cached preview belongs to this exact source revision."""
    if not target.is_file():
        return False
    marker = _sidecar(target)
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    try:
        stat = source.stat()
    except OSError:
        return False
    return (
        payload.get("size") == stat.st_size
        and float(payload.get("mtime") or -1) == stat.st_mtime
        and payload.get("source") == str(source)
    )


def _encode_sync(ffmpeg: str, source: Path, target: Path) -> None:
    """Transcode source into an H.264 MP4 (width capped at 1920)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".part.mp4")
    try:
        if temp.exists():
            temp.unlink()
        command = [
            ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", str(source),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-sn", "-dn",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-tag:v", "avc1",
            "-vf", "scale='min(1920,iw)':-2",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            str(temp),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=_creation_flags(),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise RuntimeError("ffmpeg 转码失败: " + (detail[-1] if detail else f"exit {result.returncode}"))
        if not temp.is_file():
            raise RuntimeError("ffmpeg 未生成预览文件")
        temp.replace(target)
        try:
            stat = source.stat()
            _sidecar(target).write_text(
                json.dumps(
                    {"source": str(source), "size": stat.st_size, "mtime": stat.st_mtime},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # cache still usable; only freshness tracking degrades
    finally:
        if temp.exists():
            temp.unlink()


def _needs_conversion(source: Path) -> bool:
    """Decide (without side effects) whether a preview copy is required."""
    if not source.is_file():
        return False
    codec = _probe_video_codec(source)
    if codec is None:
        return False  # unreadable; serve as-is rather than blocking playback
    container_safe = source.suffix.lower() in {".mp4", ".webm"}
    return not container_safe or codec not in _BROWSER_SAFE_CODECS


def schedule_web_preview(source: Path, aweme_id: str) -> None:
    """Pre-warm the H.264 preview in the background when it is needed.

    Safe to call from any request handler or background loop; no-ops when
    nothing to do or when no running event loop is available (unit tests).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    target = _cache_target(aweme_id)
    if _recently_failed(target) or target in _inflight:
        return
    if _cache_matches(target, source):
        return
    if not _needs_conversion(source):
        return
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        return
    task = loop.create_task(_convert_and_cache(ffmpeg, source, target))
    _inflight[target] = task
    task.add_done_callback(lambda done: _consume(task, target))


def playable_download_path(source: Path) -> Path | None:
    """Return the playable source, or None while an in-place conversion is needed."""
    if not source.is_file():
        return None
    video_codec = _probe_video_codec(source)
    audio_codec = _probe_audio_codec(source)
    if source.suffix.lower() == ".mp4" and video_codec in {"h264", "avc1"} and audio_codec in {None, "aac"}:
        return source
    return None


def _remove_legacy_cache(aweme_id: str) -> None:
    if not aweme_id:
        return
    cached = _cache_target(aweme_id)
    cached.unlink(missing_ok=True)
    _sidecar(cached).unlink(missing_ok=True)


def _promote_cached_preview_sync(source: Path, target: Path, cached: Path) -> None:
    """Move an already verified cached H.264 preview over the download."""
    cached.replace(target)
    if source != target:
        source.unlink(missing_ok=True)
    _sidecar(cached).unlink(missing_ok=True)


def schedule_download_playable(source: Path, aweme_id: str) -> None:
    """Start replacing a completed download with a browser-playable H.264 MP4."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if playable_download_path(source) is not None:
        return
    target = download_conversion_target(source)
    if target in _download_inflight:
        return
    task = loop.create_task(ensure_download_playable(source, aweme_id))
    _download_inflight[target] = task
    task.add_done_callback(lambda done: _consume_download(done, target))


def _consume_download(task: asyncio.Task, target: Path) -> None:
    _download_inflight.pop(target, None)
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        logger.warning("抖音下载视频兼容转换失败: %s", error)


def _consume(task: asyncio.Task, target: Path) -> None:
    _inflight.pop(target, None)


def requires_conversion(source: Path) -> bool:
    """True when ``source`` needs an H.264 copy for browser playback."""
    return _needs_conversion(source)


async def ensure_web_playable_at(source: Path, target: Path) -> Path:
    """Return a browser-playable path, writing the H.264 copy to ``target``.

    Generalised version used by the upload-preview flow: when the source
    already plays in browsers it is returned unchanged; otherwise a one-time
    cached conversion is guaranteed (deduplicated against the Douyin cache).
    """
    if _recently_failed(target):
        return source
    if not _needs_conversion(source):
        return source
    if _cache_matches(target, source):
        return target
    async with _schedule_lock:
        if _cache_matches(target, source):
            return target
        pending = _inflight.get(target)
        if pending is not None:
            if pending.done():
                return pending.result()
            return await pending
        ffmpeg = _tool("ffmpeg")
        if not ffmpeg:
            return source
        task = asyncio.create_task(_convert_and_cache(ffmpeg, source, target))
        _inflight[target] = task
        task.add_done_callback(lambda done: _consume(task, target))
    return await task


async def ensure_web_playable(source: Path, aweme_id: str) -> Path:
    """Douyin variant: cached under data/douyin-web keyed by aweme id."""
    return await ensure_web_playable_at(source, _cache_target(aweme_id))


async def ensure_download_playable(source: Path, aweme_id: str) -> Path:
    """Atomically replace a completed download with H.264/AAC MP4."""
    ready = playable_download_path(source)
    if ready is not None:
        _remove_legacy_cache(aweme_id)
        return ready
    target = download_conversion_target(source)
    pending = _download_inflight.get(target)
    if pending is not None and pending is not asyncio.current_task():
        return await pending
    cached = _cache_target(aweme_id)
    if aweme_id and _cache_matches(cached, source) and playable_download_path(cached) is not None:
        await asyncio.to_thread(_promote_cached_preview_sync, source, target, cached)
        return target
    await asyncio.to_thread(_convert_download_sync, source, target)
    _remove_legacy_cache(aweme_id)
    return target


def _convert_download_sync(source: Path, target: Path) -> None:
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，无法转换下载视频")
    staging = target.with_name(f"{target.stem}.h3-converted.mp4")
    try:
        _encode_sync(ffmpeg, source, staging)
        staging.replace(target)
        if source != target:
            source.unlink(missing_ok=True)
    finally:
        staging.unlink(missing_ok=True)
        _sidecar(staging).unlink(missing_ok=True)


async def _convert_and_cache(ffmpeg: str, source: Path, target: Path) -> Path:
    try:
        await asyncio.to_thread(_encode_sync, ffmpeg, source, target)
    except Exception as exc:  # noqa: BLE001 - fall back to the original file
        logger.warning("抖音预览转码失败，改为直出原文件（%s 内不再重试）: %s", int(_FAIL_RETRY_SECONDS), exc)
        _failed_at[target] = time.monotonic()
        return source
    return target
