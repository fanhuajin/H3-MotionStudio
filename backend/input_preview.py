"""Browser-playable preview for input videos picked in the upload card.

The pipeline reads any codec (HEVC included), but the browser <video> tag
cannot decode HEVC/H.265 — the very files Douyin high-quality downloads
produce.  When the user picks a singing video we store it under
``data/uploads/<id>`` right away and, when the codec is not browser-safe,
transcode an H.264 copy the UI can show while the real file is only sent to
the pipeline at submit time (unchanged).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .douyin_preview import ensure_web_playable_at, requires_conversion
from .settings import DATA_DIR

logger = logging.getLogger("uvicorn.error")

UPLOADS_DIR = DATA_DIR / "uploads"
MAX_AGE_SECONDS = 24 * 3600

_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")

_tasks: dict[str, asyncio.Task] = {}


def _source_path(upload_id: str, suffix: str) -> Path:
    return UPLOADS_DIR / f"{upload_id}{suffix}"


def _target_path(upload_id: str) -> Path:
    return UPLOADS_DIR / f"{upload_id}.web.mp4"


def _meta_path(upload_id: str) -> Path:
    return UPLOADS_DIR / f"{upload_id}.json"


def valid_upload_id(upload_id: str) -> bool:
    return bool(_ID_PATTERN.match(upload_id or ""))


def load_meta(upload_id: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(_meta_path(upload_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_upload(upload_id: str, suffix: str, stream) -> None:
    """Persist the picked file under data/uploads (atomic)."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    source = _source_path(upload_id, suffix)
    part = source.with_name(source.name + ".part")
    try:
        with part.open("wb") as handle:
            shutil.copyfileobj(stream, handle, length=1024 * 1024)
        part.replace(source)
    finally:
        if part.exists():
            part.unlink()
    _meta_path(upload_id).write_text(
        json.dumps(
            {"upload_id": upload_id, "suffix": suffix, "created": time.time()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _spawn_conversion(upload_id: str) -> None:
    meta = load_meta(upload_id)
    if not meta:
        return
    source = _source_path(upload_id, str(meta.get("suffix") or ".mp4"))
    target = _target_path(upload_id)
    if target.exists() or not requires_conversion(source):
        return
    if upload_id in _tasks:
        return
    task = asyncio.create_task(ensure_web_playable_at(source, target))
    _tasks[upload_id] = task
    task.add_done_callback(lambda done: _consume(upload_id, task))


def _consume(upload_id: str, task: asyncio.Task) -> None:
    _tasks.pop(upload_id, None)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.warning("上传预览转码失败，将直出原文件: %s", exc)


async def start_preview(upload_id: str) -> dict[str, Any]:
    """Begin (or join) the preview conversion for a stored upload.

    Returns immediately; the UI polls ``preview_status`` while the H.264
    copy is being made.  Files that already play (H.264 etc.) are served
    as-is with ``converting=False``.
    """
    _spawn_conversion(upload_id)
    return await preview_status(upload_id)


async def preview_status(upload_id: str) -> dict[str, Any]:
    meta = load_meta(upload_id)
    if not meta:
        return {}
    url = f"/api/uploads/{upload_id}/preview"
    target = _target_path(upload_id)
    pending = _tasks.get(upload_id)
    converting = pending is not None and not pending.done()
    if target.exists():
        _tasks.pop(upload_id, None)
        converting = False
    return {"uploadId": upload_id, "url": url, "converting": converting, "ready": not converting}


async def resolve_preview(upload_id: str) -> Path | None:
    """Path to serve for inline preview (conversion happens on demand)."""
    meta = load_meta(upload_id)
    if not meta:
        return None
    source = _source_path(upload_id, str(meta.get("suffix") or ".mp4"))
    if not source.is_file():
        return None
    return await ensure_web_playable_at(source, _target_path(upload_id))


def prune_old_uploads() -> None:
    """Delete uploads older than MAX_AGE_SECONDS (best effort)."""
    if not UPLOADS_DIR.is_dir():
        return
    now = time.time()
    for meta_file in UPLOADS_DIR.glob("*.json"):
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
            created = float(payload.get("created") or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if now - created < MAX_AGE_SECONDS:
            continue
        upload_id = str(payload.get("upload_id") or meta_file.stem)
        for candidate in UPLOADS_DIR.glob(f"{upload_id}*"):
            try:
                candidate.unlink()
            except OSError:
                pass
