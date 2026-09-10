from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from copy import deepcopy
from typing import Any, Callable

from .settings import DB_PATH
from .store import now_iso


TERMINAL_BATCH_STATUSES = {"completed", "failed", "cancelled"}


class BatchStore:
    """Persistent state for page-owned, strictly serial production batches."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(DB_PATH, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL
                )
                """
            )

    def create(self, state: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO batches (id, created_at, updated_at, status, state_json) VALUES (?, ?, ?, ?, ?)",
                (
                    state["id"],
                    state["createdAt"],
                    state["updatedAt"],
                    state["status"],
                    json.dumps(state, ensure_ascii=False),
                ),
            )
        return deepcopy(state)

    def get(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def latest(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM batches ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def active(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM batches WHERE status IN ('queued', 'running', 'paused', 'awaiting_review', 'failed') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def update(self, batch_id: str, **changes: Any) -> dict[str, Any]:
        state = self.get(batch_id)
        if state is None:
            raise KeyError(batch_id)
        state.update(changes)
        state["updatedAt"] = now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE batches SET updated_at = ?, status = ?, state_json = ? WHERE id = ?",
                (
                    state["updatedAt"],
                    state["status"],
                    json.dumps(state, ensure_ascii=False),
                    batch_id,
                ),
            )
        self._publish(batch_id, state)
        return deepcopy(state)

    def mutate(
        self, batch_id: str, mutator: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any]:
        state = self.get(batch_id)
        if state is None:
            raise KeyError(batch_id)
        mutator(state)
        return self.update(batch_id, **state)

    def mutate_item(
        self,
        batch_id: str,
        item_id: str,
        mutator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> None:
            for item in state.get("items") or []:
                if item.get("id") == item_id:
                    mutator(item)
                    break
            state["completedCount"] = sum(
                1
                for item in state.get("items") or []
                if item.get("status") in {"completed", "skipped"}
            )
            state["deletedCount"] = sum(
                1 for item in state.get("items") or [] if item.get("status") == "deleted"
            )

        return self.mutate(batch_id, apply)

    def set_item_milestone(
        self,
        batch_id: str,
        item_id: str,
        milestone_id: str,
        **changes: Any,
    ) -> dict[str, Any]:
        def apply(item: dict[str, Any]) -> None:
            for milestone in item.get("milestones") or []:
                if milestone.get("id") != milestone_id:
                    continue
                next_status = changes.get("status")
                if next_status == "running" and milestone.get("status") != "running":
                    milestone["startedAt"] = now_iso()
                if next_status in {"completed", "error", "skipped"}:
                    milestone["finishedAt"] = now_iso()
                milestone.update(changes)
                return

        return self.mutate_item(batch_id, item_id, apply)

    def add_item_log(self, batch_id: str, item_id: str, message: str) -> dict[str, Any]:
        message = message.rstrip()
        if not message:
            return self.get(batch_id) or {}

        def apply(item: dict[str, Any]) -> None:
            logs = list(item.get("logs") or [])
            logs.append({"time": now_iso(), "message": message})
            item["logs"] = logs[-200:]

        return self.mutate_item(batch_id, item_id, apply)

    def interrupt_active(self) -> None:
        active = self.active()
        if not active or active.get("status") in {"awaiting_review", "failed"}:
            return
        current_id = active.get("currentItemId")
        current = next(
            (item for item in active.get("items") or [] if item.get("id") == current_id),
            None,
        )
        interrupted = bool(
            current
            and current.get("status") in {"running", "revising", "confirmed"}
            and active.get("runnerActive")
        )
        if interrupted:
            message = "本地服务在当前步骤运行时重启；进度已保留，请点击重试。"

            def fail_item(item: dict[str, Any]) -> None:
                item.update(status="failed", stage="failed", error=message, childJob=None)
                for milestone in item.get("milestones") or []:
                    if milestone.get("status") == "running":
                        milestone.update(status="error", currentNode=message, finishedAt=now_iso())

            self.mutate_item(active["id"], str(current_id), fail_item)
            self.update(
                active["id"],
                status="failed",
                stage="failed",
                pauseRequested=False,
                runnerActive=False,
                notice=message,
            )
            return
        self.update(
            active["id"],
            status="paused",
            pauseRequested=True,
            runnerActive=False,
            notice="本地服务重启，批次已安全暂停；点击继续后从当前条目恢复。",
        )

    def subscribe(self, batch_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._subscribers.setdefault(batch_id, set()).add(queue)
        return queue

    def unsubscribe(self, batch_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(batch_id)
        if subscribers:
            subscribers.discard(queue)

    def _publish(self, batch_id: str, state: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(batch_id, set())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(deepcopy(state))
            except asyncio.QueueFull:
                pass


batch_store = BatchStore()
