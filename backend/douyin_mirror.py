"""Lightweight persistent mirror of the Douyin downloader service's jobs.

The downloader service keeps its job list only in memory and is meant to be
started on demand and stopped again to free RAM for ComfyUI.  Whenever the
service is reachable we snapshot every job here (JSON under ``data/``); when
the service is offline, read endpoints fall back to this mirror so completed
downloads stay visible and playable (files/previews live on disk, not in the
service process).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .settings import DATA_DIR

MIRROR_PATH = DATA_DIR / "douyin-jobs.json"
MAX_RECORDS = 60


def _load() -> dict[str, dict[str, Any]]:
    """Read the mirror from disk.

    Deliberately uncached: the file can appear or change while this process
    runs (seeding, housekeeping), and it is only a few hundred bytes.
    """
    try:
        payload = json.loads(MIRROR_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _sort_key(record: dict[str, Any]) -> str:
    job = record.get("job") or {}
    created = job.get("created_at") or record.get("updated_at") or ""
    return str(created)


def _save(data: dict[str, dict[str, Any]]) -> None:
    MIRROR_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = MIRROR_PATH.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, MIRROR_PATH)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


def upsert_jobs(jobs: list[dict[str, Any]]) -> None:
    """Snapshot raw service jobs into the mirror (by job_id)."""
    if not jobs:
        return
    data = _load()
    changed = False
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        if data.get(job_id, {}).get("job") != job:
            data[job_id] = {"job": job, "updated_at": time.time()}
            changed = True
    if changed:
        while len(data) > MAX_RECORDS:
            oldest = min(data, key=lambda key: _sort_key(data[key]))
            data.pop(oldest, None)
        _save(data)


def get_job(job_id: str) -> dict[str, Any] | None:
    record = _load().get(job_id)
    return record.get("job") if record else None


def all_jobs() -> list[dict[str, Any]]:
    records = sorted(_load().values(), key=_sort_key, reverse=True)
    return [record.get("job") or {} for record in records if record.get("job")]
