"""在批量 runner 之外，单独把某一条排队中的条目备料好。

背景（2026-09-13 用户实测）：批量 runner 是严格单链路的，`run_batch` 一次只 await
一条。它正卡在某一条的**出片**（视频生成，动辄十几分钟）上时，用户新追加的条目会
一直停在「排队中」，拿不到「下载抖音视频 → 生成人物图与发布文案 → 等你确认」这三步。
而备料本身只需要下载 + ffmpeg 抽帧 + 一次文本模型调用，**不需要 ComfyUI**，所以可以
在不重启后端（重启会打断正在跑的出片任务）的前提下另起一个进程补齐。

安全前提：runner 每约 3 秒会把**整份**批次状态写回 SQLite（读整份 → 改 → 写整份）。
本脚本用 compare-and-swap（`UPDATE ... WHERE state_json = 我读到的那一份`）写状态，
冲突就重新读取、重新计算，所以既不会覆盖 runner 的并发写，也不会被 runner 覆盖
（普通 `BatchStore.update` 会把对方的写入悄悄吃掉，比如用户刚点的「跳过」）。

用法：
    python scripts/batch_prep_item.py --batch <batch_id> --index 3
    python scripts/batch_prep_item.py --batch <batch_id> --item <item_id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import batch_store as batch_store_module  # noqa: E402
from backend import batch_worker  # noqa: E402
from backend.store import now_iso  # noqa: E402


CAS_ATTEMPTS = 400
CAS_SLEEP_SECONDS = 0.05


class CasBatchStore(batch_store_module.BatchStore):
    """把「读整份 → 改 → 写整份」换成 SQLite 上的 compare-and-swap。

    `BatchStore` 的锁是**进程内**的，挡不住另一个进程并发写同一份 state_json；
    这里在 `WHERE state_json = <读到时的那一份>` 上做条件更新，rowcount=0 就说明
    runner 在中间写过，重新读一次再改，避免丢更新。
    """

    def _raw_state(self, batch_id: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
        return None if row is None else str(row["state_json"])

    def _write_if_unchanged(self, batch_id: str, raw_before: str, state: dict[str, Any]) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE batches SET updated_at = ?, status = ?, state_json = ?"
                " WHERE id = ? AND state_json = ?",
                (
                    state["updatedAt"],
                    state["status"],
                    json.dumps(state, ensure_ascii=False),
                    batch_id,
                    raw_before,
                ),
            )
            return cursor.rowcount == 1

    def _cas(self, batch_id: str, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        for _ in range(CAS_ATTEMPTS):
            raw = self._raw_state(batch_id)
            if raw is None:
                raise KeyError(batch_id)
            state = json.loads(raw)
            mutator(state)
            state["updatedAt"] = now_iso()
            self._normalize(state)
            if self._write_if_unchanged(batch_id, raw, state):
                return deepcopy(state)
            time.sleep(CAS_SLEEP_SECONDS)
        raise RuntimeError("批次状态一直被 runner 抢写，本次写入没有落地")

    def update(self, batch_id: str, **changes: Any) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> None:
            state.update(changes)

        return self._cas(batch_id, apply)

    def mutate(self, batch_id: str, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        return self._cas(batch_id, mutator)


def install_cas_store() -> CasBatchStore:
    store = CasBatchStore()
    # batch_worker 里所有写入都走模块级名字 batch_store，换掉它即可整体切到 CAS。
    batch_worker.batch_store = store
    return store


def find_item(state: dict[str, Any], *, item_id: str | None, index: int | None) -> dict[str, Any] | None:
    for item in state.get("items") or []:
        if item_id and item.get("id") == item_id:
            return item
        if index is not None and int(item.get("index") or 0) == index:
            return item
    return None


def reconcile_batch_status(store: CasBatchStore, batch_id: str) -> None:
    """runner 可能在备料期间就收尾了：把停在 completed 的批次拉回「等待确认」。

    否则页面上会出现「批次已完成、但这一条在等你上传图片」的矛盾状态。
    """

    def apply(state: dict[str, Any]) -> None:
        waiting = [row for row in state.get("items") or [] if row.get("status") == "awaiting_review"]
        if not waiting or state.get("status") != "completed":
            return
        state.update(
            status="awaiting_review",
            stage="review",
            currentItemId=waiting[0]["id"],
            runnerActive=False,
            finishedAt=None,
            notice=f"{len(waiting)} 条素材已备齐，等你确认出片。",
        )

    store.mutate(batch_id, apply)


async def prepare_one(batch_id: str, item_id: str) -> int:
    item = batch_worker._item(batch_id, item_id)
    status = str(item.get("status") or "")
    if status not in {"pending", "revising"}:
        print(f"这一条现在是 {status}，不需要备料：#{item.get('index')} {item.get('title')}")
        return 0
    if item.get("skipRequested") or item.get("deleteRequested"):
        print("这一条已被标记跳过/删除，不做备料。")
        return 1

    print(f"开始备料：# {item.get('index')} [{item.get('kind')}] {item.get('url')}")
    # 先落一个「正在备料」的状态：runner 只认 pending/revising/confirmed，
    # 这一步能让它在毫秒级窗口里不会也把同一条捡起来重复做一遍。
    batch_worker._set_item(batch_id, item_id, status="running", stage="download", error=None)
    try:
        await batch_worker._download(batch_id, item_id)
        current = batch_worker._item(batch_id, item_id)
        if current.get("skipRequested") or current.get("deleteRequested"):
            print("备料期间这一条被跳过/删除，停止。")
            return 1
        await batch_worker._prepare_review(
            batch_id,
            item_id,
            feedback=str(current.get("revisionFeedback") or ""),
            mode=str(current.get("revisionMode") or "both"),
        )
    except batch_worker.DuplicateItem as duplicate:
        message = str(duplicate)
        current = batch_worker._item(batch_id, item_id)
        batch_worker._set_item(
            batch_id,
            item_id,
            status="skipped",
            stage="skipped",
            finishedAt=now_iso(),
            childJob=None,
            warning=message,
        )
        for milestone in current.get("milestones") or []:
            if milestone.get("status") in {"pending", "running"}:
                batch_worker.batch_store.set_item_milestone(
                    batch_id, item_id, milestone["id"], status="skipped"
                )
        batch_worker.batch_store.add_item_log(batch_id, item_id, message)
        print(f"重复作品，已跳过：{message}")
        return 0
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 与 runner 一致：单条失败只标黄/标错，不装死
        message = str(error)
        batch_worker._set_item(batch_id, item_id, status="failed", stage="failed", error=message, childJob=None)
        active = next(
            (
                row
                for row in batch_worker._item(batch_id, item_id).get("milestones") or []
                if row.get("status") == "running"
            ),
            None,
        )
        if active:
            batch_worker.batch_store.set_item_milestone(
                batch_id, item_id, active["id"], status="error", currentNode=message
            )
        batch_worker.batch_store.add_item_log(batch_id, item_id, f"备料失败：{message}")
        print(f"备料失败：{message}")
        return 1

    final = batch_worker._item(batch_id, item_id)
    print(
        "备料完成：状态 {status}，标题《{title}》，人物图 {image}".format(
            status=final.get("status"),
            title=final.get("title"),
            image=(final.get("ai") or {}).get("reference_image_path") or "（等你上传 GPT 生成的图）",
        )
    )
    return 0


async def main_async(args: argparse.Namespace) -> int:
    store = install_cas_store()
    state = store.get(args.batch)
    if not state:
        print(f"找不到批次 {args.batch}")
        return 2
    item = find_item(state, item_id=args.item, index=args.index)
    if not item:
        print("批次里找不到这一条")
        return 2
    if state.get("status") == "cancelled":
        print("批次已取消，不备料。")
        return 2
    code = await prepare_one(args.batch, str(item["id"]))
    reconcile_batch_status(store, args.batch)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="在 runner 之外单独备料一个排队中的批量条目")
    parser.add_argument("--batch", required=True, help="批次 id")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--item", help="条目 id")
    group.add_argument("--index", type=int, help="条目编号（页面上的第 N 条）")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
