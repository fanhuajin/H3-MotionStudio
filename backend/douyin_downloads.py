"""独立下载页的后台任务，沿用批量制作的直连优先策略。"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

from .douyin_mirror import upsert_jobs
from .douyin_preview import ensure_download_playable
from .douyin_service import douyin_service
from .settings import env_value
from .store import now_iso


tasks: dict[str, asyncio.Task] = {}


def submit(url: str) -> dict[str, Any]:
    job = {
        "job_id": f"direct-{uuid.uuid4().hex[:12]}", "url": url,
        "status": "pending", "created_at": now_iso(),
        "total": 1, "success": 0, "failed": 0, "skipped": 0, "error": None,
    }
    upsert_jobs([job])
    task = asyncio.create_task(_run(job))
    tasks[job["job_id"]] = task
    task.add_done_callback(lambda _: tasks.pop(job["job_id"], None))
    return dict(job)


async def _run(job: dict[str, Any]) -> None:
    from . import batch_worker

    job.update(status="running", started_at=now_iso())
    upsert_jobs([job])
    direct_failure = None
    try:
        rest_only = (env_value("H3_DOUYIN_DIRECT") or "").strip().lower() in {"0", "false", "off", "no"}
        if not rest_only:
            try:
                await batch_worker._fetch_source_direct(job)
                job.update(status="success", success=1)
                return
            except Exception as exc:
                direct_failure = exc
        try:
            remote = await douyin_service.submit(job["url"])
            while remote.get("status") in {"pending", "running"}:
                await asyncio.sleep(2)
                remote = await douyin_service.job(remote["job_id"])
            if remote.get("status") not in {"success", "completed"}:
                raise RuntimeError(remote.get("error") or "下载器未成功完成下载")
            result = douyin_service.result_for(remote)
            if not result:
                raise RuntimeError("下载完成但没有找到视频文件")
            source = Path(result["path"])
            reason = await asyncio.to_thread(batch_worker._park_truncated_source, source)
            if reason:
                raise RuntimeError(reason)
            await ensure_download_playable(source, str(result["awemeId"]))
            job.update(status="success", success=1)
        except Exception as exc:
            detail = batch_worker.douyin_download_failure_note(direct_failure, exc)
            detail = detail.replace("在批量里点「重新开始」重试本条", "在下载页重新提交链接")
            job.update(status="failed", failed=1, error=detail)
    except asyncio.CancelledError:
        job.update(status="cancelled", error="项目服务已停止，下载任务已取消")
        raise
    except Exception as exc:
        job.update(status="failed", failed=1, error=str(exc))
    finally:
        job["finished_at"] = now_iso()
        upsert_jobs([job])
