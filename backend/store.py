from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .settings import DB_PATH


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_milestones() -> list[dict[str, Any]]:
    return [
        {"id": "input", "label": "读取视频与音频", "subtitle": "加载输入视频，分离音频轨道", "status": "pending"},
        {"id": "h3", "label": "H3 分段生成", "subtitle": "按时长生成连续唱歌片段", "status": "pending"},
        {"id": "stitch", "label": "防闪拼接", "subtitle": "平滑衔接并裁切到输入时长", "status": "pending"},
        {"id": "upscale", "label": "RealESRGAN 4× 放大", "subtitle": "逐帧超采样并缩放到 1080P", "status": "pending"},
        {"id": "hd", "label": "输出 1440 × 1080", "subtitle": "保存高清加强成片", "status": "pending"},
        {"id": "handoff", "label": "关闭 ComfyUI", "subtitle": "释放内存和显存，切换到 RVC", "status": "pending"},
        {"id": "stems", "label": "分离人声与伴奏", "subtitle": "Demucs 提取演唱人声", "status": "pending"},
        {"id": "voice", "label": "转换为我的音色", "subtitle": "RVC 模型执行音色转换", "status": "pending"},
        {"id": "mux", "label": "替换最终成片音频", "subtitle": "重新混音并封装最终 MP4", "status": "pending"},
    ]


class JobStore:
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
                CREATE TABLE IF NOT EXISTS jobs (
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
                "INSERT INTO jobs (id, created_at, updated_at, status, state_json) VALUES (?, ?, ?, ?, ?)",
                (state["id"], state["createdAt"], state["updatedAt"], state["status"], json.dumps(state, ensure_ascii=False)),
            )
        return deepcopy(state)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT state_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return json.loads(row["state_json"]) if row else None

    def latest(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT state_json FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        return json.loads(row["state_json"]) if row else None

    def active(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        state = self.get(job_id)
        if state is None:
            raise KeyError(job_id)
        state.update(changes)
        state["updatedAt"] = now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET updated_at = ?, status = ?, state_json = ? WHERE id = ?",
                (state["updatedAt"], state["status"], json.dumps(state, ensure_ascii=False), job_id),
            )
        self._publish(job_id, state)
        return deepcopy(state)

    def mutate(self, job_id: str, mutator) -> dict[str, Any]:
        state = self.get(job_id)
        if state is None:
            raise KeyError(job_id)
        mutator(state)
        return self.update(job_id, **state)

    def add_log(self, job_id: str, message: str) -> dict[str, Any]:
        message = message.rstrip()
        if not message:
            return self.get(job_id) or {}

        def apply(state: dict[str, Any]) -> None:
            logs = list(state.get("logs") or [])
            logs.append({"time": now_iso(), "message": message})
            state["logs"] = logs[-300:]

        return self.mutate(job_id, apply)

    def set_milestone(self, job_id: str, milestone_id: str, **changes: Any) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> None:
            for item in state["milestones"]:
                if item["id"] == milestone_id:
                    item.update(changes)
                    return

        return self.mutate(job_id, apply)

    def subscribe(self, job_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._subscribers.setdefault(job_id, set()).add(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(job_id)
        if subscribers:
            subscribers.discard(queue)

    def _publish(self, job_id: str, state: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(job_id, set())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(deepcopy(state))
            except asyncio.QueueFull:
                pass


store = JobStore()

