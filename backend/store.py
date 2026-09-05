from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .settings import DB_PATH, RVC_MODEL


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_elapsed(started_at: str, finished_at: str) -> str:
    started = datetime.fromisoformat(started_at)
    finished = datetime.fromisoformat(finished_at)
    total = max(0, int((finished - started).total_seconds()))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def initial_milestones() -> list[dict[str, Any]]:
    # 歌曲生成：原版成片 →（关闭 ComfyUI）→ RVC 音色 → 输出；二采放大已移至独立路由
    return [
        {"id": "input", "label": "读取视频与音频", "subtitle": "加载输入视频，分离音频轨道", "status": "pending"},
        {"id": "h3", "label": "H3 分段生成", "subtitle": "按时长生成连续唱歌片段", "status": "pending"},
        {"id": "stitch", "label": "防闪拼接", "subtitle": "平滑衔接并裁切到输入时长", "status": "pending"},
        {"id": "handoff", "label": "关闭 ComfyUI", "subtitle": "释放内存和显存，切换到 RVC", "status": "pending"},
        {"id": "stems", "label": "分离人声与伴奏", "subtitle": "Demucs 提取演唱人声", "status": "pending"},
        {"id": "voice", "label": f"转换为 {RVC_MODEL.stem} 音色", "subtitle": "RVC 模型执行音色转换", "status": "pending"},
        {"id": "mux", "label": "替换最终成片音频", "subtitle": "重新混音并封装最终 MP4", "status": "pending"},
    ]


def upscale_milestones() -> list[dict[str, Any]]:
    """二采放大路由里程碑：放大 → 收 1080p 档输出。"""
    return [
        {"id": "upscale", "label": "RealESRGAN 逐帧放大", "subtitle": "8 帧分批超采样", "status": "pending"},
        {"id": "hd", "label": "收 1080p 档输出", "subtitle": "缩放并封装输出视频", "status": "pending"},
    ]


def migrate_milestones(remove_subtitles: bool, mode: str, ratio: str = "4:3") -> list[dict[str, Any]]:
    """动作迁移路由的里程碑模板（不含 RVC：输出保留原音频；不做二采放大）。

    链路：可选「去字幕-ProPainter」→ SCAIL-2 长视频分段 动作迁移/人物替换。
    需要高清时用独立的「二采放大」路由处理。id 与 pipeline 内阶段一一对应。
    """
    del ratio  # 比例只影响画布参数，里程碑不再区分倍数
    transfer = "人物替换" if mode == "replacement" else "动作迁移"
    milestones: list[dict[str, Any]] = []
    if remove_subtitles:
        milestones += [
            {"id": "read", "label": "读取视频与音频", "subtitle": "加载带字幕视频", "status": "pending"},
            {"id": "mask", "label": "定位底部字幕区域", "subtitle": "固定底部字幕遮罩", "status": "pending"},
            {"id": "paint", "label": "ProPainter 时序去字幕", "subtitle": "按前后帧修复字幕区域", "status": "pending"},
            {"id": "clean_save", "label": "输出无字幕视频", "subtitle": "保留原音频与帧率", "status": "pending"},
        ]
    milestones += [
        {"id": "prep", "label": "加载 SCAIL 模型与人物参考", "subtitle": "读取驱动视频并准备参考人物", "status": "pending"},
        {"id": "sam", "label": "SAM3 人物追踪与遮罩", "subtitle": "定位视频与参考图中的人物", "status": "pending"},
        {
            "id": "migrate",
            "label": f"长视频分段{transfer}",
            "subtitle": (
                "让参考人物按视频动作表演，保留人物形象与音频"
                if mode != "replacement"
                else "把视频中的人物替换成参考人物，保留场景与音频"
            ),
            "status": "pending",
        },
        {"id": "save", "label": "拼接输出成片", "subtitle": "逐段衔接并封装输出视频", "status": "pending"},
    ]
    return milestones


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

    def latest(self, kind: str | None = None) -> dict[str, Any] | None:
        """Latest job, optionally restricted to one task kind ('singing'/'migrate').

        Jobs created before kinds existed carry no kind and count as 'singing'.
        """
        with self._lock, self._connect() as connection:
            if kind is None:
                row = connection.execute(
                    "SELECT state_json FROM jobs ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            elif kind == "singing":
                row = connection.execute(
                    """
                    SELECT state_json FROM jobs
                    WHERE json_extract(state_json, '$.kind') IS NULL
                       OR json_extract(state_json, '$.kind') = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (kind,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT state_json FROM jobs
                    WHERE json_extract(state_json, '$.kind') = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (kind,),
                ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def active(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def recent(self, limit: int = 8) -> list[dict[str, Any]]:
        """最近任务（按创建时间倒序），供“从最近任务选”使用。"""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT state_json FROM jobs ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [json.loads(row["state_json"]) for row in rows]

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
                    next_status = changes.get("status")
                    timestamp = now_iso()
                    if next_status == "running" and item.get("status") != "running":
                        item["startedAt"] = timestamp
                        item.pop("finishedAt", None)
                        item.pop("elapsed", None)
                    elif next_status in {"completed", "error", "skipped"} and item.get("startedAt"):
                        item["finishedAt"] = timestamp
                        changes.setdefault("elapsed", format_elapsed(item["startedAt"], timestamp))
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
