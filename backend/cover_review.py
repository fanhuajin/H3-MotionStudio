"""封面专用手部检查：按图片内容缓存，检查失败保守避手，不自动重生成。"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any
from . import batch_ai

_LOCKS: dict[str, asyncio.Lock] = {}
VERSION = 3


@lru_cache(maxsize=2048)
def _digest_for_stat(path: str, modified: int, size: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(image: Path) -> str:
    stat = image.stat()
    return _digest_for_stat(str(image.resolve()), stat.st_mtime_ns, stat.st_size)


def read_report(image: Path, report: Path) -> dict[str, Any] | None:
    try:
        result = json.loads(report.read_text(encoding="utf-8"))
        digest = fingerprint(image)
        if (result.get("version") == VERSION and result.get("sha256") == digest
                and result.get("status") in {"reasonable", "no_hands", "unreasonable", "uncertain"}
                and result.get("fingersCutByFrame") in {"yes", "no", "uncertain"}
                and result.get("handsNearOrOutsideFrame") in {"yes", "no", "uncertain"}):
            return result
    except (OSError, ValueError, AttributeError):
        pass
    return None


def write_report(report: Path, result: dict[str, Any]) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=report.parent, suffix=".tmp", delete=False) as temp:
        staging = Path(temp.name)
    try:
        staging.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(staging, report)
    finally:
        staging.unlink(missing_ok=True)


async def review(image: Path, report: Path) -> dict[str, Any]:
    key = str(report.resolve()).lower()
    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        cached = read_report(image, report)
        if cached is not None:
            return cached
        digest = fingerprint(image)
        try:
            result = await batch_ai.judge_cover_hands(image)
            if (result.get("status") not in {"reasonable", "no_hands", "unreasonable", "uncertain"}
                    or result.get("fingersCutByFrame") not in {"yes", "no", "uncertain"}
                    or result.get("handsNearOrOutsideFrame") not in {"yes", "no", "uncertain"}):
                raise ValueError("无效检查结果")
        except Exception as exc:
            result = {"status": "uncertain", "fingersCutByFrame": "uncertain", "handsNearOrOutsideFrame": "uncertain", "reason": f"检查未完成：{str(exc)[:160]}"}
        result = {**result, "sha256": digest, "version": VERSION}
        write_report(report, result)
        return result


def hide_hands(report: dict[str, Any] | None) -> bool:
    return (not report or report.get("status") not in {"reasonable", "no_hands"}
            or report.get("fingersCutByFrame") != "no"
            or report.get("handsNearOrOutsideFrame") != "no")
