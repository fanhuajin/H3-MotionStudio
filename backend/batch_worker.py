from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import batch_ai, batch_image, batch_portrait
from .batch_store import batch_store
from .douyin_mirror import upsert_jobs as mirror_upsert
from .douyin_preview import ensure_download_playable
from .douyin_service import (
    DOUYIN_OUTPUT,
    DouyinServiceError,
    _extract_aweme_id as extract_aweme_id,
    douyin_service,
    is_douyin_url,
)
from .settings import (
    BATCH_OUTPUT_ROOT,
    BATCH_RATIO_CHOICES,
    BATCH_SELF_URL,
    DATA_DIR,
    batch_default_ratio,
    env_value,
    normalize_batch_ratio,
)
from .store import now_iso, store


logger = logging.getLogger("uvicorn.error")


IDENTITY_PATH = Path(r"E:\AI_Assets\PortraitIdentity\本人固定参考.png")
MANIFEST_PATH = Path(r"D:\EV\download_manifest.jsonl")
COVER_FONT = Path(r"C:\Windows\Fonts\msyhbd.ttc")
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
_RUNNING_BATCHES: set[str] = set()
# 每个条目当前在跑的预审协程（模型调用 + 本地出图），跳过/删除时据此安全取消。
_ITEM_TASKS: dict[str, asyncio.Task] = {}
# 跳过/取消后仍在后台跑的子任务：等它落定再把成片补进发布目录（避免任务被垃圾回收）。
_SALVAGE_TASKS: set[asyncio.Task] = set()
# 正在后台出片的条目任务。runner 不再原地 await 出片，而是把出片放进这里的任务，
# 自己继续给后面的条目备料；这个集合是**强引用**（否则任务可能在跑的过程中被回收）。
_VIDEO_TASKS: set[asyncio.Task] = set()
# 后台自动生成封面的任务（强引用）。封面生成走 ChatGPT 桌面端，由 _IMAGE_LOCK 保证单链。
_COVER_TASKS: set[asyncio.Task] = set()


def cancel_item_work(batch_id: str, item_id: str) -> bool:
    task = _ITEM_TASKS.get(f"{batch_id}:{item_id}")
    if not task or task.done():
        return False
    task.cancel()
    return True


def item_milestones(kind: str) -> list[dict[str, Any]]:
    """批量条目的里程碑。

    **不再有「生成歌词字幕版」这一步**（2026-09-14 用户确认）：歌词字幕路由已因效果差
    隐藏，批量也一并去掉这一步，只交付最终成片（无字幕）+ 发布文案 + 人物图，避免
    流程里挂着一个永远不产出的步骤（用户原话：「已经没有生成歌词字幕版，可是流程还是存在」）。
    **也不再有「整理发布文件」这一格**（2026-09-13 用户：「直接去掉这一格」）：交付照做，
    但进度只保留 下载 → 备料 → 审核 → 出片 四步。
    `kind` 只影响日志文案，里程碑本身两类一致。
    """
    del kind
    return [
        {"id": "download", "label": "下载抖音视频", "subtitle": "获取源视频与原作品文案", "status": "pending"},
        {"id": "prepare", "label": "生成人物图与发布文案", "subtitle": "本地分析画面并生成候选结果", "status": "pending"},
        {"id": "review", "label": "等待你的确认", "subtitle": "查看图片、标题、简介和标签", "status": "pending"},
        {"id": "video", "label": "生成最终视频", "subtitle": "复用工作台真实节点与单链路进度", "status": "pending"},
    ]


def unique_urls(values: list[str]) -> list[str]:
    """同一次提交里按**归一化链接**去重，保留用户粘贴时的原始写法。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        url = str(value or "").strip()
        key = url_key(url)
        if url and key not in seen:
            seen.add(key)
            result.append(url)
    return result


# 链接判重时忽略的**跟踪参数**（只丢这些；不认识的参数一律保留，避免把作品号丢掉）。
_TRACKING_QUERY_PREFIXES = ("utm_", "share_", "track_")
_TRACKING_QUERY_NAMES = {
    "_d", "a_bogus", "aid", "checksum", "enter_from", "extra_params", "fp", "from",
    "from_source", "from_ssr", "from_tab_name", "gd_label", "iid", "is_from_webapp",
    "log_id", "mid", "mstoken", "previous_page", "region", "schema_type", "sender_device",
    "share_token", "show_tab", "showtab", "timestamp", "titletype", "tt_from", "u_code",
    "verifyfp", "video_share_track_ver", "web_id", "with_sec_did", "x-bogus",
}


def url_key(url: str) -> str:
    """链接指纹：忽略大小写、结尾斜杠与**跟踪参数**，但保留作品号等有意义的参数。

    坑（2026-09-11 用户实测「点击准备任务没有效果」）：抖音「喜欢列表」的链接是
    `www.douyin.com/user/self?from_tab_name=main&modal_id=<作品号>&showTab=like` ——
    作品号在 **query** 里。早先的实现把整段 query 丢掉，导致所有这类链接都变成
    `www.douyin.com/user/self`，两条不同视频被误判成重复链接过滤掉，点了没有任何新增。
    现在只丢已知跟踪参数，`modal_id` / `vid` / `item_id` 之类全部保留。
    """
    text = str(url or "").strip().lower()
    if not text:
        return ""
    text = text.split("#", 1)[0]
    base, separator, query = text.partition("?")
    base = base.rstrip("/")
    if not separator or not query:
        return base
    kept: list[str] = []
    for chunk in query.split("&"):
        if not chunk:
            continue
        name, _, value = chunk.partition("=")
        if not name or name in _TRACKING_QUERY_NAMES or name.startswith(_TRACKING_QUERY_PREFIXES):
            continue
        kept.append(f"{name}={value}")
    if not kept:
        return base
    return f"{base}?{'&'.join(sorted(kept))}"


def item_key(kind: str, url: str) -> str:
    """批次内判重用的键：类型 + 链接指纹（同一段素材当歌曲和当跳舞是两件事，不能互相吃掉）。"""
    return f"{kind}:{url_key(url)}"


# 一个批次（含随时追加）最多多少条视频
MAX_BATCH_ITEMS = 50

# 能「重新开始」的条目状态：只有已经跑完一轮的（失败 / 已跳过 / 已出片）。
# 正在出片的必须先「取消」，已删除的不能复活，还没出片的本来就会跑、不需要重开。
RETRYABLE_ITEM_STATUSES = {"failed", "skipped", "completed"}


def new_item(kind: str, url: str, ratio: str, index: int, created: str) -> dict[str, Any]:
    """新建一个批次条目。新建批次与「随时追加」共用，保证字段一致。"""
    return {
        "id": uuid.uuid4().hex[:12],
        "index": index,
        "kind": kind,
        "url": url,
        "ratio": ratio,
        "status": "pending",
        "stage": "queued",
        "createdAt": created,
        "updatedAt": created,
        "title": "等待处理",
        "milestones": item_milestones(kind),
        "logs": [{"time": created, "message": "已加入队列（等「开跑」后才开始）"}],
        "revision": 0,
        "reviewApproved": False,
        "childJob": None,
        "outputs": {},
        "error": None,
        "warning": None,
    }


def _build_items(
    rows: list[tuple[str, str]],
    defaults: dict[str, str],
    start_index: int,
    created: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    index = start_index
    for kind, url in rows:
        index += 1
        items.append(new_item(kind, url, defaults[kind], index, created))
    return items


def new_batch_state(
    singing_urls: list[str],
    dance_urls: list[str],
    singing_ratio: str | None = None,
    dance_ratio: str | None = None,
    shutdown_on_complete: bool = False,
) -> dict[str, Any]:
    """新建批次。比例是**条目级**字段：歌曲默认 4:3、跳舞默认 9:16，用户在页面里逐条可改。

    `singing_ratio` / `dance_ratio` 只是建批次时给整组链接的默认值（页面上的分组选择），
    之后每条都独立保存自己的 `ratio`，运行时以条目自己的值为准。

    `shutdown_on_complete`：整批跑完后是否自动关机，**默认关闭**（2026-09-15 用户要求）。
    """
    created = now_iso()
    batch_id = uuid.uuid4().hex
    defaults = {
        "singing": normalize_batch_ratio(singing_ratio, "singing"),
        "dance": normalize_batch_ratio(dance_ratio, "dance"),
    }
    rows = [("singing", url) for url in unique_urls(singing_urls)] + [
        ("dance", url) for url in unique_urls(dance_urls)
    ]
    items = _build_items(rows, defaults, 0, created)
    return {
        "id": batch_id,
        "status": "queued",
        "stage": "queued",
        "createdAt": created,
        "updatedAt": created,
        "startedAt": None,
        "finishedAt": None,
        "total": len(items),
        "completedCount": 0,
        "deletedCount": 0,
        "currentIndex": 0,
        "currentItemId": items[0]["id"] if items else None,
        "pauseRequested": False,
        "runnerActive": False,
        # 整批跑完后是否自动关机：**默认关闭**，用户在批量页的开关里打开（2026-09-15）
        "shutdownOnComplete": bool(shutdown_on_complete),
        "notice": "任务已创建，准备处理第 1 条。" if items else "没有任务。",
        "items": items,
    }


def append_batch_items(
    batch_id: str,
    singing_urls: list[str],
    dance_urls: list[str],
    singing_ratio: str | None = None,
    dance_ratio: str | None = None,
) -> dict[str, Any]:
    """给已有批次追加条目：批次随时能加，新条目排在队尾（编号接着往下排）。

    用户 2026-09-10：「可以让我随时添加新的任务，删除单条任务」「重复的记得过滤掉」。
    重复链接按「类型 + 链接指纹」（忽略大小写/结尾斜杠/查询串）比对已有条目后直接跳过，
    只把明确不要了的（已删除 / 已跳过）排除在判重之外，返回跳过的条数给页面提示，
    避免同一条视频被做两遍。
    """
    state = batch_store.get(batch_id)
    if not state:
        raise KeyError(batch_id)
    defaults = {
        "singing": normalize_batch_ratio(singing_ratio, "singing"),
        "dance": normalize_batch_ratio(dance_ratio, "dance"),
    }
    singing_clean = unique_urls(singing_urls)
    dance_clean = unique_urls(dance_urls)
    rows = [("singing", url) for url in singing_clean] + [("dance", url) for url in dance_clean]
    if not rows:
        raise ValueError("请至少填写一条抖音链接")
    # 同一次粘贴里就重复的（含写法不同）也要算进「已过滤的重复」，否则用户看不到它被吃掉了
    raw_count = sum(1 for url in list(singing_urls) + list(dance_urls) if str(url or "").strip())
    created = now_iso()
    result = {"added": 0, "duplicates": max(0, raw_count - len(rows))}

    def apply(row: dict[str, Any]) -> None:
        items = list(row.get("items") or [])
        known = {
            item_key(str(item.get("kind") or ""), str(item.get("url") or ""))
            for item in items
            if item.get("status") not in {"deleted", "skipped"}
        }
        fresh: list[tuple[str, str]] = []
        for kind, url in rows:
            key = item_key(kind, url)
            if key in known:
                result["duplicates"] += 1
                continue
            known.add(key)
            fresh.append((kind, url))
        if not fresh:
            return
        live = sum(1 for item in items if item.get("status") != "deleted")
        if live + len(fresh) > MAX_BATCH_ITEMS:
            raise ValueError(f"一个批次最多 {MAX_BATCH_ITEMS} 条视频")
        next_index = max((int(item.get("index") or 0) for item in items), default=0)
        added = _build_items(fresh, defaults, next_index, created)
        items.extend(added)
        row["items"] = items
        row["total"] = len(items)
        row["finishedAt"] = None
        if not row.get("currentItemId"):
            row["currentItemId"] = added[0]["id"]
        result["added"] = len(added)

    batch_store.mutate(batch_id, apply)
    return {"state": batch_store.get(batch_id) or {}, **result}


def move_batch_item(batch_id: str, item_id: str, direction: str) -> dict[str, Any]:
    """把一条**还没开始处理**的条目在队列里上移 / 下移（调的是处理顺序）。

    用户 2026-09-15：批量页改成后台表格交互后要能自己调整顺序。出片严格按队列顺序
    一条一条跑（`_next_work` 按 `items` 的先后挑活），所以调顺序就是换 `items` 里的
    位置；正在出片（running/revising）和已经出片（completed）的条目锁死不能动，
    避免把正在跑的链子打乱。已删除的条目排在末尾不占位，只在**可见**条目之间换。
    """
    if direction not in {"up", "down"}:
        raise ValueError("direction 必须是 up 或 down")
    state = batch_store.get(batch_id)
    if not state:
        raise KeyError(batch_id)
    items = list(state.get("items") or [])
    live = [i for i, item in enumerate(items) if item.get("status") != "deleted"]
    pos = next((i for i in live if items[i].get("id") == item_id), None)
    if pos is None:
        raise RuntimeError("批量条目不存在")
    if items[pos].get("status") in {"running", "revising", "completed"}:
        raise ValueError("正在出片或已经出片的条目不能调整顺序")
    rank = live.index(pos)
    neighbor = rank - 1 if direction == "up" else rank + 1
    if neighbor < 0 or neighbor >= len(live):
        return state  # 已经在队列头/尾：什么都不用改
    other = live[neighbor]
    items[pos], items[other] = items[other], items[pos]

    def apply(row: dict[str, Any]) -> None:
        row["items"] = items

    batch_store.mutate(batch_id, apply)
    batch_store.add_item_log(
        batch_id, item_id, f"已把这条{'上移' if direction == 'up' else '下移'}（影响之后的处理顺序）。"
    )
    return batch_store.get(batch_id) or {}


def confirm_batch_items(batch_id: str, item_ids: list[str]) -> dict[str, Any]:
    """批量确认：一次放行多条「等待确认」且已有候选人物图的条目。

    用户 2026-09-15：批量页改成后台表格交互后要能批量确认已就绪的条目。放行只是把它们
    标成 `confirmed` 排队等出片（出片仍严格一条一条，不会并发）；缺候选图 / 不在待确认
    状态的条目逐个跳过并说明原因，让页面能提示「为什么那几条没放行」。
    """
    state = batch_store.get(batch_id)
    if not state:
        raise KeyError(batch_id)
    by_id = {item.get("id"): item for item in state.get("items") or []}
    confirmed: list[str] = []
    skipped: list[dict[str, str]] = []
    for item_id in item_ids or []:
        item = by_id.get(item_id)
        if not item:
            skipped.append({"id": item_id, "reason": "条目不存在"})
            continue
        if item.get("status") != "awaiting_review":
            skipped.append({"id": item_id, "reason": "不在待确认状态"})
            continue
        if not str((item.get("ai") or {}).get("reference_image_path") or "").strip():
            skipped.append({"id": item_id, "reason": "还没有候选人物图"})
            continue
        confirmed.append(item_id)
    for item_id in confirmed:
        batch_store.set_item_milestone(batch_id, item_id, "review", status="completed", progress=100)

        def approve(row: dict[str, Any]) -> None:
            row.update(
                status="confirmed",
                stage="confirmed",
                reviewApproved=True,
                approvedAt=now_iso(),
                error=None,
            )

        batch_store.mutate_item(batch_id, item_id, approve)
    return {"confirmed": confirmed, "skipped": skipped}


def mark_items_skipped(batch_id: str, item_ids: list[str]) -> dict[str, Any]:
    """批量跳过：没在出片的条目直接落 `skipped`；正在出片的标 `skipRequested` 交给 runner
    安全取消后落 skipped（成片已生成的照样抢救进发布目录，`_finish_abandoned` 兜底）。"""
    state = batch_store.get(batch_id)
    if not state:
        raise KeyError(batch_id)
    by_id = {item.get("id"): item for item in state.get("items") or []}
    marked: list[str] = []
    busy: list[str] = []
    rejected: list[dict[str, str]] = []
    for item_id in item_ids or []:
        item = by_id.get(item_id)
        if not item:
            rejected.append({"id": item_id, "reason": "条目不存在"})
            continue
        if item.get("status") in {"completed", "skipped", "deleted"}:
            rejected.append({"id": item_id, "reason": "已经结束"})
            continue
        batch_store.mutate_item(batch_id, item_id, lambda row: row.update(skipRequested=True))
        cancel_item_work(batch_id, item_id)
        if item.get("status") not in {"running", "revising"}:
            def mark_skipped(row: dict[str, Any]) -> None:
                row.update(status="skipped", stage="skipped", finishedAt=now_iso(), childJob=None)
                for milestone in row.get("milestones") or []:
                    if milestone.get("status") in {"pending", "running"}:
                        milestone["status"] = "skipped"

            batch_store.mutate_item(batch_id, item_id, mark_skipped)
            marked.append(item_id)
        else:
            busy.append(item_id)
    return {"marked": marked, "busy": busy, "rejected": rejected}


def mark_items_deleted(batch_id: str, item_ids: list[str]) -> dict[str, Any]:
    """批量删除：没在出片的条目直接落 `deleted`；正在出片的标 `deleteRequested` 交给
    runner 安全取消后落 deleted（成片已生成的照样抢救进发布目录，不丢交付）。"""
    state = batch_store.get(batch_id)
    if not state:
        raise KeyError(batch_id)
    by_id = {item.get("id"): item for item in state.get("items") or []}
    marked: list[str] = []
    busy: list[str] = []
    rejected: list[dict[str, str]] = []
    for item_id in item_ids or []:
        item = by_id.get(item_id)
        if not item:
            rejected.append({"id": item_id, "reason": "条目不存在"})
            continue
        if item.get("status") == "deleted":
            rejected.append({"id": item_id, "reason": "已经删除"})
            continue
        batch_store.mutate_item(batch_id, item_id, lambda row: row.update(deleteRequested=True))
        cancel_item_work(batch_id, item_id)
        if item.get("status") not in {"running", "revising"}:
            def mark_deleted(row: dict[str, Any]) -> None:
                row.update(status="deleted", stage="deleted", finishedAt=now_iso(), childJob=None)
                for milestone in row.get("milestones") or []:
                    if milestone.get("status") in {"pending", "running"}:
                        milestone["status"] = "skipped"

            batch_store.mutate_item(batch_id, item_id, mark_deleted)
            marked.append(item_id)
        else:
            busy.append(item_id)
    return {"marked": marked, "busy": busy, "rejected": rejected}


def restart_item_to_run(batch_id: str, item_id: str) -> bool:
    """把一条**已经结束**的条目重置成可以重跑的状态，返回它是否直接续跑。

    失败 / 已跳过 / 已出片共用的重置逻辑（单条「重试 / 重新开始」与批量「重新开始」
    必须走同一份代码，否则两边的行为会慢慢跑偏）：

    - 已经确认过（审核放行 + 有候选人物图 + **源视频还在磁盘上**）→ 回到 `confirmed`，
      只重跑出片链路，不重复下载与备料；
    - 还没确认、或者源视频已经被清理掉 → 回到 `pending`，从下载与备料重新走一遍
      （2026-09-14 实测：源文件不在时直接续跑会在提交时报「文件不存在」）；
    - 清掉 skip/delete 请求、错误、上一次的子任务与发布文件记录，并把 `video`（以及任何
      报错的）里程碑重置为待办，页面上的流程重新变回待办而不是停在 ✗。
    """
    box: dict[str, bool] = {}

    def reset(row: dict[str, Any]) -> None:
        ai = row.get("ai") or {}
        approved = (
            bool(row.get("reviewApproved"))
            and bool(str(ai.get("reference_image_path") or "").strip())
            and Path(str(row.get("sourcePath") or "")).is_file()
        )
        box["approved"] = approved
        row.update(
            status="confirmed" if approved else "pending",
            stage="confirmed" if approved else "queued",
            error=None,
            warning=None,
            skipRequested=False,
            deleteRequested=False,
            childJob=None,
            videoJobId=None,
            outputs={},
            finishedAt=None,
        )
        for milestone in row.get("milestones") or []:
            if milestone.get("id") == "video" or milestone.get("status") == "error":
                milestone.update(status="pending", progress=0, currentNode=None, finishedAt=None)

    batch_store.mutate_item(batch_id, item_id, reset)
    return bool(box.get("approved"))


def restart_batch_items(batch_id: str, item_ids: list[str]) -> dict[str, Any]:
    """批量重新开始：失败 / 已跳过 / **已出片**的条目一次全部重来。

    用户 2026-09-15：「批量选择选择后加个批量重新开始」——跟批量确认/跳过/删除一套交互。
    能重开的只有「已经跑完一轮」的状态（`failed` / `skipped` / `completed`），跟单条
    「重试 / 重新开始」按钮完全同一条规则：正在出片（`running` / `revising`）的必须先
    「取消」，已删除的不能复活，还没出片的（`pending` / `awaiting_review` / `confirmed`）
    本来就会跑、不需要重开 —— 这些都逐个记下原因返回，页面照旧提示「哪几条没重开、为什么」。
    出片仍然严格一条一条（重置后只是回到队列/出片队列，不并发）。
    """
    state = batch_store.get(batch_id)
    if not state:
        raise KeyError(batch_id)
    by_id = {item.get("id"): item for item in state.get("items") or []}
    restarted: list[str] = []
    rejected: list[dict[str, str]] = []
    for item_id in item_ids or []:
        item = by_id.get(item_id)
        if not item:
            rejected.append({"id": item_id, "reason": "条目不存在"})
            continue
        status = str(item.get("status") or "")
        if status in {"running", "revising"}:
            rejected.append({"id": item_id, "reason": "正在出片，请先「取消」再重新开始"})
            continue
        if status == "deleted":
            rejected.append({"id": item_id, "reason": "已删除"})
            continue
        if status not in RETRYABLE_ITEM_STATUSES:
            rejected.append({"id": item_id, "reason": "还没出片，不用重新开始"})
            continue
        direct = restart_item_to_run(batch_id, item_id)
        batch_store.add_item_log(
            batch_id,
            item_id,
            "已重新开始这一条，正在按当前结果继续出片。" if direct else "已重新开始这一条，将从下载与备料重做。",
        )
        restarted.append(item_id)
    return {"restarted": restarted, "rejected": rejected}


def _item(batch_id: str, item_id: str) -> dict[str, Any]:
    state = batch_store.get(batch_id)
    for item in (state or {}).get("items") or []:
        if item.get("id") == item_id:
            return item
    raise RuntimeError("批量条目不存在")

def _set_item(batch_id: str, item_id: str, **changes: Any) -> dict[str, Any]:
    def apply(item: dict[str, Any]) -> None:
        item.update(changes)
        item["updatedAt"] = now_iso()

    state = batch_store.mutate_item(batch_id, item_id, apply)
    return next(item for item in state["items"] if item["id"] == item_id)


def item_ratio(item: dict[str, Any]) -> str:
    """条目当前的画布比例：歌曲默认 4:3、跳舞默认 9:16，用户在页面上逐条可改。

    历史批次里没有 `ratio` 字段（甚至可能是脏值），这里兜底成类型默认值，保证老批次
    重试/出片时行为不变；写入路径（新建批次 / 改比例接口）仍然严格校验。
    """
    kind = str(item.get("kind") or "singing")
    try:
        return normalize_batch_ratio(item.get("ratio"), kind)
    except ValueError:
        return batch_default_ratio(kind)


def ratio_hint(kind: str, ratio: str) -> str:
    """给日志/前端用的比例说明（生成分辨率按各链路参数组不同）。"""
    if kind == "singing":
        return {
            "4:3": "4:3 横版（生成 640×480，二采 1440×1080）",
            "9:16": "9:16 竖版（生成 480×864，二采 1080×1920）",
        }.get(ratio, ratio)
    return {
        "4:3": "4:3 横版（生成 512×384，二采 1440×1080）",
        "9:16": "9:16 竖版（生成 512×896，二采 1080×1920）",
    }.get(ratio, ratio)


def _safe_name(value: str, fallback: str = "作品") -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value or "")
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return (clean[:48] or fallback).strip()


def _manifest_metadata(aweme_id: str) -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        return {}
    try:
        lines = MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if str(row.get("aweme_id") or "") == aweme_id:
            return row
    return {}


class DuplicateItem(RuntimeError):
    """同一个抖音作品已经在批次里：本条自动跳过，不重复烧一遍算力。"""


def duplicate_item_by_aweme(
    batch_id: str, item_id: str, aweme_id: str, kind: str = ""
) -> dict[str, Any] | None:
    """按作品号找批次里已经存在的同一条视频（同类型、未删除未跳过）。

    链接指纹只能挡住写法不同的同一个链接；同一条作品用两种分享方式（短链 vs
    `www.douyin.com/video/{id}`）粘进来时只有下载后拿到的 awemeId 才能识别，
    所以下载完立刻再兜一次底。已删除/已跳过的条目不算重复——用户明确不要了。
    """
    if not aweme_id:
        return None
    state = batch_store.get(batch_id) or {}
    for item in state.get("items") or []:
        if item.get("id") == item_id:
            continue
        if item.get("status") in {"deleted", "skipped"}:
            continue
        if kind and str(item.get("kind") or "") != kind:
            continue
        if str(item.get("awemeId") or "") == str(aweme_id):
            return item
    return None


def cached_download_path(url: str) -> Path | None:
    """本地已经有这条作品（按作品号能对上文件）时返回它，否则 None。

    用户 2026-09-13：「抖音下载的时候如果已经有了就不要下载了」。
    下载器子进程自己也会跳过已存在的视频（`core/video_downloader.py`：
    「Video %s already downloaded, skipping」），但那条路要**先把下载服务拉起来、再提交一次任务**
    （一个完整 Python 进程 + 一次轮询）。这里在提交之前就先在本地找一遍：命中就直接复用。

    找法：① 下载器写的 metadata 清单（`download_manifest.jsonl` 的 `file_paths`，最可靠）；
    ② 清单没有/路径变了时，按作品号在下载目录里搜（结构与 `douyin_service.result_for` 一致）。
    """
    aweme_id = extract_aweme_id(str(url or ""))
    if not aweme_id:
        return None
    roots: list[Path] = []
    for root in (DOUYIN_OUTPUT, MANIFEST_PATH.parent):
        candidate = Path(root)
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    if not roots:
        return None
    metadata = _manifest_metadata(aweme_id)
    for relative in metadata.get("file_paths") or []:
        for root in roots:
            candidate = root / str(relative)
            if candidate.is_file():
                return candidate
    for root in roots:
        matches = [
            path
            for path in root.rglob(f"*{aweme_id}*")
            if path.is_file()
            and path.suffix.lower() in VIDEO_SUFFIXES
            and ".h3-converted" not in path.name
            and ".part." not in path.name
        ]
        if matches:
            return max(matches, key=lambda path: path.stat().st_mtime)
    return None


async def _adopt_existing_source(batch_id: str, item_id: str, cached: Path) -> Path:
    """复用本地已有的源视频：照常过判重、转码与元数据，但不下载。"""
    item = _item(batch_id, item_id)
    kind = str(item.get("kind") or "")
    aweme_id = extract_aweme_id(str(item.get("url") or "")) or str(item.get("awemeId") or "") or cached.stem
    duplicate = duplicate_item_by_aweme(batch_id, item_id, aweme_id, kind)
    if duplicate is not None:
        raise DuplicateItem(
            f"和第 {duplicate.get('index')} 条是同一个抖音作品"
            f"（{duplicate.get('title') or duplicate.get('url')}），本条已自动跳过，不重复制作。"
        )
    source = await ensure_download_playable(cached, aweme_id)
    metadata = _manifest_metadata(aweme_id)
    title = str(metadata.get("desc") or source.stem).splitlines()[0].strip() or source.stem
    _set_item(
        batch_id,
        item_id,
        sourcePath=str(source.resolve()),
        sourceName=source.name,
        sourceOrigin="douyin",
        awemeId=aweme_id,
        sourceMetadata=metadata,
        title=title[:80],
    )
    batch_store.set_item_milestone(batch_id, item_id, "download", status="completed", progress=100)
    batch_store.add_item_log(batch_id, item_id, f"本地已有这条视频，跳过下载：{source.name}")
    return source


async def _download(batch_id: str, item_id: str) -> Path:
    item = _item(batch_id, item_id)
    existing = Path(item.get("sourcePath") or "")
    if existing.is_file():
        return existing
    # 这条作品本地已经有了（之前下过、或另一条用过同一条素材）→ 直接用，不启动下载器
    cached = await asyncio.to_thread(cached_download_path, str(item.get("url") or ""))
    if cached is not None:
        batch_store.set_item_milestone(batch_id, item_id, "download", status="running", progress=5)
        _set_item(batch_id, item_id, stage="download", status="running", error=None)
        return await _adopt_existing_source(batch_id, item_id, cached)
    batch_store.set_item_milestone(batch_id, item_id, "download", status="running", progress=5)
    _set_item(batch_id, item_id, stage="download", status="running", error=None)
    batch_store.add_item_log(batch_id, item_id, "正在下载抖音源视频……")
    try:
        # 先走下载器 REST；它取作品详情用的是被 Argus 门禁的 Web 接口，会时好时坏，
        # 失败时用 douyin_direct（分享页 + aweme.snssdk.com 直连）兜底。
        service_failure: Exception | None = None
        source: Path | None = None
        aweme_id = ""
        metadata: dict[str, Any] = {}
        try:
            job = await douyin_service.submit(item["url"])
            job_id = str(job.get("job_id") or "")
            if not job_id:
                raise RuntimeError("下载服务没有返回任务编号")
            _set_item(batch_id, item_id, downloadJobId=job_id)
            while job.get("status") not in {"success", "failed", "cancelled"}:
                await asyncio.sleep(2)
                job = await douyin_service.job(job_id)
                mirror_upsert([job])
                done = int(job.get("success") or 0) + int(job.get("failed") or 0) + int(job.get("skipped") or 0)
                total = max(1, int(job.get("total") or 1))
                batch_store.set_item_milestone(
                    batch_id,
                    item_id,
                    "download",
                    status="running",
                    progress=min(92, max(8, round(done / total * 90))),
                )
            mirror_upsert([job])
            if job.get("status") != "success":
                raise RuntimeError(str(job.get("error") or "抖音下载失败"))
            result = douyin_service.result_for(job)
            if not result:
                raise RuntimeError("下载完成但没有找到视频文件")
            aweme_id = str(result["awemeId"])
            source = await ensure_download_playable(Path(result["path"]), aweme_id)
            metadata = _manifest_metadata(aweme_id)
        except Exception as error:  # noqa: BLE001 - 下载器失败就兜底
            service_failure = error
            batch_store.add_item_log(
                batch_id, item_id,
                f"下载器未能取到源视频（{error}），改用直连兜底下载……",
            )
            from . import douyin_direct
            got = await asyncio.to_thread(douyin_direct.fetch, str(item["url"]), DOUYIN_OUTPUT)
            if not got:
                raise
            path, direct_meta = got
            aweme_id = str(direct_meta["awemeId"])
            source = await ensure_download_playable(Path(path), aweme_id)
            metadata = {
                "desc": direct_meta.get("desc") or "",
                "tags": list(direct_meta.get("tags") or []),
                "nickname": direct_meta.get("nickname") or "",
                "source": "direct",
            }
            batch_store.add_item_log(batch_id, item_id, "直连兜底下载成功。")

        # 链接写法不同的同一条作品只有下载后才知道，这里再兜一次重复过滤
        duplicate = duplicate_item_by_aweme(
            batch_id, item_id, aweme_id, str(item.get("kind") or "")
        )
        if duplicate is not None:
            raise DuplicateItem(
                f"和第 {duplicate.get('index')} 条是同一个抖音作品"
                f"（{duplicate.get('title') or duplicate.get('url')}），本条已自动跳过，不重复制作。"
            )
        title = str(metadata.get("desc") or source.stem).splitlines()[0].strip() or source.stem
        _set_item(
            batch_id,
            item_id,
            sourcePath=str(source.resolve()),
            sourceName=source.name,
            sourceOrigin="douyin",
            awemeId=aweme_id,
            sourceMetadata=metadata,
            title=title[:80],
        )
        batch_store.set_item_milestone(batch_id, item_id, "download", status="completed", progress=100)
        batch_store.add_item_log(batch_id, item_id, f"源视频已就绪：{source.name}")
        return source
    except DouyinServiceError as error:
        raise RuntimeError(str(error)) from error
    finally:
        # 下载完成立刻释放下载器内存，给图片分析与 ComfyUI 留足空间。
        await douyin_service.stop()


def extract_scene_frame(source: Path, target: Path, ratio: float = 0.38) -> Path:
    """从源视频抽一帧全分辨率画面，作为 Krea2 双图编辑的「图像-1 造型场景」。"""
    duration = _video_duration_seconds(source)
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{duration * ratio:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(target),
        ],
        capture_output=True,
        timeout=120,
        creationflags=flags,
    )
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError("从源视频抽帧失败，无法生成候选人物图")
    return target


def _video_duration_seconds(source: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source),
        ],
        capture_output=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if result.returncode != 0:
        raise RuntimeError("无法读取视频时长，关键帧准备失败")
    try:
        return max(0.1, float(result.stdout.decode("utf-8", errors="replace").strip()))
    except ValueError as error:
        raise RuntimeError("视频时长信息无效") from error


def build_contact_sheet(source: Path, target: Path) -> Path:
    """Extract six full-span frames once so the model need not inspect the whole video."""
    duration = _video_duration_seconds(source)
    timestamps = [duration * ratio for ratio in (0.06, 0.22, 0.38, 0.56, 0.73, 0.91)]
    frames: list[tuple[Image.Image, float]] = []
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    for timestamp in timestamps:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(640,iw)':-2",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            capture_output=True,
            timeout=90,
            creationflags=flags,
        )
        if result.returncode != 0 or not result.stdout:
            continue
        with Image.open(io.BytesIO(result.stdout)) as opened:
            frames.append((opened.convert("RGB"), timestamp))
    if len(frames) < 3:
        raise RuntimeError("关键帧提取不足，无法可靠分析视频造型")
    cell = (520, 390)
    sheet = Image.new("RGB", (cell[0] * 3, cell[1] * 2), "#090716")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(COVER_FONT), 24) if COVER_FONT.is_file() else ImageFont.load_default()
    for index, (frame, timestamp) in enumerate(frames[:6]):
        fitted = ImageOps.contain(frame, (cell[0] - 12, cell[1] - 12), method=Image.Resampling.LANCZOS)
        x = index % 3 * cell[0] + (cell[0] - fitted.width) // 2
        y = index // 3 * cell[1] + (cell[1] - fitted.height) // 2
        sheet.paste(fitted, (x, y))
        label = f"{timestamp:.1f}s"
        box = draw.textbbox((0, 0), label, font=font)
        draw.rectangle((x + 8, y + 8, x + 22 + box[2], y + 18 + box[3]), fill="#090716")
        draw.text((x + 15, y + 11), label, font=font, fill="#74e6ed")
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target, format="JPEG", quality=91, optimize=True)
    return target


async def _prepare_review(
    batch_id: str,
    item_id: str,
    *,
    feedback: str = "",
    mode: str = "both",
) -> None:
    """预审一个条目：模型分析 + 本地出候选图，然后停在审核点等用户确认。

    整个预审跑在独立子任务里，跳过/删除时由 `cancel_item_work` 取消，
    取消后由 `run_batch` 的条目级 CancelledError 分支收尾。
    """
    key = f"{batch_id}:{item_id}"
    task = asyncio.create_task(_prepare_review_work(batch_id, item_id, feedback=feedback, mode=mode))
    _ITEM_TASKS[key] = task
    try:
        await task
    finally:
        if _ITEM_TASKS.get(key) is task:
            _ITEM_TASKS.pop(key, None)


def _adopt_previous_work(batch_id: str, item_id: str) -> bool:
    """同一条抖音作品**之前已经备过料**（提示词 / 候选图 / 文案都还在磁盘上）→ 直接沿用。

    用户 2026-09-13：「你可以知道是否已经建立的文件吗，如果已经建立的文件 当我复制抖音链接
    的时候不要在重复建立了直接往下走」+「我可能只跑完了前面的几步没有让 comfyui 去生成又重新
    跑了这条任务」—— 备料跑完但没出片、或者删了又重加同一条时，没必要再下载、再抽帧（6 帧联系表）、
    再调一次模型：把上一版的文件复制进本条目录、状态直接落到「等待你的确认」。**不碰 ComfyUI。**

    只在「本条自己还没有备料结果」且**不是**用户在要求调整（feedback/mode）时生效。
    """
    item = _item(batch_id, item_id)
    if item.get("ai"):
        return False
    aweme_id = str(item.get("awemeId") or "")
    if not aweme_id:
        return False
    candidates = [
        row
        for row in batch_store.items_for_aweme(aweme_id)
        if row.get("id") != item_id and (row.get("ai") or {}).get("imagePrompt")
    ]
    if not candidates:
        return False
    same_kind = [row for row in candidates if str(row.get("kind")) == str(item.get("kind"))]
    pool = same_kind or candidates
    pool.sort(key=lambda row: str(row.get("updatedAt") or ""), reverse=True)
    for previous in pool:
        previous_ai = dict(previous.get("ai") or {})
        previous_work = DATA_DIR / "batches" / str(previous.get("_batchId") or "") / str(previous["id"])
        work = DATA_DIR / "batches" / batch_id / item_id
        work.mkdir(parents=True, exist_ok=True)

        ratio = item_ratio(item)
        result = dict(previous_ai)
        # 画布比例是**条目级**字段：按本条的比例重拼出图提示词（与 set_item_ratio 同一套逻辑）
        result["imagePrompt"] = batch_ai.compose_image_prompt(
            str(item["kind"]),
            str(result.get("style_source") or "video"),
            str(result.get("imagePromptFeedback") or ""),
            str(result.get("imagePromptMode") or "both"),
            song_name=str(result.get("song_name") or ""),
            song_mood=str(result.get("song_mood") or ""),
            ratio=ratio,
        )
        result["imageRatio"] = ratio
        try:
            (work / "出图提示词.txt").write_text(str(result["imagePrompt"]), encoding="utf-8")
        except OSError:
            pass
        # 联系表 / 取景帧 / 候选图都复制进本条目录，别跨条目共用同一份文件
        copied_image = ""
        previous_image = Path(str(previous_ai.get("reference_image_path") or ""))
        if previous_image.is_file():
            suffix = previous_image.suffix.lower() if previous_image.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
            target = work / f"candidate_r{int(item.get('revision') or 0)}_upload{suffix}"
            try:
                shutil.copy2(previous_image, target)
                copied_image = str(target.resolve())
            except OSError:
                copied_image = ""
        if previous_work.is_dir():
            for name in ("source-contact-sheet.jpg", "scene-frame.jpg"):
                source = previous_work / name
                if source.is_file() and not (work / name).is_file():
                    try:
                        shutil.copy2(source, work / name)
                    except OSError:
                        pass
        result["reference_image_path"] = copied_image
        result["sceneFramePath"] = str(work / "scene-frame.jpg")

        _set_item(
            batch_id,
            item_id,
            ai=result,
            title=str(result.get("title") or item.get("title") or "候选作品"),
            status="awaiting_review",
            stage="review",
            reviewApproved=False,
            revisionFeedback="",
            revisionMode="",
            warning=None,
        )
        batch_store.set_item_milestone(batch_id, item_id, "prepare", status="completed", progress=100)
        batch_store.set_item_milestone(batch_id, item_id, "review", status="running")
        batch_store.add_item_log(
            batch_id,
            item_id,
            "这条作品之前已经备过料：直接沿用上次的出图提示词、人物图与发布文案"
            "（不会再下载/抽帧/调模型），可以直接确认出片，也可以在确认页换一张图。",
        )
        deliver_review_materials(batch_id, item_id)
        return True
    return False


async def _prepare_review_work(
    batch_id: str,
    item_id: str,
    *,
    feedback: str = "",
    mode: str = "both",
) -> None:
    item = _item(batch_id, item_id)
    batch_store.set_item_milestone(batch_id, item_id, "prepare", status="running", progress=5)
    _set_item(batch_id, item_id, stage="prepare", status="running", error=None)
    batch_store.add_item_log(
        batch_id,
        item_id,
        "正在按修改意见重新准备候选结果……" if feedback else "正在分析源视频并准备候选图与发布文案……",
    )
    # 这条作品之前已经备过料（源视频/提示词/成图/文案都在）→ 直接沿用，不再下载/抽帧/调模型。
    # 用户在要求调整（feedback/mode）时不走这条路，老实按意见重做。
    if not feedback and mode == "both":
        if await asyncio.to_thread(_adopt_previous_work, batch_id, item_id):
            return
    work = DATA_DIR / "batches" / batch_id / item_id
    work.mkdir(parents=True, exist_ok=True)
    source = Path(item["sourcePath"])
    duration = await asyncio.to_thread(_video_duration_seconds, source)

    contact_sheet = work / "source-contact-sheet.jpg"
    if not contact_sheet.is_file():
        batch_store.set_item_milestone(
            batch_id, item_id, "prepare", status="running", progress=8, currentNode="正在抽取 6 帧联系表"
        )
        await asyncio.to_thread(build_contact_sheet, source, contact_sheet)

    scene_frame = work / "scene-frame.jpg"
    if not scene_frame.is_file():
        scene_frame = await asyncio.to_thread(extract_scene_frame, source, scene_frame)

    meta = item.get("sourceMetadata") or {}
    meta_desc = str(meta.get("desc") or "")
    meta_tags = [str(tag) for tag in (meta.get("tags") or [])]
    warning = ""

    # 1) 模型分析：造型来源判断 + 发布文案 + 动作/运镜（或迁移提示词）。
    #    「只调图片」不重跑模型；失败不抛错，降级到源作品信息继续，避免单个条目把整批卡死。
    previous_ai = dict(item.get("ai") or {})
    result = reuse_previous_analysis(mode, previous_ai)
    if result is not None:
        batch_store.add_item_log(batch_id, item_id, "只调整图片：沿用上一版的文案与动作/运镜。")
    else:
        batch_store.set_item_milestone(
            batch_id, item_id, "prepare", status="running", progress=20, currentNode="正在分析画面与撰写文案"
        )
        try:
            result = await batch_ai.analyze(
                kind=item["kind"],
                duration=duration,
                contact_sheet=contact_sheet,
                description=meta_desc,
                tags=meta_tags,
                feedback=feedback,
                mode=mode,
                previous=previous_ai,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            result = batch_ai.fallback_result(kind=item["kind"], description=meta_desc, tags=meta_tags)
            warning = f"模型分析不可用，已降级为源作品信息：{error}"
            batch_store.add_item_log(batch_id, item_id, warning)

    # 歌曲条目的动作/运镜要能在审核时就给用户看，缺了就现在补上保守时间轴
    # （提交时 `_post_video_job` 还会再兜一次，两边逻辑一致）。
    if item["kind"] == "singing":
        action = str(result.get("action_prompt") or "").strip()
        camera = str(result.get("camera_prompt") or "").strip()
        if not action or not camera:
            fallback_action, fallback_camera = default_action_plan(duration)
            result["action_prompt"] = action or fallback_action
            result["camera_prompt"] = camera or fallback_camera
            batch_store.add_item_log(
                batch_id, item_id, "模型没有给出动作/运镜，已用按时长铺开的保守时间轴兜底。"
            )

    # 2) 候选人物图。
    #    默认 `manual`：系统只备料（提示词 + 图一 + 图二），图片由用户在 GPT 聊天里
    #    自行生成后上传回页面。也可用 H3_BATCH_IMAGE_PROVIDER 切回 api / local / frame。
    revision = int(item.get("revision") or 0)
    previous_image = Path((item.get("ai") or {}).get("reference_image_path") or "")
    target_image = work / f"candidate_r{revision}.png"
    fallback_image = work / f"candidate_r{revision}_frame.png"
    provider = _image_provider()
    if provider == "auto":
        provider = "api" if batch_image.configured() else "manual"

    style_source = str(result.get("style_source") or "video")
    ratio = item_ratio(item)
    image_prompt = batch_ai.compose_image_prompt(
        item["kind"],
        style_source,
        feedback,
        mode,
        song_name=str(result.get("song_name") or ""),
        song_mood=str(result.get("song_mood") or ""),
        ratio=ratio,
    )
    scene = scene_frame
    if provider != "manual":
        if item["kind"] == "singing" and style_source == "redesign":
            # 源视频造型不适合出片：改按歌曲情绪重做造型。
            scene = IDENTITY_PATH
            image_prompt = (
                "本次没有造型参考图。图像-1 与图像-2 是同一位人物的身份参考，"
                "请按歌曲情绪完全重新设计造型、服装、背景与灯光。\n\n" + image_prompt
            )
        else:
            # 图一里是源视频那个人的脸，必须糊掉：否则模型会把别人的五官混进来。
            scene, defocused = await batch_image.defocus_reference_face(scene_frame, work)
            if defocused:
                batch_store.add_item_log(
                    batch_id, item_id, "已把参考帧里的人脸模糊掉，避免混入源视频人物的五官。"
                )
            else:
                note = "参考帧人脸定位失败，未能屏蔽源视频人物的脸（可能影响五官一致性）。"
                warning = f"{warning} {note}".strip()
                batch_store.add_item_log(batch_id, item_id, note)

    # 出图素材写进状态；提示词同时落盘，方便直接从条目目录取用
    result["imagePrompt"] = image_prompt
    result["sceneFramePath"] = str(scene_frame)
    # 记住拼提示词用的参数：用户中途改画布比例时，要能按新比例重拼这一条提示词
    result["imagePromptFeedback"] = feedback
    result["imagePromptMode"] = mode
    result["imageRatio"] = ratio
    try:
        (work / "出图提示词.txt").write_text(image_prompt, encoding="utf-8")
    except OSError:
        pass

    image_path: Path | None = None

    if mode == "copy" and previous_image.is_file():
        image_path = previous_image
    elif provider == "manual":
        if previous_image.is_file():
            # 手动模式永远不自动出图：已有成图就保留，用户上传新图即为替换。
            image_path = previous_image
        else:
            batch_store.add_item_log(
                batch_id,
                item_id,
                "已备好出图素材：复制提示词、下载图一与图二，在 GPT 聊天里生成后把图上传回来。",
            )
    elif provider == "frame":
        await asyncio.to_thread(shutil.copy2, scene_frame, fallback_image)
        image_path = fallback_image
        note = "已直接用源视频取帧作为候选人物图。"
        warning = f"{warning} {note}".strip()
        batch_store.add_item_log(batch_id, item_id, note)
    else:

        async def report(fraction: float, note: str) -> None:
            changes: dict[str, Any] = {"currentNode": note}
            if fraction > 0:
                changes["progress"] = round(30 + 68 * max(0.0, min(1.0, fraction)))
            else:
                changes["progress"] = None
            batch_store.set_item_milestone(
                batch_id, item_id, "prepare", status="running", **changes
            )

        try:
            batch_store.set_item_milestone(
                batch_id, item_id, "prepare", status="running", progress=32, currentNode="正在生成候选人物图"
            )
            if provider == "local":
                await batch_portrait.generate_portrait(
                    scene_image=scene,
                    identity_image=IDENTITY_PATH,
                    prompt=image_prompt,
                    ratio=ratio,
                    output_path=target_image,
                    prefix=f"batch_{item_id}",
                    on_progress=report,
                )
            else:
                await batch_image.generate_candidate_image(
                    scene_image=scene,
                    identity_image=IDENTITY_PATH,
                    prompt=image_prompt,
                    ratio=ratio,
                    output_path=target_image,
                    on_progress=report,
                )
            image_path = target_image
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await asyncio.to_thread(shutil.copy2, scene_frame, fallback_image)
            image_path = fallback_image
            note = f"候选图生成失败，已退回源视频取帧：{error}"
            warning = f"{warning} {note}".strip()
            batch_store.add_item_log(batch_id, item_id, note)

        # 换脸锁定身份：编辑模型是重新合成脸，只靠提示词保不住五官。
        # **默认关闭**：用户实测 ReActor 换脸「效果太差」（贴脸感明显、肤色与脖子对不上），
        # 所以只在显式设置 H3_BATCH_FACE_SWAP=1 时才启用，不默认污染结果。
        if (
            env_value("H3_BATCH_FACE_SWAP", "0").strip() == "1"
            and image_path == target_image
            and target_image.is_file()
        ):
            swapped = work / f"candidate_r{revision}_swapped.png"
            try:
                batch_store.set_item_milestone(
                    batch_id, item_id, "prepare", status="running", progress=None,
                    currentNode="正在用 ReActor 锁定五官",
                )
                await batch_image.swap_face_with_prototype(
                    generated=target_image, prototype=IDENTITY_PATH, output_path=swapped
                )
                image_path = swapped
                batch_store.add_item_log(batch_id, item_id, "已用 ReActor 把原型图的五官换到候选图上。")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                note = f"换脸失败，沿用未换脸的生成图（五官可能偏离原型图）：{error}"
                warning = f"{warning} {note}".strip()
                batch_store.add_item_log(batch_id, item_id, note)

    if image_path is not None:
        image_path = image_path.resolve()
        try:
            image_path.relative_to(work.resolve())
        except ValueError:
            raise RuntimeError("人物图必须保存在当前批次目录内") from None
        if not image_path.is_file():
            raise RuntimeError("人物图没有留下可用文件，请重新上传或点击重试")
        result["reference_image_path"] = str(image_path)
    else:
        result["reference_image_path"] = ""

    result["tags"] = [
        str(tag).strip().lstrip("#") for tag in result.get("tags") or [] if str(tag).strip()
    ][:5]

    # 3) 看着最终候选图重写发布文案，保证图文一致。
    #    预审的分析只看得到源视频联系表、看不到之后生成的图，两者一旦不一致
    #    （实测出图换成黑发水晶场景、文案却还在写「粉色氛围」），文案就会和画面脱节。
    #    「只调图片」按用户约定不动文案，所以那条路径不重写。
    if mode != "image" and result.get("reference_image_path"):
        batch_store.set_item_milestone(
            batch_id, item_id, "prepare", status="running", progress=99, currentNode="正在看着最终图写发布文案"
        )
        try:
            copy = await batch_ai.write_copy(
                kind=str(item.get("kind") or "singing"),
                candidate_image=Path(str(result["reference_image_path"])),
                song_name=str(result.get("song_name") or ""),
                song_mood=str(result.get("song_mood") or ""),
                description=meta_desc,
                feedback=feedback if mode in {"copy", "both"} else "",
            )
            for key in ("title", "introduction", "tags"):
                if copy.get(key):
                    result[key] = copy[key]
            result["tags"] = [
                str(tag).strip().lstrip("#") for tag in result.get("tags") or [] if str(tag).strip()
            ][:5]
            batch_store.add_item_log(batch_id, item_id, "发布文案已按最终画面重写，确保图文一致。")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            note = f"图文一致性的文案重写失败，沿用上一版文案：{error}"
            warning = f"{warning} {note}".strip()
            batch_store.add_item_log(batch_id, item_id, note)

    # 简介 / 标签缺了就自动生成：模型不可用（`fallback_result` 的简介恒为空、标签可能一个都没有）、
    # 模型返回空串、以及 manual 出图时用户还没上传图（根本没走 write_copy）都要覆盖。
    # 用户 2026-09-13：「流程中简介和标签没有的话自动生成」。
    filled = batch_ai.ensure_copy_fields(
        result,
        kind=str(item["kind"]),
        description=meta_desc,
        source_tags=meta_tags,
    )
    if filled:
        batch_store.add_item_log(
            batch_id, item_id, f"{'与'.join(filled)}为空，已按歌曲与源作品信息自动生成。"
        )

    _set_item(
        batch_id,
        item_id,
        ai=result,
        title=str(result.get("title") or item.get("title") or "候选作品"),
        status="awaiting_review",
        stage="review",
        reviewApproved=False,
        revisionFeedback="",
        revisionMode="",
        warning=warning or None,
    )
    batch_store.set_item_milestone(batch_id, item_id, "prepare", status="completed", progress=100)
    batch_store.set_item_milestone(batch_id, item_id, "review", status="running")
    if result.get("reference_image_path"):
        batch_store.add_item_log(batch_id, item_id, "候选图与发布文案已就绪，等待你确认出片。")
    else:
        batch_store.add_item_log(
            batch_id, item_id, "出图素材已备齐，等待你上传 GPT 生成的图片后再确认出片。"
        )
    # 审核点就把「人物图 + 发布文案」先放进发布目录（用户 2026-09-13 要求），
    # 最终成片等出片后由 `_deliver` 补上；失败只记日志，不影响停在审核点。
    await asyncio.to_thread(deliver_review_materials, batch_id, item_id)
    # 注意：这里**不**把整个批次置为 awaiting_review，也不停 runner ——
    # 用户要求先整批备料，所以要让 run_batch 继续跑下一条的备料。

    # 2026-09 图片流程自动化（opt-in）：manual 模式备料完、还没有候选图时，
    # 自动送 ChatGPT 桌面端生成候选人物图并上传回本条。由 `_IMAGE_LOCK` 保证
    # 严格一条一条完成（绝不并发）；失败只标黄停下等人工。开关 H3_AUTO_CHATGPT_IMAGE=1。
    if _image_provider() == "manual" and not result.get("reference_image_path"):
        from . import chatgpt_image as _cgi
        if _cgi.auto_candidate_enabled():
            batch_store.add_item_log(
                batch_id, item_id, "已开启自动生成人物图：正在送 ChatGPT 桌面端生成候选图……"
            )
            await _cgi.auto_generate_candidate_image(batch_id, item_id)

    # 封面自动化（opt-in）：人物图 + 发布文案落盘后，后台自动生成 B站4:3 / 抖音3:4。
    # 开关 H3_AUTO_CHATGPT_COVER=1；由 _IMAGE_LOCK 保证和人物图一样严格单链，绝不并发。
    _maybe_spawn_covers(batch_id, item_id)


def _maybe_spawn_covers(batch_id: str, item_id: str) -> None:
    """若已开启封面自动化且该条的人物图+发布文案都齐了，就后台触发封面生成。

    后台 fire-and-forget，不阻塞备料/出片；`_IMAGE_LOCK` 保证全局串行。
    """
    from . import chatgpt_image as _cgi
    if not _cgi.auto_cover_enabled():
        return
    item = _item(batch_id, item_id)
    outputs = item.get("outputs") or {}
    folder = outputs.get("folder")
    if not folder:
        return
    publish_folder = Path(folder)
    image = next(publish_folder.glob("人物图.*"), None) if publish_folder.is_dir() else None
    copy = publish_folder / "发布文案.txt"
    if image is None or not copy.is_file():
        return
    task = asyncio.create_task(
        _cgi.auto_generate_covers(str(publish_folder), str(copy), str(image))
    )
    _COVER_TASKS.add(task)
    task.add_done_callback(_COVER_TASKS.discard)


async def _wait_for_free_pipeline(batch_id: str, item_id: str) -> None:
    announced = False
    while True:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{BATCH_SELF_URL}/api/comfy/queue")
            payload = response.json() if response.is_success else {}
        active = payload.get("app")
        if not active or active.get("status") not in {"queued", "running"}:
            return
        if not announced:
            batch_store.add_item_log(batch_id, item_id, "已有单链路任务运行，当前条目正在等待资源。")
            announced = True
        await asyncio.sleep(4)


IMAGE_PROVIDERS = {"auto", "api", "local", "frame", "manual"}


def _image_provider() -> str:
    """候选人物图来源：`manual` 用户自己在 GPT 聊天里做（**默认**）/ `api` 中转站 /
    `local` 本地 Krea2 / `frame` 源视频取帧 / `auto`。

    默认 `manual`：用户明确要求「去掉图片生成，图片由我自己去 gpt 聊天补充」。
    出图质量与五官由用户把关，批量只负责备料（提示词 + 图一 + 图二）并接收成图。
    """
    value = env_value("H3_BATCH_IMAGE_PROVIDER", "manual").strip().lower()
    return value if value in IMAGE_PROVIDERS else "manual"


def reuse_previous_analysis(mode: str, previous: dict[str, Any]) -> dict[str, Any] | None:
    """「只调图片」时复用上一版的分析结果，不重新调用模型。

    否则文案与动作/运镜会被一起改写 —— 实测把「保留头顶留白」这类构图措辞
    串进了运镜时间轴。三种调整范围必须严格各管各的。
    """
    if mode == "image" and previous and previous.get("reference_image_path"):
        return dict(previous)
    return None


def default_action_plan(duration: float) -> tuple[str, str]:
    """模型不可用时的保守动作/运镜时间轴，按时长铺满，避免视频链路没有输入。"""
    span = max(4.0, min(60.0, float(duration or 0) or 30.0))
    half = round(span / 2, 1)
    action = (
        f"0–{half}秒：身体随节拍轻轻左右摇摆，目光自然看向镜头\n"
        f"{half}–{span}秒：头部小幅转动，肩部保持轻微律动，目光回到镜头"
    )
    camera = (
        f"0–{half}秒：保持稳定的近距离正面构图，只有轻微自然手持漂移\n"
        f"{half}–{span}秒：机位基本固定，人物保持居中，不做明显推拉摇移"
    )
    return action, camera


async def _post_video_job(batch_id: str, item_id: str) -> dict[str, Any]:
    item = _item(batch_id, item_id)
    ai = item.get("ai") or {}
    source = Path(item["sourcePath"])
    reference = Path(ai["reference_image_path"])
    # 比例以条目自己的选择为准（歌曲默认 4:3、跳舞默认 9:16，用户可逐条改）
    ratio = item_ratio(item)
    await _wait_for_free_pipeline(batch_id, item_id)
    batch_store.add_item_log(
        batch_id, item_id, f"本条画布比例：{ratio_hint(str(item['kind']), ratio)}"
    )
    if item["kind"] == "singing":
        # 动作/运镜已在预审时随文案一起产出（同一次模型调用）。
        # 模型降级时用按时长铺开的保守时间轴兜底，保证视频链路仍能启动。
        action = str(ai.get("action_prompt") or "").strip()
        camera = str(ai.get("camera_prompt") or "").strip()
        if not action or not camera:
            duration = await asyncio.to_thread(_video_duration_seconds, source)
            fallback_action, fallback_camera = default_action_plan(duration)
            action = action or fallback_action
            camera = camera or fallback_camera
            batch_store.add_item_log(batch_id, item_id, "动作/运镜缺少模型结果，已使用保守时间轴兜底。")
        current = _item(batch_id, item_id)
        if current.get("skipRequested") or current.get("deleteRequested"):
            raise asyncio.CancelledError
        _set_item(batch_id, item_id, actionPrompt=action, cameraPrompt=camera)
        data = {
            "action_prompt": action,
            "camera_prompt": camera,
            "ratio": ratio,
            "use_rvc": "1",
            "use_upscale": "1",
        }
        endpoint = "/api/jobs"
    else:
        # 迁移模式：默认「动作迁移」，可在审核点改成「人物替换」（2026-09-15 用户：「跳舞可以选择
        # 人物迁移吗 现在是动作迁移 生成的效果不好我想看下人物迁移会是什么效果」）。
        migrate_mode = str(ai.get("migrate_mode") or "animation")
        if migrate_mode not in {"animation", "replacement"}:
            migrate_mode = "animation"
        data = {
            "ratio": ratio,
            "remove_subtitles": "1" if ai.get("remove_subtitles") else "0",
            "mode": migrate_mode,
            "content_prompt": str(ai.get("content_prompt") or ""),
            "video_prompt": str(ai.get("video_prompt") or ""),
            "image_prompt": str(ai.get("image_prompt") or ""),
            "use_upscale": "1",
        }
        endpoint = "/api/jobs/migrate"
    async with httpx.AsyncClient(timeout=180) as client:
        with source.open("rb") as video_file, reference.open("rb") as image_file:
            response = await client.post(
                f"{BATCH_SELF_URL}{endpoint}",
                data=data,
                files={
                    "video": (source.name, video_file, "video/mp4"),
                    "reference_image": (reference.name, image_file, "image/png"),
                },
            )
    if not response.is_success:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        raise RuntimeError(detail or f"视频任务提交失败（HTTP {response.status_code}）")
    return response.json()


def child_progress_label(child: dict[str, Any]) -> str:
    """条目进度行里那句说明：当前节点 + 分段 / 去字幕 / 二采第几批。

    2026-09-14 用户问「批量跳舞视频没有进度吗」：跳舞（SCAIL 迁移）链路根本不广播
    ComfyUI 采样进度事件，子任务的 `progress` 全程是 `None`，而这里以前写着 `or 0`，
    于是条目进度条**整整 35 分钟冻在 0%**（实际已经跑完 7 段并进到二采）。
    分段 / 批次是真实数字，比一个假的百分比有用得多。
    """
    parts: list[str] = []
    title = str(child.get("currentNodeTitle") or "").strip()
    if title:
        parts.append(title)
    segment, segments = child.get("currentSegment"), child.get("estimatedSegments")
    if segment and segments:
        parts.append(f"分段 {segment}/{segments}")
    clean, cleans = child.get("cleanBatch"), child.get("cleanBatches")
    if clean and cleans:
        parts.append(f"去字幕 {clean}/{cleans}")
    upscale, upscales = child.get("upscaleBatch"), child.get("upscaleBatches")
    if upscale and upscales:
        parts.append(f"二采 {upscale}/{upscales}")
    return " · ".join(parts)


async def _watch_child(batch_id: str, item_id: str, child_id: str, milestone_id: str) -> dict[str, Any]:
    while True:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{BATCH_SELF_URL}/api/jobs/{child_id}")
        if not response.is_success:
            raise RuntimeError("无法读取子任务进度")
        child = response.json()
        snapshot = {
            "id": child.get("id"),
            "kind": child.get("kind"),
            "status": child.get("status"),
            "stage": child.get("stage"),
            "progress": child.get("progress"),
            "currentNodeTitle": child.get("currentNodeTitle"),
            "milestones": child.get("milestones") or [],
            "logs": (child.get("logs") or [])[-20:],
            "currentSegment": child.get("currentSegment"),
            "estimatedSegments": child.get("estimatedSegments"),
            "upscaleBatch": child.get("upscaleBatch"),
            "upscaleBatches": child.get("upscaleBatches"),
            "cleanBatch": child.get("cleanBatch"),
            "cleanBatches": child.get("cleanBatches"),
        }
        _set_item(batch_id, item_id, childJob=snapshot, stageMedia=stage_media(child))
        batch_store.set_item_milestone(
            batch_id,
            item_id,
            milestone_id,
            status="running",
            # None 必须保持 None：节点级进度未知时前端显示「进行中 + 已耗时」，
            # 写成 0 会让用户以为卡死（跳舞链路全程没有 progress 事件）。
            progress=child.get("progress"),
            currentNode=child_progress_label(child),
        )
        if child.get("status") == "completed":
            return child
        current = _item(batch_id, item_id)
        if current.get("skipRequested") or current.get("deleteRequested"):
            async with httpx.AsyncClient(timeout=20) as client:
                await client.post(f"{BATCH_SELF_URL}/api/jobs/{child_id}/cancel")
            raise asyncio.CancelledError
        if child.get("status") in {"failed", "cancelled", "interrupted"}:
            # 子任务被取消/中断时 errorSummary 是空的，只有一句「子任务失败」看不出原因
            # （2026-09-14 实测：用户在队列面板取消 ComfyUI 任务后条目只说「子任务失败」）。
            fallback = {
                "cancelled": "视频子任务被取消（可在这一条点「重试」重新出片）",
                "interrupted": "视频子任务被中断（本地服务重启过），点「重试」即可继续",
            }.get(str(child.get("status")), "子任务失败")
            raise RuntimeError(str(child.get("errorSummary") or child.get("errorDetail") or fallback))
        await asyncio.sleep(3)


def _copy_file(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def _deliverable_image(item: dict[str, Any]) -> Path | None:
    """本条要一起放进发布目录的人物图（就是这一条最终使用的候选图/用户上传的那张）。"""
    raw = str((item.get("ai") or {}).get("reference_image_path") or "").strip()
    path = Path(raw)
    return path if raw and path.is_file() else None


def _ideal_publish_folder(item: dict[str, Any]) -> Path:
    """发布目录的**理想名字**：`{三位编号}_{歌名或标题}_{作品号}`。"""
    ai = item.get("ai") or {}
    aweme_id = str(item.get("awemeId") or item.get("id"))
    base_name = _safe_name(str(ai.get("song_name") or ai.get("title") or item.get("title") or "作品"))
    return BATCH_OUTPUT_ROOT / f"{int(item['index']):03d}_{base_name}_{aweme_id}"


def _existing_publish_folder(item: dict[str, Any]) -> Path | None:
    """发布根里**已经属于同一条作品**的目录（之前批次留下的）。

    用户 2026-09-13：「现在加入队列目录又重复生成文件夹了……如果目录里已经有了最终成片
    说明已经生成过了……再次加入队列时候不要重复生成文件夹」。同一个作品号只允许一个目录：
    优先复用**已经有 `最终成片.mp4`** 的那个（说明这条出过片），其次复用最新的那个。
    """
    aweme = str(item.get("awemeId") or "").strip()
    if not aweme or not BATCH_OUTPUT_ROOT.is_dir():
        return None
    candidates = [
        path
        for path in BATCH_OUTPUT_ROOT.iterdir()
        if path.is_dir() and path.name.endswith(f"_{aweme}")
    ]
    if not candidates:
        return None
    finished = [path for path in candidates if (path / "最终成片.mp4").is_file()]
    pool = finished or candidates
    return max(pool, key=lambda path: path.stat().st_mtime)


def _recorded_publish_folder(item: dict[str, Any]) -> Path | None:
    """`outputs.folder` 里记的目录；不在发布根内（脏数据）就当没有。"""
    raw = str((item.get("outputs") or {}).get("folder") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        path.relative_to(BATCH_OUTPUT_ROOT)
    except ValueError:
        return None
    return path


def publish_folder(item: dict[str, Any]) -> Path:
    """本条作品的发布目录：**一条作品只能有一个目录**。

    标题会变（备料时一版、用户上传候选图后 `write_copy` 又改一版），如果每次都按新标题拼
    路径，同一个作品号就会留下好几个目录 —— 2026-09-13 用户实测：「我只开始了两个任务啊
    文件夹多了好多」，后来又发现**重新加入队列**时会再建一套（用户原话：「现在加入队列
    目录又重复生成文件夹了……如果目录里已经有了最终成片说明已经生成过了……再次加入队列
    时候不要重复生成文件夹」）。所以规则是：

    1. 发布根里已经有这个作品号的目录 → **直接复用**（优先带 `最终成片.mp4` 的那个）；
       本条的记录正好是这个目录、只是标题变了，就顺手改名到理想名；
    2. 没有同作品号的目录、但本条有记录 → 改名到理想名（改不动就用旧的）；
    3. 都没有 → 用理想名字。
    """
    ideal = _ideal_publish_folder(item)
    recorded = _recorded_publish_folder(item)
    existing = _existing_publish_folder(item)
    if existing is not None:
        if recorded is not None and recorded == existing and existing.name != ideal.name:
            try:
                existing.rename(ideal)
                return ideal
            except OSError:
                return existing
        return existing
    if recorded is not None and recorded.is_dir():
        try:
            recorded.rename(ideal)
            return ideal
        except OSError:
            return recorded
    return ideal


def publish_copy_text(ai: dict[str, Any]) -> str:
    tags = " ".join(f"#{str(tag).strip().lstrip('#')}" for tag in ai.get("tags") or [] if str(tag).strip())
    return (
        f"标题：\n{str(ai.get('title') or '').strip()}\n\n"
        f"简介：\n{str(ai.get('introduction') or '').strip()}\n\n"
        f"标签：\n{tags}\n"
    )


def _write_publish_image(folder: Path, image: Path | None) -> Path | None:
    """写 `人物图.<原后缀>`；先清掉同名其它后缀，避免换图后目录里留两张。"""
    for stale in folder.glob("人物图.*"):
        try:
            stale.unlink()
        except OSError:
            pass
    if image is None:
        return None
    suffix = image.suffix.lower() if image.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
    return _copy_file(image, folder / f"人物图{suffix}")


def deliver_review_materials(batch_id: str, item_id: str) -> dict[str, str] | None:
    """**审核点就把「人物图 + 发布文案」写进发布目录**，最终成片等出片后由 `_deliver` 补。

    用户 2026-09-13：「批量创建的时候 E:\\AI_Exports\\H3-MotionStudio\\发布成品 这个目录下
    怎么没有生成对应内容呢」—— 人物图（用户上传的那张）与发布文案在确认前就已就绪，
    没必要等成片跑完才落盘。这里只提前落这两件 + `folder`，**不碰里程碑、不写 videoFinal**，
    所以「发布文件还没整理 / 重新整理发布文件」的语义不变；`_deliver` 之后照旧覆盖补全。
    幂等：换图或重写文案后再次调用会覆盖同名文件。
    """
    item = _item(batch_id, item_id)
    ai = dict(item.get("ai") or {})
    if not ai:
        return None
    folder = publish_folder(item)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fresh: dict[str, str] = {}
        image = _write_publish_image(folder, _deliverable_image(item))
        if image is not None:
            fresh["image"] = str(image)
        copy_path = folder / "发布文案.txt"
        copy_path.write_text(publish_copy_text(ai), encoding="utf-8-sig")
        fresh["copy"] = str(copy_path)
        fresh["folder"] = str(folder.resolve())
        merged = {**(item.get("outputs") or {}), **fresh}
        _set_item(batch_id, item_id, outputs=merged)
    except OSError as error:
        batch_store.add_item_log(
            batch_id, item_id, f"提前写发布目录失败（不影响出片）：{error}"
        )
        return None
    batch_store.add_item_log(
        batch_id,
        item_id,
        f"{'人物图与发布文案' if 'image' in fresh else '发布文案'}已先放进发布目录："
        f"{folder}（最终成片跑完后再补）",
    )
    return merged


async def _deliver(
    batch_id: str,
    item_id: str,
    video_job: dict[str, Any],
) -> dict[str, str]:
    """把这一条的成品整理进发布目录：最终成片 + 人物图 + 发布文案。

    用户 2026-09-14：「最终成片没有在指定目录中出现，我上传的图片也要放到指定目录下」——
    成片一直会拷，人物图以前压根没拷，这里补上（用户上传的那张就是本条最终发布用图）。
    """
    item = _item(batch_id, item_id)
    ai = item.get("ai") or {}
    folder = publish_folder(item)
    folder.mkdir(parents=True, exist_ok=True)
    final_path = Path(video_job.get("finalOutput") or "")
    if not final_path.is_file():
        raise RuntimeError("视频任务完成但没有找到最终成片")
    outputs: dict[str, str] = {}
    final = _copy_file(final_path, folder / "最终成片.mp4")
    outputs["videoFinal"] = str(final)
    image = _write_publish_image(folder, _deliverable_image(item))
    if image is not None:
        outputs["image"] = str(image)
    copy_path = folder / "发布文案.txt"
    copy_path.write_text(publish_copy_text(ai), encoding="utf-8-sig")
    outputs["copy"] = str(copy_path)
    # 双封面由用户自己在 GPT 聊天里生成，这里不再渲染（2026-09-10 用户要求）。
    outputs["folder"] = str(folder.resolve())
    return outputs


async def _mark_delivered(batch_id: str, item_id: str, outputs: dict[str, str], *, note: str) -> None:
    """交付成功后的统一收尾：里程碑打勾 + 写 outputs + 记日志。

    交付本身没有独立的进度格（2026-09-13 用户去掉「整理发布文件」那一格），
    所以这里只把「生成最终视频」打勾。
    """
    batch_store.set_item_milestone(batch_id, item_id, "video", status="completed", progress=100)
    _set_item(
        batch_id,
        item_id,
        status="completed",
        stage="completed",
        outputs=outputs,
        finishedAt=now_iso(),
        error=None,
    )
    batch_store.add_item_log(batch_id, item_id, f"{note}{outputs['folder']}")


async def _salvage_deliver(batch_id: str, item_id: str) -> bool:
    """条目被跳过 / 删除 / 整批取消时，抢救已经出片的成果。

    用户 2026-09-14 实测：点「跳过」时子任务其实已经跑完（成片就在磁盘上），但原来的
    实现把结果整条丢掉——成片没进发布目录，后面的步骤全部标成 ✗。现在只要子任务真的
    完成过，就照样整理进发布目录并把已完成的步骤打勾（用户确认「照样整理」）。
    """
    item = _item(batch_id, item_id)
    job_id = str(item.get("videoJobId") or "")
    if not job_id:
        return False
    child = store.get(job_id)
    if not child or child.get("status") != "completed":
        return False
    if not Path(str(child.get("finalOutput") or "")).is_file():
        return False
    try:
        outputs = await _deliver(batch_id, item_id, child)
    except Exception as error:
        batch_store.add_item_log(batch_id, item_id, f"成片已生成，但整理发布文件失败：{error}")
        return False
    await _mark_delivered(batch_id, item_id, outputs, note="子任务已出片，已照样整理发布文件：")
    batch_store.add_item_log(
        batch_id, item_id, "这一条虽然被跳过/取消，但视频子任务已经跑完，成片与人物图已保留。"
    )
    return True


async def deliver_item_now(batch_id: str, item_id: str) -> dict[str, Any]:
    """手动补齐发布文件：给已经出片、但发布目录里没有成片的历史条目用。

    用户 2026-09-14：「加按钮并现在就把它们补出来」——页面上的
    「重新整理发布文件」按钮走这里，对老条目做一次幂等的重新交付。
    """
    item = _item(batch_id, item_id)
    if item.get("status") == "deleted":
        raise ValueError("这一条已经删除，不再整理发布文件")
    job_id = str(item.get("videoJobId") or "")
    if not job_id:
        raise ValueError("这一条还没有提交过视频任务，没有可整理的成片")
    child = store.get(job_id)
    if not child:
        raise ValueError("找不到这一条的视频任务记录")
    if child.get("status") != "completed":
        raise ValueError(f"视频任务还没有完成（当前：{child.get('status')}），无法整理发布文件")
    final_path = Path(str(child.get("finalOutput") or ""))
    if not final_path.is_file():
        raise ValueError(f"最终成片文件已不在磁盘上：{final_path}")
    # 交付没有独立的进度格；「生成最终视频」在这一步之前就已经完成，不要把它退回 running
    outputs = await _deliver(batch_id, item_id, child)
    await _mark_delivered(batch_id, item_id, outputs, note="已重新整理发布文件：")
    return batch_store.get(batch_id) or {}


async def salvage_abandoned_items(batch_id: str) -> int:
    """整批取消后，把已经出片但还没交付的条目补进发布目录（返回抢救成功的条数）。

    用户 2026-09-14 确认：「跳过 / 删除 / 取消整批」时已经跑完的成片照样整理，
    不能因为用户点了取消就把磁盘上已经做好的成片丢掉。
    """
    state = batch_store.get(batch_id) or {}
    saved = 0
    for item in state.get("items") or []:
        if item.get("status") == "deleted":
            continue
        if (item.get("outputs") or {}).get("videoFinal"):
            continue
        if await _salvage_deliver(batch_id, str(item.get("id") or "")):
            saved += 1
    return saved


async def _deferred_salvage(batch_id: str, item_id: str, job_id: str) -> None:
    """跳过/取消时子任务还在跑：等它落定，万一它自己跑完了就把成片补进发布目录。

    2026-09-13 实测：用户在 RVC 阶段点跳过，取消请求到得太晚，子任务 3 秒后照样
    完成——那一刻还没成片，抢救不到。这个看护任务负责补上这一步。
    """
    try:
        child = store.get(job_id)
        deadline = time.monotonic() + 3 * 60 * 60
        while (
            child
            and child.get("status") in {"queued", "running", "cancelling"}
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(10)
            child = store.get(job_id)
        if not child or child.get("status") != "completed":
            return
        item = batch_store.get(batch_id)
        row = next((it for it in (item or {}).get("items") or [] if it.get("id") == item_id), None)
        if row is None or (row.get("outputs") or {}).get("videoFinal"):
            return
        if await _salvage_deliver(batch_id, item_id):
            batch_store.add_item_log(
                batch_id, item_id, "视频子任务随后自行跑完，成片与人物图已补进发布目录。"
            )
    except Exception:
        return


def _spawn_deferred_salvage(batch_id: str, item_id: str, job_id: str) -> None:
    child = store.get(job_id)
    if not child or child.get("status") not in {"queued", "running", "cancelling"}:
        return
    task = asyncio.create_task(_deferred_salvage(batch_id, item_id, job_id))
    _SALVAGE_TASKS.add(task)
    task.add_done_callback(_SALVAGE_TASKS.discard)


def reset_review_row(row: dict[str, Any]) -> None:
    """把条目行重置回「等待你的确认」（纯函数，调用方自己负责持久化）。

    用户 2026-09-14：「我希望可以回到等待你的确认的页面，有可能我需要重新修改内容」——
    出片之后想换人物图 / 改画布比例 / 改跳舞条目的「去除字幕」时，必须能退回审核点。
    `reviewApproved` 一并清掉：退回确认页就代表上一次的放行作废，重新点确认才会再出片。
    旧成片留在发布目录里（`outputs` 不删），只是流程重新变成待办。
    """
    row.update(
        status="awaiting_review",
        stage="review",
        reviewApproved=False,
        reopenRequested=False,
        skipRequested=False,
        deleteRequested=False,
        childJob=None,
        videoJobId=None,
        error=None,
        finishedAt=None,
    )
    for milestone in row.get("milestones") or []:
        if milestone.get("id") == "review":
            # 与 _prepare_review 停在审核点时的状态一致（页面上是「等你确认」的进行中）
            milestone.update(status="running", progress=None, currentNode=None, finishedAt=None)
        elif milestone.get("id") == "video" or milestone.get("status") == "error":
            milestone.update(status="pending", progress=0, currentNode=None, finishedAt=None)


def reset_item_to_review(batch_id: str, item_id: str) -> None:
    batch_store.mutate_item(batch_id, item_id, reset_review_row)


def set_item_remove_subtitles(batch_id: str, item_id: str, value: bool) -> dict[str, Any]:
    """跳舞条目的「是否去除字幕」：和画布比例同一规则，没开始出片就能改。

    用户 2026-09-14：「跳舞视频我想要修改是否去除字幕这个操作」——以前这个值由预审模型
    给出且**只能看不能改**，现在在审核点（以及任何还没出片的状态）可以自己决定，
    确认出片时按这个值提交迁移工作流。
    """
    item = _item(batch_id, item_id)
    if str(item.get("kind") or "") != "dance":
        raise ValueError("只有跳舞条目有「去除字幕」开关")
    if not item_settings_editable(item):
        raise ValueError("这一条正在出片或已经完成，请先点「回到确认」再改去除字幕")
    ai = dict(item.get("ai") or {})
    ai["remove_subtitles"] = bool(value)

    def apply(row: dict[str, Any]) -> None:
        row["ai"] = ai

    batch_store.mutate_item(batch_id, item_id, apply)
    batch_store.add_item_log(
        batch_id,
        item_id,
        "已改为：先跑一遍去字幕再迁移。" if value else "已改为：直接用源视频驱动，不去字幕。",
    )
    return batch_store.get(batch_id) or {}

def set_item_migrate_mode(batch_id: str, item_id: str, mode: str) -> dict[str, Any]:
    """跳舞条目的「迁移模式」：动作迁移（默认）/ 人物替换，规则与画布比例完全一致。

    用户 2026-09-15：「跳舞可以选择人物迁移吗 现在是动作迁移 生成的效果不好我想看下人物迁移会是
    什么效果」——以前批量提交跳舞任务时把 `mode` **写死成 animation**，现在逐条可选，出片时原样
    提交给迁移工作流（`backend/workflows.py` 节点 #353：false=动作迁移、true=人物替换）。
    这只是**提交时**的选择：候选图、文案、提示词都不用重做，所以没开始出片就能随时改。
    """
    target = str(mode or "").strip()
    if target not in {"animation", "replacement"}:
        raise ValueError("迁移模式只支持「动作迁移」或「人物替换」")
    item = _item(batch_id, item_id)
    if str(item.get("kind") or "") != "dance":
        raise ValueError("只有跳舞条目有「迁移模式」")
    if not item_settings_editable(item):
        raise ValueError("这一条正在出片或已经完成，请先点「回到确认」再改迁移模式")
    ai = dict(item.get("ai") or {})
    ai["migrate_mode"] = target

    def apply(row: dict[str, Any]) -> None:
        row["ai"] = ai

    batch_store.mutate_item(batch_id, item_id, apply)
    batch_store.add_item_log(
        batch_id,
        item_id,
        "迁移模式已改为：人物替换（保留源视频场景，把人物换成候选图的人）。"
        if target == "replacement"
        else "迁移模式已改为：动作迁移（把源视频的动作迁移到候选图的人身上）。",
    )
    return batch_store.get(batch_id) or {}


async def _finish_abandoned(batch_id: str, item_id: str, *, deleted: bool) -> None:
    """条目被跳过 / 删除 / **退回确认页**时的收尾。

    用户 2026-09-14 实测「跳过时成片已经跑完但没进发布目录、后面的步骤全是 ✗」：
    只要视频子任务真的完成过，就照样整理发布文件并把已完成的步骤打勾；没出片的
    才整条标成已跳过/已删除，并留一个看护任务等子任务落定后再补一次交付。
    """
    current = _item(batch_id, item_id)
    job_id = str(current.get("videoJobId") or "")
    if not deleted and current.get("reopenRequested"):
        reset_item_to_review(batch_id, item_id)
        batch_store.add_item_log(
            batch_id, item_id, "已回到「等待你的确认」，可以换图、改比例或改去除字幕后重新确认。"
        )
        if (batch_store.get(batch_id) or {}).get("status") in {"completed", "cancelled", "failed"}:
            batch_store.update(
                batch_id,
                status="awaiting_review",
                stage="review",
                currentItemId=item_id,
                runnerActive=False,
                finishedAt=None,
                notice="已回到「等待你的确认」。",
            )
        return
    if job_id and await _salvage_deliver(batch_id, item_id):
        batch_store.add_item_log(
            batch_id,
            item_id,
            "当前条目已删除，但成片已经生成，已先整理进发布目录。" if deleted
            else "当前条目已跳过，但成片已经生成，已先整理进发布目录。",
        )
        return
    next_status = "deleted" if deleted else "skipped"
    _set_item(
        batch_id,
        item_id,
        status=next_status,
        stage=next_status,
        finishedAt=now_iso(),
        childJob=None,
    )
    for milestone in current.get("milestones") or []:
        if milestone.get("status") in {"pending", "running"}:
            batch_store.set_item_milestone(batch_id, item_id, milestone["id"], status="skipped")
    batch_store.add_item_log(batch_id, item_id, "当前条目已删除。" if deleted else "当前条目已跳过。")
    if job_id:
        _spawn_deferred_salvage(batch_id, item_id, job_id)


async def _process_confirmed(batch_id: str, item_id: str) -> None:
    _set_item(batch_id, item_id, status="running", stage="video", error=None, childJob=None)
    # 源视频可能已经被清理掉（用户重新开始一条老任务时很常见）：先补下载，
    # 否则提交视频任务时会直接抛「文件不存在」，看起来像系统坏了。
    item = _item(batch_id, item_id)
    if not Path(str(item.get("sourcePath") or "")).is_file():
        batch_store.add_item_log(batch_id, item_id, "源视频已不在磁盘上，正在重新下载……")
        await _download(batch_id, item_id)
    batch_store.set_item_milestone(batch_id, item_id, "video", status="running", progress=1)
    batch_store.add_item_log(batch_id, item_id, "已确认候选结果，开始执行视频生成单链路。")
    video_job = await _post_video_job(batch_id, item_id)
    _set_item(batch_id, item_id, videoJobId=video_job["id"], childJob=video_job)
    video_job = await _watch_child(batch_id, item_id, video_job["id"], "video")
    batch_store.set_item_milestone(batch_id, item_id, "video", status="completed", progress=100)
    _set_item(batch_id, item_id, stage="deliver", childJob=None)
    outputs = await _deliver(batch_id, item_id, video_job)
    await _mark_delivered(batch_id, item_id, outputs, note="发布文件已整理：")


def stage_media(state: dict[str, Any]) -> dict[str, str]:
    """把子任务状态里的中间成片路径抽出来，供页面只读回看每个阶段的产物。

    这些键在 `app.py` 的 `MEDIA_FIELD_BY_KEY` 里有对应项；成片入口只暴露
    final/original，但用户要求「可以回看每一阶段生成的内容」——所以中间产物
    （去字幕、二创草稿、二采高清）也要能被点开看，只是不能改。
    """
    media: dict[str, str] = {}
    for key, field in (
        ("draft", "draftOutput"),
        ("clean", "cleanOutput"),
        ("original", "originalOutput"),
        ("enhanced", "enhancedOutput"),
        ("final", "finalOutput"),
    ):
        raw = str(state.get(field) or "").strip()
        if raw and Path(raw).is_file():
            media[key] = raw
    return media


def _next_work(state: dict[str, Any], *, allow_confirmed: bool = True) -> dict[str, Any] | None:
    """下一个要跑的任务：先整批备料（pending / revising），再逐条出片（confirmed）。

    **队列不会自己跑**：只有用户点了「开跑」（`POST /api/batches/{id}/start`）才会 spawn
    runner（2026-09-10 用户：「加入队列并不是马上开跑，需要由我点击总的开跑按钮才开始」）。
    `awaiting_review` 是在等用户上传图片并确认，不算可跑任务。

    `allow_confirmed=False`：已经有一条在后台出片时，不再挑新的出片条目（出片仍严格
    一条一条来），但**继续**返回可备料的条目 —— 2026-09-13 用户实测的痛点：
    出片要十几分钟，期间新追加的条目一直卡在「排队中」拿不到候选图与文案。
    """
    items = state.get("items") or []
    for statuses in ({"pending", "revising"}, {"confirmed"}):
        if not allow_confirmed and statuses == {"confirmed"}:
            continue
        for item in items:
            if item.get("status") in statuses:
                return item
    return None


def _fail_item_and_batch(batch_id: str, item_id: str, message: str) -> None:
    """条目级失败：条目标错 + 整个批次停下等人工处理（与 runner 内联分支同一套收尾）。

    出片改到后台任务后，失败可能从任务里抛出来，收尾逻辑必须和原来内联时完全一致。
    """
    _set_item(batch_id, item_id, status="failed", stage="failed", error=message, childJob=None)
    active_milestone = next(
        (row for row in _item(batch_id, item_id)["milestones"] if row.get("status") == "running"),
        None,
    )
    if active_milestone:
        batch_store.set_item_milestone(
            batch_id, item_id, active_milestone["id"], status="error", currentNode=message
        )
    batch_store.add_item_log(batch_id, item_id, f"任务暂停：{message}")
    batch_store.update(
        batch_id,
        status="failed",
        stage="failed",
        runnerActive=False,
        notice="当前条目需要处理后重试，后续条目尚未启动。",
    )


async def run_batch(batch_id: str) -> None:
    """批次 runner：备料（下载 + 出图素材 + 文案）在循环里跑，**出片放到后台任务**。

    2026-09-13 用户实测的问题：出片一条要十几分钟，runner 原地 await 出片时，
    新追加的条目一直停在「排队中」，拿不到候选图与发布文案（「都处理了，现在没有处理啊」）。
    出片与备料互不争资源（备料不用 ComfyUI），所以出片改成后台任务、runner 继续备料；
    出片本身仍然严格一条一条来（`_next_work(allow_confirmed=...)`）。
    """
    if batch_id in _RUNNING_BATCHES:
        return
    _RUNNING_BATCHES.add(batch_id)
    video_task: asyncio.Task | None = None
    video_row: dict[str, Any] | None = None
    try:
        state = batch_store.get(batch_id)
        if not state:
            return
        batch_store.update(
            batch_id,
            status="running",
            stage="running",
            startedAt=state.get("startedAt") or now_iso(),
            runnerActive=True,
            pauseRequested=False,
            notice="批次正在运行。",
        )
        while True:
            state = batch_store.get(batch_id)
            if not state:
                return
            # ① 后台出片落定：在这里 await 一次，让失败/取消复用下面同一套收尾分支。
            if video_task is not None and video_task.done():
                finished, row = video_task, video_row
                video_task, video_row = None, None
                try:
                    await finished
                except asyncio.CancelledError:
                    current = _item(batch_id, str((row or {}).get("id") or ""))
                    await _finish_abandoned(
                        batch_id, current["id"], deleted=bool(current.get("deleteRequested"))
                    )
                except Exception as error:
                    _fail_item_and_batch(batch_id, str((row or {}).get("id") or ""), str(error))
                    return
                else:
                    # 出片正常返回却没把条目推进出 confirmed（异常路径之外不该发生）：
                    # 再挑一次就会无限重投出片任务，直接停下等人工处理。
                    landed_id = str((row or {}).get("id") or "")
                    landed = next(
                        (
                            item
                            for item in (batch_store.get(batch_id) or {}).get("items") or []
                            if item.get("id") == landed_id
                        ),
                        None,
                    )
                    if landed is not None and landed.get("status") == "confirmed":
                        _fail_item_and_batch(
                            batch_id, landed_id, "出片任务结束后条目仍停在「已确认」，已停下避免重复出片"
                        )
                        return
                continue
            # ② 暂停不打断正在出片的这一条（保持原语义：等它落定再停）。
            if state.get("pauseRequested") or state.get("status") == "paused":
                if video_task is not None:
                    await asyncio.wait({video_task}, timeout=3)
                    continue
                batch_store.update(batch_id, status="paused", runnerActive=False, notice="批次已暂停。")
                return
            # ③ 挑下一件活：备料优先，出片一次只许一条（`allow_confirmed`）。
            item = _next_work(state, allow_confirmed=video_task is None)
            if item is None:
                if video_task is not None:
                    # 备料都做完了，只剩后台出片：等它落定（每 3 秒回到循环顶，暂停仍能响应）
                    await asyncio.wait({video_task}, timeout=3)
                    continue
                items = state.get("items") or []
                waiting = [row for row in items if row.get("status") == "awaiting_review"]
                if waiting:
                    # 素材已备齐，停下来等用户逐条上传图片 + 确认出片
                    batch_store.update(
                        batch_id,
                        status="awaiting_review",
                        stage="review",
                        runnerActive=False,
                        currentItemId=state.get("currentItemId") or waiting[0]["id"],
                        notice=f"{len(waiting)} 条素材已备齐，等你确认出片。",
                    )
                    return
                warnings = any(row.get("warning") for row in items)
                batch_store.update(
                    batch_id,
                    status="completed",
                    stage="completed",
                    runnerActive=False,
                    currentItemId=None,
                    currentIndex=state.get("total") or 0,
                    finishedAt=now_iso(),
                    notice="全部条目已完成。" + (" 部分歌词字幕需要稍后重试。" if warnings else ""),
                )
                return
            preparing = item.get("status") in {"pending", "revising"}
            busy_with_video = video_task is not None and video_row is not None
            if preparing and busy_with_video:
                notice = (
                    f"第 {video_row['index']} 条正在出片，同时正在备料第 {item['index']} / {state['total']} 条。"
                )
            elif preparing:
                notice = f"正在备料第 {item['index']} / {state['total']} 条。"
            else:
                notice = f"正在出片第 {item['index']} / {state['total']} 条。"
            batch_store.update(
                batch_id,
                # 出片在后台跑时，页面焦点留在出片的那一条，不因为备料跳走
                currentItemId=(video_row["id"] if busy_with_video else item["id"]),
                currentIndex=(int(video_row["index"]) if busy_with_video else item["index"]),
                notice=notice,
            )
            try:
                if item.get("skipRequested"):
                    await _finish_abandoned(batch_id, item["id"], deleted=bool(item.get("deleteRequested")))
                    continue
                if item.get("status") == "confirmed":
                    # 出片丢进后台任务：runner 立刻回到循环顶，继续给后面的条目备料
                    # （备料只用下载 + ffmpeg 抽帧 + 一次文本模型调用，不碰 ComfyUI，
                    #  所以能和出片并行；用户 2026-09-13：「新加的这条现在没有处理啊」）。
                    task = asyncio.create_task(_process_confirmed(batch_id, item["id"]))
                    _VIDEO_TASKS.add(task)
                    task.add_done_callback(_VIDEO_TASKS.discard)
                    video_task, video_row = task, item
                    continue
                await _download(batch_id, item["id"])
                refreshed = _item(batch_id, item["id"])
                if refreshed.get("skipRequested") or refreshed.get("deleteRequested"):
                    raise asyncio.CancelledError
                feedback = str(refreshed.get("revisionFeedback") or "")
                mode = str(refreshed.get("revisionMode") or "both")
                await _prepare_review(batch_id, item["id"], feedback=feedback, mode=mode)
                continue
            except DuplicateItem as duplicate:
                # 同一条作品已经在批次里：直接标成「已跳过」，不当失败，也不拦住后面的条目
                message = str(duplicate)
                current = _item(batch_id, item["id"])
                _set_item(
                    batch_id,
                    item["id"],
                    status="skipped",
                    stage="skipped",
                    finishedAt=now_iso(),
                    childJob=None,
                    warning=message,
                )
                for milestone in current.get("milestones") or []:
                    if milestone.get("status") in {"pending", "running"}:
                        batch_store.set_item_milestone(
                            batch_id, item["id"], milestone["id"], status="skipped"
                        )
                batch_store.add_item_log(batch_id, item["id"], message)
                continue
            except asyncio.CancelledError:
                current = _item(batch_id, item["id"])
                await _finish_abandoned(
                    batch_id, item["id"], deleted=bool(current.get("deleteRequested"))
                )
                continue
            except Exception as error:
                current = _item(batch_id, item["id"])
                if current.get("skipRequested") or current.get("deleteRequested"):
                    await _finish_abandoned(
                        batch_id, item["id"], deleted=bool(current.get("deleteRequested"))
                    )
                    continue
                _fail_item_and_batch(batch_id, item["id"], str(error))
                return
    finally:
        _RUNNING_BATCHES.discard(batch_id)
        state = batch_store.get(batch_id)
        if state and state.get("runnerActive"):
            batch_store.update(batch_id, runnerActive=False)


def _ratio_aspect(ratio: str) -> float:
    width, height = (int(part) for part in ratio.split(":"))
    return (width / height) if height else 1.0


def _image_box(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except (OSError, ValueError):
        return None


def image_ratio_note(image: Path, ratio: str) -> str:
    """候选图与条目画布比例不一致时给一句人话提示；一致或读不出来时返回空串。

    只提示不拦截：最终构图由工作流按画布缩放，用户有权用任意比例的图出片。
    """
    box = _image_box(image)
    if not box:
        return ""
    width, height = box
    if not height:
        return ""
    expected = _ratio_aspect(ratio)
    if abs((width / height) - expected) / expected <= 0.08:
        return ""
    return (
        f"这张图是 {width}×{height}，与本条画布比例 {ratio} 不一致；"
        f"需要的话可以改用一张 {ratio} 的图（不换也能出片）。"
    )


def replace_item_source(
    batch_id: str,
    item_id: str,
    url: str,
    kind: str | None = None,
) -> dict[str, Any]:
    """把这一条的**源视频**换成另一条抖音链接（不用删了重加）。

    用户 2026-09-13：看完「本条源视频」发现是唱歌视频却放在跳舞槽里 →「要有让我可以替换的操作」。
    替换 = 这一条从头再来一遍：清掉旧源视频与**按它做的**分析（联系表 / 取景帧 / 出图提示词 /
    文案 / 候选图指针 / 里程碑），排回 `pending` 重新下载与备料，然后停在审核点等确认。

    - 类型可以一起改（唱歌 ↔ 跳舞）；改了类型就用新类型的默认画布比例，没改则保持原比例。
    - **不删用户的东西**：用户上传过的图还在条目目录里，只是不再指向它；可再生的分析缓存
      （联系表 / 取景帧 / 出图提示词）必须删掉，否则下载后会被当成新视频的分析结果复用。
    - `running` / `revising` / `completed` 不允许直接换（先「取消出片」或「回到确认」）。
    """
    item = _item(batch_id, item_id)
    status = str(item.get("status") or "")
    if status in {"running", "revising", "completed"}:
        raise ValueError("这一条正在出片或已经出片，请先「取消出片」或「回到确认」再换源视频")
    if status == "deleted":
        raise ValueError("这一条已经删除，不能换源视频")
    target_url = str(url or "").strip()
    if not is_douyin_url(target_url):
        raise ValueError("请填写有效的抖音链接")
    current_kind = str(item.get("kind") or "singing")
    target_kind = str(kind or current_kind).strip() or current_kind
    if target_kind not in {"singing", "dance"}:
        raise ValueError("类型只支持唱歌视频或跳舞视频")

    state = batch_store.get(batch_id) or {}
    for other in state.get("items") or []:
        if other.get("id") == item_id or other.get("status") in {"deleted", "skipped"}:
            continue
        if item_key(str(other.get("kind") or ""), str(other.get("url") or "")) == item_key(
            target_kind, target_url
        ):
            raise ValueError(
                f"第 {other.get('index')} 条已经是这条链接了，同一条视频不用重复制作"
            )

    ratio = item_ratio(item) if target_kind == current_kind else batch_default_ratio(target_kind)
    work = DATA_DIR / "batches" / batch_id / item_id

    def apply(row: dict[str, Any]) -> None:
        row.update(
            url=target_url,
            kind=target_kind,
            ratio=ratio,
            status="pending",
            stage="queued",
            title="等待处理",
            ai={},
            outputs={},
            stageMedia={},
            sourcePath="",
            sourceName="",
            sourceOrigin="douyin",
            awemeId="",
            sourceMetadata={},
            downloadJobId=None,
            videoJobId=None,
            childJob=None,
            reviewApproved=False,
            skipRequested=False,
            deleteRequested=False,
            reopenRequested=False,
            revision=0,
            revisionFeedback="",
            revisionMode="",
            error=None,
            warning=None,
            finishedAt=None,
            milestones=item_milestones(target_kind),
        )

    batch_store.mutate_item(batch_id, item_id, apply)
    # 旧视频的联系表 / 取景帧 / 出图提示词必须清掉：`_prepare_review_work` 命中即复用，
    # 留着就会把上一条视频的分析结果套到新视频上。
    for name in ("source-contact-sheet.jpg", "scene-frame.jpg", "出图提示词.txt"):
        try:
            (work / name).unlink(missing_ok=True)
        except OSError:
            pass
    batch_store.add_item_log(
        batch_id,
        item_id,
        f"已把源视频换成 {target_url}（类型：{'唱歌视频' if target_kind == 'singing' else '跳舞视频'}），"
        "正在重新下载与备料。",
    )
    return batch_store.get(batch_id) or {}


# 本机替换源视频的体积上限：抖音源一般几十 MB，本机录像可能更大；1 GiB 够用，
# 而且保存时是分块落盘（不把整段视频读进内存），不会和 ComfyUI 抢内存。
MAX_LOCAL_SOURCE_BYTES = 1024 * 1024 * 1024


def replace_item_source_file(batch_id: str, item_id: str, source: Path) -> dict[str, Any]:
    """把这一条的源视频换成**本机选的一个视频文件**：只换视频，其余内容一律不动。

    用户 2026-09-15：「替换源视频可以让我进行本地选择」+「其他内容都不需要改变只需要改变视频
    而且，所有定义好的内容都不需要变」——与换抖音链接（`replace_item_source`：清空重备料）不同，
    这条路**不动任何生成结果**：标题、简介、标签、候选人物图、画布比例与去除字幕、动作或迁移
    提示词、里程碑与当前状态、发布目录记录全部原样保留。

    但**「本条源视频」卡片的身份信息必须跟着换**（用户 2026-09-15：「本条源视频 那边的内容也
    替换一下 不然我不知道是否修改成功了」）：源文件换成本机视频后，旧的「抖音作品号」与
    「源作品文案」就不再成立，所以这里把它们清掉并打上 `sourceOrigin="local"`，
    页面据此显示「本机视频」、隐藏「打开抖音原链接」。`item.url` 保留（它是队列身份与
    源文件丢失时的回退下载地址）。
    """
    item = _item(batch_id, item_id)
    status = str(item.get("status") or "")
    if status in {"running", "revising"}:
        raise ValueError("这一条正在出片或重新备料，先「取消」再换源视频")
    if status in {"completed", "deleted"}:
        raise ValueError("这一条已经结束，不能换源视频")
    target = Path(source)
    if not target.is_file():
        raise ValueError("没有收到可用的视频文件")

    def apply(row: dict[str, Any]) -> None:
        # **不动任何生成结果**：`ai`（标题/简介/标签/候选图/提示词）、里程碑、outputs、比例、
        # 状态全部保持原样。只换源视频本身 + 换掉不再成立的「抖音身份」（作品号 / 源作品文案）。
        row["sourcePath"] = str(target)
        row["sourceName"] = target.name
        row["sourceOrigin"] = "local"
        row["awemeId"] = ""
        row["sourceMetadata"] = {}
        row["downloadJobId"] = None
        row["warning"] = None

    batch_store.mutate_item(batch_id, item_id, apply)
    batch_store.add_item_log(
        batch_id,
        item_id,
        f"已用本机文件替换源视频：{target.name}（标题、文案、候选图与其余设置保持不变）。",
    )
    return batch_store.get(batch_id) or {}


def item_settings_editable(item: dict[str, Any]) -> bool:
    """条目是否还处在「没开始出片」的可改阶段（用户 2026-09-14：「未开始前的任务都允许修改」）。

    - 可改：`pending`（排队中）、`awaiting_review`（等你确认）、`confirmed`（已放行但 runner
      还没轮到它）、`failed`、`skipped`——这些改比例/去字幕都只会影响**还没提交**的那次出片；
    - 不可改：`running`/`revising`（子任务已经在跑，改了就和工作流里的输入打架）、
      `completed`（已经出片，要先点「回到确认」）、`deleted`。
    """
    return str(item.get("status") or "") not in {"running", "revising", "completed", "deleted"}


def set_item_ratio(batch_id: str, item_id: str, ratio: str) -> dict[str, Any]:
    """改某一条视频的画布比例（歌曲默认 4:3、跳舞默认 9:16，用户逐条可改）。

    用户 2026-09-10：「每个视频需要让我选择比例，歌曲默认 4:3，跳舞默认 9:16，我可以改的」。
    2026-09-14 放宽为：**只要还没开始出片就能改**（见 `item_settings_editable`）。

    - 已经备过料的条目：按新比例重拼出图提示词并落盘，让用户拿到的备料与最终成片一致；
      旧候选图若与新比例不符只提示、不擅自删——图是用户自己出的，重做与否由他决定。
    """
    item = _item(batch_id, item_id)
    kind = str(item.get("kind") or "singing")
    target = normalize_batch_ratio(ratio, kind)
    if not item_settings_editable(item):
        raise ValueError("这一条正在出片或已经完成，请先点「回到确认」再改画布比例")
    previous = item_ratio(item)
    if target == previous:
        return batch_store.get(batch_id) or {}

    ai = dict(item.get("ai") or {})
    if ai.get("imagePrompt"):
        ai["imagePrompt"] = batch_ai.compose_image_prompt(
            kind,
            str(ai.get("style_source") or "video"),
            str(ai.get("imagePromptFeedback") or ""),
            str(ai.get("imagePromptMode") or "both"),
            song_name=str(ai.get("song_name") or ""),
            song_mood=str(ai.get("song_mood") or ""),
            ratio=target,
        )
        ai["imageRatio"] = target
        try:
            prompt_path = DATA_DIR / "batches" / batch_id / item_id / "出图提示词.txt"
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(str(ai["imagePrompt"]), encoding="utf-8")
        except OSError:
            pass

    warning = item.get("warning")
    note = f"画布比例已改为 {ratio_hint(kind, target)}。"
    mismatch = image_ratio_note(Path(str(ai.get("reference_image_path") or "")), target)
    if mismatch:
        warning = mismatch
        note = f"{note} {mismatch}"

    def apply(row: dict[str, Any]) -> None:
        row["ratio"] = target
        row["ai"] = ai
        row["warning"] = warning

    batch_store.mutate_item(batch_id, item_id, apply)
    batch_store.add_item_log(batch_id, item_id, note)
    return batch_store.get(batch_id) or {}


def request_review_adjustment(batch_id: str, item_id: str, feedback: str, mode: str) -> dict[str, Any]:
    item = _item(batch_id, item_id)
    if item.get("status") != "awaiting_review":
        raise ValueError("当前条目不在审核状态")
    if mode not in {"image", "copy", "both"}:
        raise ValueError("修改范围不正确")
    if not feedback.strip():
        raise ValueError("请填写需要调整的地方")
    revision = int(item.get("revision") or 0) + 1
    batch_store.set_item_milestone(batch_id, item_id, "review", status="pending")
    _set_item(
        batch_id,
        item_id,
        status="revising",
        stage="prepare",
        revision=revision,
        revisionFeedback=feedback.strip(),
        revisionMode=mode,
        error=None,
    )
    return batch_store.update(
        batch_id,
        status="running",
        stage="prepare",
        runnerActive=False,
        notice="正在按你的修改意见调整候选结果。",
    )


# ---------------------------------------------------------------------------
# 「所有任务完成后自动关机」
#
# 用户 2026-09-15：「在批量制作那边可以帮我加个开关吗 是否所有任务完成后关机 …
# 默认关闭」。设计取「安全优先」：
#
# * **什么时候才关**：批次里**没有任何条目还在推进**（排队 / 等你确认 / 已放行待出片 /
#   出片中 / 重新备料都算没完成），并且**至少有一条真的出完了片**、本进程也没有任何
#   runner / 出片 / 备料任务在跑。所以「有 3 条还在等你确认」不会把机器关掉，跑完的
#   那一刻才会。
# * **不会突然黑屏**：先由 Windows 自己倒计时（默认 60 秒，`H3_SHUTDOWN_DELAY_SECONDS`
#   可调），页面顶部同时显示倒计时和「取消关机」。
# * **倒计时期间也能反悔**：看护任务每秒级复查（每 10 秒一跳），只要又冒出新任务（用户
#   追加链接、又确认了一条）或者关掉了开关，立刻 `shutdown /a` 撤销。
# * `H3_AUTO_SHUTDOWN=0` 是硬开关：这台机器永不自动关机（测试与「不想被关」时用）。
# ---------------------------------------------------------------------------

SHUTDOWN_FLAG = "shutdownOnComplete"
# 把这些状态视为「这一条已经结束、不需要再等它」；只要还有别的状态就是不完整。
SHUTDOWN_DONE_STATUSES = {"completed", "skipped", "failed", "deleted"}
SHUTDOWN_POLL_SECONDS = 10.0
SHUTDOWN_DEFAULT_DELAY_SECONDS = 60
SHUTDOWN_MIN_DELAY_SECONDS = 10
SHUTDOWN_MAX_DELAY_SECONDS = 3600
SHUTDOWN_DISABLED_VALUES = {"0", "false", "no", "off", "disabled"}
# 关机提示语由 Windows 弹窗显示，非 ASCII 在无控制台的子进程里容易变乱码，固定用英文。
SHUTDOWN_COMMENT = "H3-MotionStudio: all batch tasks finished, shutting down."
PENDING_SHUTDOWN_PATH = DATA_DIR / "pending-shutdown.json"

_shutdown_task: asyncio.Task | None = None


def shutdown_disabled() -> bool:
    """硬开关：`H3_AUTO_SHUTDOWN=0` 时这台机器永不自动关机。"""
    return str(env_value("H3_AUTO_SHUTDOWN") or "").strip().lower() in SHUTDOWN_DISABLED_VALUES


def shutdown_delay_seconds() -> int:
    """关机前的倒计时秒数（留出按「取消关机」的时间），默认 60 秒。"""
    raw = str(env_value("H3_SHUTDOWN_DELAY_SECONDS") or "").strip()
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = SHUTDOWN_DEFAULT_DELAY_SECONDS
    return max(SHUTDOWN_MIN_DELAY_SECONDS, min(SHUTDOWN_MAX_DELAY_SECONDS, value))


def _read_pending_shutdown() -> dict[str, Any] | None:
    """已经排好的关停（`data/pending-shutdown.json`）。文件没了就返回 None。"""
    try:
        record = json.loads(PENDING_SHUTDOWN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not record.get("executeAt"):
        return None
    return record


def _write_pending_shutdown(record: dict[str, Any] | None) -> None:
    try:
        if record is None:
            PENDING_SHUTDOWN_PATH.unlink(missing_ok=True)
            return
        PENDING_SHUTDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_SHUTDOWN_PATH.write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        logger.warning("自动关机状态写入失败", exc_info=True)


def shutdown_unfinished_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    """还没处理完的条目（有它们就不关机）。"""
    return [
        item
        for item in state.get("items") or []
        if str(item.get("status") or "") not in SHUTDOWN_DONE_STATUSES
    ]


def batch_ready_for_shutdown(state: dict[str, Any]) -> bool:
    """这个批次是不是「真的全部做完了」——只有它为真才会排关机。"""
    if not state.get(SHUTDOWN_FLAG):
        return False
    items = state.get("items") or []
    if not items or shutdown_unfinished_items(state):
        return False
    # 至少有一条真的出完片：全是「失败 / 跳过」时更像是出了故障，不关机器（用户回来还要处理）。
    return any(str(item.get("status") or "") == "completed" for item in items)


def work_in_flight() -> bool:
    """本进程里还有没有在跑的东西（runner / 出片 / 备料 / 补交成片）。"""
    return bool(_RUNNING_BATCHES or _VIDEO_TASKS or _ITEM_TASKS or _SALVAGE_TASKS)


def _flagged_states() -> list[dict[str, Any]]:
    try:
        return batch_store.flagged_for_shutdown()
    except Exception:  # noqa: BLE001 - 看护任务不能因为一次读库失败就崩
        logger.warning("读取「自动关机」批次失败", exc_info=True)
        return []


def _no_window_kwargs() -> dict[str, Any]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flags} if flags else {}


def launch_shutdown(seconds: int) -> bool:
    """交给 Windows 自己倒计时关机（这期间 `shutdown /a` 可以撤销）。"""
    args = [
        shutil.which("shutdown") or "shutdown",
        "/s",
        "/t",
        str(int(seconds)),
        "/c",
        SHUTDOWN_COMMENT,
    ]
    try:
        subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_no_window_kwargs()
        )
    except OSError:
        logger.warning("调用系统关机失败", exc_info=True)
        return False
    return True


def abort_shutdown() -> bool:
    """撤销系统已经排好的关停（没有在倒计时时返回 False）。"""
    try:
        result = subprocess.run(
            [shutil.which("shutdown") or "shutdown", "/a"],
            capture_output=True,
            text=True,
            timeout=15,
            **_no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def shutdown_status() -> dict[str, Any]:
    """给页面的只读状态：有没有在倒计时、还剩多少秒、哪个批次触发的。"""
    record = _read_pending_shutdown()
    base: dict[str, Any] = {
        "pending": False,
        "disabled": shutdown_disabled(),
        "delaySeconds": shutdown_delay_seconds(),
    }
    if not record:
        return base
    execute_at = float(record.get("executeAt") or 0)
    base.update(
        {
            "pending": True,
            "batchId": record.get("batchId"),
            "seconds": record.get("seconds"),
            # 时间戳交给页面自己算，倒计时才能每秒跳（服务端时钟与本机一致）
            "executeAt": execute_at,
            "secondsLeft": max(0.0, execute_at - time.time()),
        }
    )
    return base


def schedule_shutdown(state: dict[str, Any]) -> dict[str, Any]:
    """排一次自动关机（已经排过就不重复排）。"""
    existing = _read_pending_shutdown()
    if existing:
        return existing
    seconds = shutdown_delay_seconds()
    now = time.time()
    if not launch_shutdown(seconds):
        return {}
    record = {
        "batchId": str(state.get("id") or ""),
        "seconds": seconds,
        "scheduledAt": now,
        "scheduledAtIso": now_iso(),
        "executeAt": now + seconds,
    }
    _write_pending_shutdown(record)
    batch_id = str(state.get("id") or "")
    if batch_id:
        batch_store.update(
            batch_id,
            notice=f"全部条目已完成，{seconds} 秒后自动关机；点「取消关机」可以撤销。",
        )
    logger.info("批量任务已全部完成，将在 %s 秒后自动关机", seconds)
    return record


def cancel_pending_shutdown(*, notice: str | None = None) -> dict[str, Any]:
    """撤销自动关机（用户点「取消关机」，或看护任务发现又有新任务）。"""
    record = _read_pending_shutdown()
    if not record:
        return {"pending": False, "cancelled": False}
    _write_pending_shutdown(None)
    aborted = abort_shutdown()
    batch_id = str(record.get("batchId") or "")
    if notice and batch_id:
        try:
            batch_store.update(batch_id, notice=notice)
        except KeyError:
            pass
    logger.info("自动关机已取消（abort=%s）", aborted)
    return {"pending": False, "cancelled": True, "batchId": batch_id or None}


def _clear_shutdown_flag(batch_id: str) -> None:
    """把某个批次的自动关机开关关掉（记录不存在 / 批次早没了都当无事发生）。"""
    if not batch_id:
        return
    try:
        state = batch_store.get(batch_id)
        if state and state.get(SHUTDOWN_FLAG):
            batch_store.update(batch_id, **{SHUTDOWN_FLAG: False})
    except KeyError:
        pass


def _consume_pending_shutdown() -> None:
    """收掉「已经到点」的关停记录，并把触发它的那个批次的开关一并关掉。

    **必须连开关一起关**：只删记录的话，下一次启动时这条早已跑完的批次还在标记里，
    看护任务会再排一次关机 —— 用户第二天刚开机就又被关掉（设计时特意避开的坑）。
    """
    record = _read_pending_shutdown()
    _write_pending_shutdown(None)
    _clear_shutdown_flag(str((record or {}).get("batchId") or ""))


def shutdown_tick() -> str:
    """看护任务的一跳：返回 `idle` 表示没事可做（看护任务可以退出）。

    `idle` / `waiting`（还没跑完，继续等）/ `scheduled`（已在倒计时或刚排上）。
    """
    record = _read_pending_shutdown()
    if record:
        if float(record.get("executeAt") or 0) <= time.time():
            # Windows 那边已经到点执行（或被别的程序拦下）：收掉记录与开关，看护任务可以收工
            _consume_pending_shutdown()
            return "idle"
        # 倒计时期间又冒出新任务（追加链接 / 又确认了一条 / 关掉了开关）：立刻撤销，
        # 否则用户会看着机器在还有活没干完的时候关掉。
        if work_in_flight() or any(
            not batch_ready_for_shutdown(state) for state in _flagged_states()
        ):
            cancel_pending_shutdown(notice="检测到还有任务要处理，已取消自动关机。")
            return "waiting" if _flagged_states() else "idle"
        return "scheduled"
    if shutdown_disabled():
        return "idle"
    states = _flagged_states()
    if not states:
        return "idle"
    if work_in_flight():
        return "waiting"
    for state in states:
        if batch_ready_for_shutdown(state):
            schedule_shutdown(state)
            return "scheduled"
    return "waiting"


async def shutdown_watcher() -> None:
    """只负责「整批跑完了没有」：批次 runner 退出后由它把关机排上。"""
    while True:
        await asyncio.sleep(SHUTDOWN_POLL_SECONDS)
        try:
            result = shutdown_tick()
        except Exception:  # noqa: BLE001 - 看护任务绝不能因为一次异常就死掉
            logger.warning("自动关机看护失败", exc_info=True)
            continue
        if result == "idle":
            return


def ensure_shutdown_watcher() -> None:
    global _shutdown_task
    if _shutdown_task is not None and not _shutdown_task.done():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 没有事件循环（同步调用 / 单元测试）：不起看护任务，逻辑本身仍可单独测
        return
    _shutdown_task = asyncio.create_task(shutdown_watcher())


def resume_shutdown_watch() -> None:
    """本地服务重启后恢复看护。

    - 已经在倒计时的那一次照常看着（`data/pending-shutdown.json` 还在且没到点）：Windows 的
      倒计时不会因为后端重启而消失，重启后仍然要能撤销它；
    - 开关还开着、**还没跑完**的批次继续等它跑完（重启不影响「跑完就关」这个承诺）；
    - 开关开着但**早就跑完**的批次只把开关关掉，**绝不重新排一次关机** —— 否则第二天开机
      后看护任务会立刻把机器再关一次（这正是 `_consume_pending_shutdown` 要一起清开关的原因）。
    """
    if shutdown_disabled():
        return
    record = _read_pending_shutdown()
    if record:
        if float(record.get("executeAt") or 0) > time.time():
            ensure_shutdown_watcher()
        else:
            _consume_pending_shutdown()
    pending_work = False
    for state in _flagged_states():
        if batch_ready_for_shutdown(state):
            _clear_shutdown_flag(str(state.get("id") or ""))
        else:
            pending_work = True
    if pending_work:
        ensure_shutdown_watcher()


def set_batch_shutdown_on_complete(batch_id: str, enabled: bool) -> dict[str, Any]:
    """打开 / 关闭「所有任务完成后自动关机」（批次级，默认关闭）。"""
    state = batch_store.get(batch_id)
    if state is None:
        raise KeyError(batch_id)
    if enabled and shutdown_disabled():
        raise ValueError("本机已禁用自动关机（环境变量 H3_AUTO_SHUTDOWN=0）")
    if not enabled:
        # 关掉开关时把已经排好的关停一起撤销，避免「我明明关了它还是关了」
        record = _read_pending_shutdown()
        if record and str(record.get("batchId") or "") == batch_id:
            cancel_pending_shutdown(notice="已关闭「全部完成后自动关机」，本次关机已取消。")
    state = batch_store.update(batch_id, **{SHUTDOWN_FLAG: bool(enabled)})
    if enabled:
        ensure_shutdown_watcher()
        # 打开开关时批次可能早就跑完了（事后才开）：立刻评估一次，不用等下一个轮询
        try:
            shutdown_tick()
        except Exception:  # noqa: BLE001 - 评估失败不能影响开关本身
            logger.warning("自动关机评估失败", exc_info=True)
        state = batch_store.get(batch_id) or state
    return state
