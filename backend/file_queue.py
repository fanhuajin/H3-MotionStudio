"""本地多选视频共用的串行执行与失败隔离。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .store import now_iso


async def run_file_queue(
    job_ids: list[str], store: Any, runner: Callable[[str], Awaitable[None]],
) -> None:
    for job_id in job_ids:
        state = store.get(job_id)
        if not state or state.get("status") != "queued":
            continue
        try:
            await runner(job_id)
        except asyncio.CancelledError:
            # 服务退出时停止整队，重启沿用现有 interrupted 恢复规则。
            raise
        except Exception as error:
            store.update(job_id, status="failed", errorSummary=str(error), finishedAt=now_iso())
