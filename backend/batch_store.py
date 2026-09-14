from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from copy import deepcopy
from typing import Any, Callable

from .settings import DB_PATH
from .store import now_iso
from . import batch_ai


TERMINAL_BATCH_STATUSES = {"completed", "failed", "cancelled"}

# 已经取消的步骤：历史条目里残留的这些里程碑在**读取时**剔掉，页面不再显示它们。
# 用户 2026-09-14：「已经没有生成歌词字幕版，可是流程还是存在」——歌词字幕路由已因效果差
# 关闭，批量也去掉了这一步，老条目不能再挂着一个永远不产出的步骤。
RETIRED_MILESTONE_IDS = {"lyrics", "deliver"}


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

    @staticmethod
    def _renumber(state: dict[str, Any]) -> None:
        """把**未删除**的条目按顺序编号成 1..N，已删除的排到末尾不占号。

        页面只显示未删除的条目，编号必须跟着用户看到的队列走：删掉两条旧任务后新加的
        那条应该是「第 1 条」，而不是「第 3 条」（2026-09-10 用户：「怎么有三条啊，
        我只有两个链接啊」）。所有读/写都过这一手，历史批次也会自动纠正。
        """
        number = 0
        items = state.get("items") or []
        for item in items:
            if item.get("status") == "deleted":
                continue
            number += 1
            item["index"] = number
        for item in items:
            if item.get("status") == "deleted":
                number += 1
                item["index"] = number

    @staticmethod
    def _prune_milestones(state: dict[str, Any]) -> None:
        """剔掉已取消步骤的历史里程碑，让页面流程与实际交付一致。

        - `lyrics`：批量已去掉「生成歌词字幕版」这一步；
        - `deliver`：2026-09-13 用户要求「直接去掉这一格」（进度只剩 下载/备料/审核/出片），
          交付仍然照做，只是不再单独占一格进度。
        """
        for item in state.get("items") or []:
            rows = item.get("milestones")
            if not rows:
                continue
            kept = [row for row in rows if row.get("id") not in RETIRED_MILESTONE_IDS]
            if len(kept) != len(rows):
                item["milestones"] = kept

    @staticmethod
    def _backfill_copy_fields(state: dict[str, Any]) -> None:
        """就地补全 / 纠正条目的发布文案（纯本地，不调模型）。

        用户 2026-09-13：官方文本模型 429 打满（要等约 10 小时）时预审降级，
        确认页出现**空简介 + 空标签**（实测 #3 就是 `intro='' tags=[]`）——用户要求
        「流程中简介和标签没有的话自动生成」。放在读取路径上，历史条目也会自愈；
        之后模型（或用户上传图触发的 `write_copy`）给出真文案时会正常覆盖。

        2026-09-15 追加：跳舞条目被写成「翻唱」这类唱歌用词时同样在读取路径自愈
        （用户：「我看你现在的简介或者标题跳舞都会写上翻唱 这是不对的」），所以这里
        **每次都过一遍** `ensure_copy_fields`（纯字符串运算、幂等），不再因为「简介和标签
        都齐了」就跳过。
        """
        for item in state.get("items") or []:
            ai = item.get("ai")
            if not isinstance(ai, dict) or not ai:
                continue
            metadata = item.get("sourceMetadata") or {}
            try:
                batch_ai.ensure_copy_fields(
                    ai,
                    kind=str(item.get("kind") or "singing"),
                    description=str(metadata.get("desc") or ""),
                    source_tags=[str(tag) for tag in metadata.get("tags") or []],
                )
            except Exception:  # noqa: BLE001 - 兜底文案不能影响任何读写
                continue

    @staticmethod
    def _sync_titles(state: dict[str, Any]) -> None:
        """条目标题只认**发布标题** `ai.title`，`item.title` 跟着它走。

        用户 2026-09-13 看到「批量生成任务 4 为什么标题不一致」：左侧队列显示的是
        `item.title`（预审阶段写的），审核面板「标题」与发布文案/发布目录用的是
        `ai.title`（用户上传候选图后 `write_copy` 按图重写过），两个字段各自更新，
        于是同一条出现两个标题。放在读取路径上，历史条目读一次就对齐。
        """
        for item in state.get("items") or []:
            ai = item.get("ai")
            if not isinstance(ai, dict):
                continue
            published = str(ai.get("title") or "").strip()
            if published and str(item.get("title") or "").strip() != published:
                item["title"] = published

    def _normalize(self, state: dict[str, Any]) -> None:
        self._renumber(state)
        self._prune_milestones(state)
        # 先补/纠正文案（跳舞条目的「翻唱」在这里被清掉，可能改 `ai.title`），再同步条目标题，
        # 否则同一条会读出两个标题（`old` 版顺序是先 sync 再 backfill）。
        self._backfill_copy_fields(state)
        self._sync_titles(state)

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
        self._normalize(state)
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
        if not row:
            return None
        state = json.loads(row["state_json"])
        self._normalize(state)
        return state

    def latest(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            # created_at 在 Windows 上的分辨率约 15.6ms：同一毫秒内建的两个批次会拿到相同
            # 时间戳，只按时间排序会随机取到旧的那条；rowid 兜底保证取最新插入的。
            row = connection.execute(
                "SELECT state_json FROM batches ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        state = json.loads(row["state_json"])
        self._normalize(state)
        return state

    def active(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM batches WHERE status IN ('queued', 'running', 'paused', 'awaiting_review', 'failed')"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        state = json.loads(row["state_json"])
        self._normalize(state)
        return state

    def update(self, batch_id: str, **changes: Any) -> dict[str, Any]:
        state = self.get(batch_id)
        if state is None:
            raise KeyError(batch_id)
        state.update(changes)
        state["updatedAt"] = now_iso()
        self._normalize(state)
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
        if active.get("status") == "queued" and not active.get("startedAt"):
            # 只排了队、还没点「启动」的批次：没有任何东西被打断，保持原样等用户启动
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

    def items_for_aweme(self, aweme_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """按作品号找**历史做过的条目**（跨批次，最新的在前）。

        用户 2026-09-13：「如果已经建立的文件 当我复制抖音链接的时候不要在重复建立了直接往下走」
        —— 备料跑完但没出片、或者删了又重加同一条时，源视频 / 出图提示词 / 用户上传的成图 /
        发布文案都还在磁盘上，没必要再下载、再抽帧、再调一次模型。这里给出可复用的候选条目。
        """
        if not aweme_id:
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT state_json FROM batches WHERE state_json LIKE ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (f'%"{aweme_id}"%', limit),
            ).fetchall()
        found: list[dict[str, Any]] = []
        for row in rows:
            try:
                state = json.loads(row["state_json"])
            except (TypeError, ValueError):
                continue
            for item in state.get("items") or []:
                if str(item.get("awemeId") or "") == aweme_id:
                    found.append({**item, "_batchId": state.get("id")})
        return found

    def flagged_for_shutdown(self) -> list[dict[str, Any]]:
        """找**打开了「全部完成后自动关机」**的批次（跨状态查，已完成/失败/暂停都算）。

        关机看护任务每 10 秒扫一次：只看状态会漏掉「刚刚跑完、状态已经变成 completed」的批次，
        所以直接按 state_json 里的标记找（与 `items_for_aweme` 同一套做法）。
        """
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT state_json FROM batches WHERE state_json LIKE ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 20",
                ('%"shutdownOnComplete": true%',),
            ).fetchall()
        states: list[dict[str, Any]] = []
        for row in rows:
            try:
                state = json.loads(row["state_json"])
            except (TypeError, ValueError):
                continue
            self._normalize(state)
            states.append(state)
        return states

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
