from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

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
    full_auto_enabled,
    normalize_batch_ratio,
    publish_subdir,
)
from .store import now_iso, store
from .video_integrity import truncation_reason


logger = logging.getLogger("uvicorn.error")


IDENTITY_PATH = Path(r"E:\AI_Assets\PortraitIdentity\本人固定参考.png")
MANIFEST_PATH = Path(r"D:\EV\download_manifest.jsonl")
COVER_FONT = Path(r"C:\Windows\Fonts\msyhbd.ttc")
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
_RUNNING_BATCHES: set[str] = set()
# 每一批 runner 的「第几代」。用户点「重新开始 / 继续 / 准备任务」时由 `take_over_batch_run`
# 推进一代，**旧 runner 看到自己的代已经过时就自己退出**，新一代立刻接管。
#
# 为什么必须有这个：`run_batch` 开头那句 `if batch_id in _RUNNING_BATCHES: return` 是
# 静默 no-op —— 2026-09-16 实测用户点「批量重新开始」时旧 runner 正卡在等一个被堵死的
# 出片子任务上（它攥着 `_RUNNING_BATCHES` 不放），于是新 runner 一去就 return，
# 界面提示「已重新开始 7 条」，实际一条都没跑（批次停在 running + runnerActive=False，
# 条目全躺在 confirmed）。没有这个「换代」机制，用户唯一的出路是重启本地服务。
_RUN_GENERATION: dict[str, int] = {}
# 被换代顶掉的旧 runner 留下来的出片任务：留着强引用，免得它在跑的过程中被 GC 掉。
_STALE_VIDEO_TASKS: set[asyncio.Task] = set()
# `claim_batch_run` 占不到名额时的返回值（第 0 代是合法的，不能拿 0 当哨兵）。
RUN_CLAIM_BUSY = -1
# 每个条目当前在跑的预审协程（模型调用 + 本地出图），跳过/删除时据此安全取消。
_ITEM_TASKS: dict[str, asyncio.Task] = {}
# 跳过/取消后仍在后台跑的子任务：等它落定再把成片补进发布目录（避免任务被垃圾回收）。
_SALVAGE_TASKS: set[asyncio.Task] = set()
# 正在后台出片的条目任务。runner 不再原地 await 出片，而是把出片放进这里的任务，
# 自己继续给后面的条目备料；这个集合是**强引用**（否则任务可能在跑的过程中被回收）。
_VIDEO_TASKS: set[asyncio.Task] = set()
# 正在后台**备料**的条目任务（2026-09-20 起备料也不再内联在 runner 里）。同样留强引用：
# runner 只持有一个备料任务 + 一个出片任务，正好是用户要的「出片期间一条一条备料」。
_PREP_TASKS: set[asyncio.Task] = set()
# 每个批次的视频生成闸门：备料可以提前完成，但从“提交视频子任务”到“最终成片交付”
# 必须整段串行。即使用户连续确认多条、旧 runner 被换代，后来的条目也只能在这里等。
_BATCH_VIDEO_LOCKS: dict[str, asyncio.Lock] = {}
# 后台自动生成封面的任务（强引用）。封面生成走 ChatGPT 桌面端，由 _IMAGE_LOCK 保证单链。
_COVER_TASKS: set[asyncio.Task] = set()


def cancel_item_work(batch_id: str, item_id: str) -> bool:
    task = _ITEM_TASKS.get(f"{batch_id}:{item_id}")
    if not task or task.done():
        return False
    task.cancel()
    return True


async def _cancel_child_job_quietly(child_id: str, batch_id: str, item_id: str) -> None:
    """取消一个还挂在条目名下的出片子任务（失败只记日志，绝不往上抛）。"""
    del batch_id, item_id
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{BATCH_SELF_URL}/api/jobs/{child_id}/cancel")
        if response.status_code not in {200, 404, 409}:
            logger.warning("取消子任务 %s 返回 %s（继续收尾）", child_id, response.status_code)
    except Exception:
        logger.warning("取消子任务失败（忽略）：%s", child_id, exc_info=True)


def cancel_item_video_job(batch_id: str, item_id: str) -> str:
    """把一条条目名下**还在跑**的出片子任务真正取消掉，返回被取消的 job id（没有则空串）。

    2026-09-17 实测事故：条目被跳过、又被「重新开始」，而它上一轮的出片子任务其实还活着
    （取消只写了个状态、没真的中断它），重置时又把 `videoJobId` 一清 —— 那个任务就此变成
    谁都管不到的孤儿：攥着 `pipeline_lock` 继续烧显卡，页面则永远停在「排队中」，
    而 ComfyUI 里跑的偏偏是它。凡是「重置 / 跳过 / 删除一条」的地方，都要在清指针
    **之前**先调这里。
    """
    try:
        item = _item(batch_id, item_id)
    except Exception:
        return ""
    child_id = str(item.get("videoJobId") or (item.get("childJob") or {}).get("id") or "")
    if not child_id:
        return ""
    child = store.get(child_id) or {}
    if child.get("status") not in {"queued", "running", "cancelling"}:
        return ""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 没有事件循环（同步调用 / 关停中）：留给重启后的僵尸清理
        return ""
    task = loop.create_task(_cancel_child_job_quietly(child_id, batch_id, item_id))
    _SALVAGE_TASKS.add(task)
    task.add_done_callback(_SALVAGE_TASKS.discard)
    return child_id


def batch_run_alive(batch_id: str) -> bool:
    """这一批是不是已经有一个 runner 在跑（他才会把批次推进下去）。"""
    return batch_id in _RUNNING_BATCHES


def claim_batch_run(batch_id: str) -> int:
    """占住这一批的 runner 名额，返回本次运行的「代」。

    已经被别的 runner 占着时返回 `RUN_CLAIM_BUSY`（调用方直接退出，别做任何事）——
    路由层的 `take_over_batch_run` 会先把旧 runner 顶掉，所以正常路径不会走到这里。
    注意第 0 代是**合法**的（批次第一次跑就是第 0 代），别拿 0 当「占不到」的哨兵。
    """
    if batch_id in _RUNNING_BATCHES:
        return RUN_CLAIM_BUSY
    _RUNNING_BATCHES.add(batch_id)
    return int(_RUN_GENERATION.get(batch_id) or 0)


def take_over_batch_run(batch_id: str) -> int:
    """把这一批交给**新的一代** runner 跑（用户点了开始 / 继续 / 重新开始）。

    推进代数 + 放掉旧 runner 占的名额，这样紧接着 spawn 的 `run_batch` 一定跑得起来；
    旧 runner（可能正卡在等一个出片子任务上）在下一个循环边界看到自己的代过时就自己退出。
    """
    generation = int(_RUN_GENERATION.get(batch_id) or 0) + 1
    _RUN_GENERATION[batch_id] = generation
    _RUNNING_BATCHES.discard(batch_id)
    return generation


def batch_run_superseded(batch_id: str, generation: int) -> bool:
    """我这一代是不是已经被顶掉了（被顶掉的 runner 必须**立刻停止一切推进**）。

    只认代数、**不认 `_RUNNING_BATCHES`**：新一代 claim 之后这个集合里也有同一个 id，
    照它判断会把新 runner 自己也当成过期的。
    """
    return int(_RUN_GENERATION.get(batch_id) or 0) > int(generation or 0)


def finish_batch_run(batch_id: str, generation: int) -> None:
    """runner 收尾：只有**还属于自己这一代**时才放掉名额与 `runnerActive`。

    被顶掉的旧 runner 什么都不许动 —— 否则它会把新一代刚设好的 `runnerActive=True`
    抹成 False，界面又变回「看着在跑、实际没人跑」。
    """
    if batch_run_superseded(batch_id, generation):
        return
    _RUNNING_BATCHES.discard(batch_id)
    state = batch_store.get(batch_id)
    if state and state.get("runnerActive"):
        batch_store.update(batch_id, runnerActive=False)


def absorb_stale_video_task(task: asyncio.Task) -> None:
    """接手被顶掉的 runner 遗留的出片任务（只留强引用，不再管它的收尾）。"""
    _STALE_VIDEO_TASKS.add(task)
    task.add_done_callback(_STALE_VIDEO_TASKS.discard)


def _hand_over_stale_video_task(
    batch_id: str, video_task: asyncio.Task | None, video_row: dict[str, Any] | None
) -> None:
    """被顶掉的那一代 runner 退出前，把还在跑的出片任务交代清楚（记一条日志 + 留强引用）。

    **不去 cancel 它**：这一条可能正是用户「重新开始」要重跑的那一条，取消会连带把
    新一代刚建好的子任务也搅乱；留着强引用让它自己落定即可（新一代按条目当前状态推进）。
    """
    if video_task is None or video_task.done():
        return
    absorb_stale_video_task(video_task)
    item_id = str((video_row or {}).get("id") or "")
    if item_id:
        batch_store.add_item_log(
            batch_id, item_id, "本条之前的出片任务已交给新一轮接管（旧的那一轮已退出）。"
        )


def _cancel_stale_prep_task(
    batch_id: str, prep_task: asyncio.Task | None, prep_row: dict[str, Any] | None
) -> None:
    """被顶掉的那一代 runner 退出前，把还在跑的**备料**任务收干净。

    与出片任务相反，备料任务**必须取消**：它跑在后台、结果只落在这一条自己身上，留着它
    会让条目永远停在 `running`（新一代看它是 running 就不会再挑它，等于永久卡住）。
    备料本身是幂等的（重来一遍即可，半截结果不会被登记），所以取消代价最小。
    """
    item_id = str((prep_row or {}).get("id") or "")
    if prep_task is None or prep_task.done():
        return
    cancel_item_work(batch_id, item_id)
    if not item_id:
        return
    state = batch_store.get(batch_id) or {}
    current = next((row for row in state.get("items") or [] if row.get("id") == item_id), None)
    if current and str(current.get("status")) == "running":
        _set_item(batch_id, item_id, status="pending", stage="queued", finishedAt=None)
        for milestone in current.get("milestones") or []:
            if milestone.get("status") == "running":
                batch_store.set_item_milestone(
                    batch_id, item_id, milestone["id"], status="pending", progress=0, currentNode=None
                )
    batch_store.add_item_log(
        batch_id, item_id, "本条之前的备料已交给新一轮接管（旧的那一轮已退出）。"
    )


def _skip_duplicate_item(batch_id: str, item_id: str, message: str) -> None:
    """同一条作品已经在批次里：标成「已跳过」，不当失败，也不拦住后面的条目。"""
    current = _item(batch_id, item_id)
    _set_item(
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
            batch_store.set_item_milestone(batch_id, item_id, milestone["id"], status="skipped")
    batch_store.add_item_log(batch_id, item_id, message)


def _running_notice(
    item: dict[str, Any], state: dict[str, Any], other: dict[str, Any] | None
) -> str:
    """runner 的运行提示：正在出片 / 正在备料，两个并行时把另一条也说出来。"""
    total = state.get("total") or len(state.get("items") or [])
    preparing = str(item.get("status")) in {"pending", "revising"}
    head = (
        f"正在备料第 {item.get('index')} / {total} 条。"
        if preparing
        else f"正在出片第 {item.get('index')} / {total} 条。"
    )
    if other is None or str(other.get("id")) == str(item.get("id")):
        return head
    other_word = "备料" if preparing else "出片"
    return f"{head[:-1]}（同时在{other_word}第 {other.get('index')} 条）。"


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

# 发布目录里「必须有」的图片（用户 2026-09-16：「点击重新开始的时候要检查发布成品是否
# 人物图.png 和 封面_抖音3x4.png 和 封面_B站4x3.png 是否完整 如果不完整看下是不是需要补充」）。
# ⚠️ 封面名字有两代：现在的 `B站4x3.png` / `抖音3x4.png`（`image_workbench.save_upload` 写的）
# 与早期的 `封面_B站4x3.png` / `封面_抖音3x4.png`。体检必须**两代都认**，否则新目录永远报「缺封面」。
PUBLISH_IMAGE_NAME = "人物图.png"
PUBLISH_COVER_NAMES = ("B站4x3.png", "抖音3x4.png")
LEGACY_PUBLISH_COVER_NAMES = ("封面_B站4x3.png", "封面_抖音3x4.png")
PUBLISH_COVER_ALIASES = {
    "bilibili": ("B站4x3.png", "封面_B站4x3.png"),
    "douyin": ("抖音3x4.png", "封面_抖音3x4.png"),
}
# 源视频抽帧（图一）也复制一份进发布目录，方便直接拿去出图（用户 2026-09-16 要求）
PUBLISH_FRAME_NAME = "抽帧.jpg"


def new_item(
    kind: str,
    url: str,
    ratio: str,
    index: int,
    created: str,
    kind_index: int = 0,
) -> dict[str, Any]:
    """新建一个批次条目。新建批次与「随时追加」共用，保证字段一致。

    `kindIndex`（2026-09-20 用户：「唱歌的序号和跳舞的序号都是独立的」）：**按类型各自编号**，
    创建时分配、之后永不变（拖动换位也不会改），删除也不回收 —— 发布目录名用它，
    所以两种类型各自从 001 起。
    """
    return {
        "id": uuid.uuid4().hex[:12],
        "index": index,
        "kindIndex": int(kind_index or index),
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
    kind_starts: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """按顺序建条目：全局 `index` 接着往下排，`kindIndex` **按类型各自接着排**。"""
    items: list[dict[str, Any]] = []
    index = start_index
    counters = {str(key): int(value) for key, value in (kind_starts or {}).items()}
    for kind, url in rows:
        index += 1
        number = counters.get(kind, 0) + 1
        counters[kind] = number
        items.append(new_item(kind, url, defaults[kind], index, created, number))
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
        kind_starts: dict[str, int] = {}
        for item in items:
            kind = str(item.get("kind") or "")
            # 老条目没有 kindIndex：拿它的全局 index 当起点，保证新条目的类型编号接着老的不撞号
            number = int(item.get("kindIndex") or item.get("index") or 0)
            kind_starts[kind] = max(kind_starts.get(kind, 0), number)
        added = _build_items(fresh, defaults, next_index, created, kind_starts)
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
                # **落地就把请求旗标清掉**：`skipRequested` 是「请 runner 去取消」的请求，
                # 条目已经不在跑了，旗标留着就成了定时炸弹 —— 2026-09-16 实测：
                # 全部「重新开始」→ 恢复运行时 `run_batch` 先看到 `skipRequested`，
                # 把每一条都当「取消中的条目」直接丢弃，整批瞬间变成「全部条目已完成」，
                # ComfyUI 一次都没被叫起来（用户：「点击全部重新开始之后没有运行 comfyui」）。
                row.update(
                    status="skipped",
                    stage="skipped",
                    finishedAt=now_iso(),
                    childJob=None,
                    skipRequested=False,
                )
                for milestone in row.get("milestones") or []:
                    if milestone.get("status") in {"pending", "running"}:
                        milestone["status"] = "skipped"

            batch_store.mutate_item(batch_id, item_id, mark_skipped)
            marked.append(item_id)
        else:
            busy.append(item_id)
            # **总是**直接收尾，不再依赖 runner 是否活着。
            # 实测两次踩坑：① 批次因别的条目失败而 failed、runner 已退出；
            # ② 批次处于 paused、runner 也不在 —— 两种情况下 skipRequested 都没人处理，
            # 条目永远卡在 running，用户看到的现象就是「点击取消没有效果」。
            # runner 若也在处理，两条路都收敛到 skipped，重复执行无副作用。
            _spawn_orphan_abandon(batch_id, item_id, deleted=False)
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
                # 同 `mark_items_skipped`：落地即清掉请求旗标，否则「重新开始」之后
                # runner 还会把它当成待取消的条目丢掉。
                row.update(
                    status="deleted",
                    stage="deleted",
                    finishedAt=now_iso(),
                    childJob=None,
                    deleteRequested=False,
                )
                for milestone in row.get("milestones") or []:
                    if milestone.get("status") in {"pending", "running"}:
                        milestone["status"] = "skipped"

            batch_store.mutate_item(batch_id, item_id, mark_deleted)
            marked.append(item_id)
        else:
            busy.append(item_id)
            # 同 mark_items_skipped：**总是**直接收尾（runner 退出或批次 paused 时都没人做）
            _spawn_orphan_abandon(batch_id, item_id, deleted=True)
    return {"marked": marked, "busy": busy, "rejected": rejected}


def _adopt_published_image_for_restart(
    batch_id: str, item_id: str, item: dict[str, Any]
) -> Path | None:
    """重新开始之前的兜底收编：候选图没了、但发布目录里还有出好的人物图 → 复制进条目目录。

    用户 2026-09-20：「**对已经有的内容重新点击不需要重新出图**」。候选图落在
    `data/batches/...` 里有被清理掉的可能，而发布目录那张一直在 —— 不先收编就会因为
    「条目没有候选图」退回备料（有重新出图的风险）。
    """
    work = DATA_DIR / "batches" / batch_id / item_id
    revision = int(item.get("revision") or 0)
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return _adopt_published_image(batch_id, item_id, item, work, revision)


def restart_item_to_run(batch_id: str, item_id: str) -> bool:
    """把一条**已经结束**的条目重置成可以重跑的状态，返回它是否直接续跑。

    失败 / 已跳过 / 已出片共用的重置逻辑（单条「重试 / 重新开始」与批量「重新开始」
    必须走同一份代码，否则两边的行为会慢慢跑偏）：

    - **已经有内容**（源视频在磁盘上 + 有候选人物图，条目里没有了就先把发布目录那张
      收编进来）→ 回到 `confirmed`，**只重跑出片**，不重新下载/备料、更不会重新出图；
    - 还没备齐料、或者源视频已经被清理掉 → 回到 `pending`，从下载与备料重新走一遍
      （2026-09-14 实测：源文件不在时直接续跑会在提交时报「文件不存在」）；
    - 清掉 skip/delete 请求、错误、上一次的子任务与发布文件记录，并把 `video`（以及任何
      报错的）里程碑重置为待办，页面上的流程重新变回待办而不是停在 ✗；
    - 清掉 `revisionFeedback` / `revisionMode`：那是**上一次**「只调图片 / 只调文案」的意图，
      不该跨过一次重新开始继续生效 —— `revisionMode="image"` 残留会让备料这条路
      **强制重出人物图**（用户 2026-09-20 报的就是「重新点击又给我重新出图」）。
    """
    box: dict[str, bool] = {}

    # 有候选图就直接续跑；没有但发布目录里有出好的那张 → 先收编，同样是"只重跑出片"。
    # 这两件事要读磁盘/发布目录，放在改状态之前做。
    pending_item = next(
        (
            item
            for item in (batch_store.get(batch_id) or {}).get("items") or []
            if item.get("id") == item_id
        ),
        None,
    )
    adopted: Path | None = None
    candidate = Path(str((pending_item or {}).get("ai", {}).get("reference_image_path") or ""))
    if pending_item is not None and not candidate.is_file():
        adopted = _adopt_published_image_for_restart(batch_id, item_id, pending_item)

    def reset(row: dict[str, Any]) -> None:
        ai = row.get("ai") or {}
        if adopted is not None:
            ai = {**ai, "reference_image_path": str(adopted)}
            row["ai"] = ai
        approved = (
            Path(str(row.get("sourcePath") or "")).is_file()
            and Path(str(ai.get("reference_image_path") or "")).is_file()
        )
        box["approved"] = approved
        # **保留发布目录的记录**：`publish_folder()` 按 `outputs.folder` 认目录，
        # 一清空就只能靠标题去猜，标题又常常变过 —— 一条作品会被指到另一个目录去
        # （2026-09-16 检查发布成品时发现的：人物图/封面会补到错的文件夹）。
        # 其余记录（成片、文案路径）确实该作废，出片后 `_deliver` 会重新写。
        previous = row.get("outputs") or {}
        keep = {
            key: str(previous[key])
            for key in ("folder", "image")
            if str(previous.get(key) or "").strip()
        }
        row.update(
            status="confirmed" if approved else "pending",
            stage="confirmed" if approved else "queued",
            error=None,
            warning=None,
            skipRequested=False,
            deleteRequested=False,
            childJob=None,
            videoJobId=None,
            outputs=keep,
            finishedAt=None,
            revisionFeedback="",
            revisionMode="",
            videoAttempts=0,        # 手动重新开始：把「出片失败自动重试」的额度也还回来
        )
        for milestone in row.get("milestones") or []:
            if milestone.get("id") == "video" or milestone.get("status") == "error":
                milestone.update(status="pending", progress=0, currentNode=None, finishedAt=None)

    # 上一轮的出片子任务若还活着（取消没真的生效过一次），必须**在清指针之前**掐掉它，
    # 否则它会变成谁也管不到的孤儿，继续攥着流水线烧显卡。
    cancel_item_video_job(batch_id, item_id)
    batch_store.mutate_item(batch_id, item_id, reset)
    return bool(box.get("approved"))


def restart_item_log(direct: bool) -> str:
    """「重新开始」写进条目日志的那句话（单条与批量共用，免得两边跑偏）。

    用户 2026-09-20：「如果人物图 B站图片 抖音图片都有的话点击重新开始不要重新生成」——
    所以这句话必须写清「三张图沿用现有的」，这也是链路真实行为：直接续跑那条路
    （`confirmed`）根本不碰图片，回备料那条路也只会收编/沿用已有的图、只补缺的那些。
    """
    if direct:
        return "已重新开始这一条：人物图、B站封面、抖音封面全部沿用现有的，只重跑出片（不会重新出图）。"
    return (
        "已重新开始这一条，将从下载与备料重做；已经出好的人物图与封面会直接沿用，"
        "三张都在时不会重新出图（只补缺的那几张）。"
    )


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
        batch_store.add_item_log(batch_id, item_id, restart_item_log(direct))
        # 「重新开始」时先把发布成品**体检**一次：缺人物图 / 封面就把「缺什么」写进条目日志。
        # 只报告、不自动出图（2026-09-16 用户：「去除图片生成的自动化流程吧 只保留生成字幕的流程」）。
        try:
            _sync_publish_images(batch_id, item_id)
        except Exception:  # noqa: BLE001 - 只做体检，不能拦住重开
            logger.exception("重新开始时检查发布成品失败（忽略）")
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


def _park_truncated_source(path: Path) -> str | None:
    """半截下载的文件：改名搁置并返回原因；完整/探测不出返回 None。

    **必须挪走**：`_download()` 开头的 `existing.is_file()` 和 `cached_download_path()`
    都会先认本地文件，坏文件留在原地会让之后每次「重新开始」都复用它、原地复现同一个
    失败（2026-09-19 条目 012 就是这样白跑了五次）。不静默删除（用户素材），改名成
    `xxx.truncated.mp4` 并在搜索时排除。
    """
    reason = truncation_reason(path)
    if reason is None or not path.is_file():
        return None
    try:
        parked = path.with_name(f"{path.stem}.truncated{path.suffix}")
        counter = 1
        while parked.exists():
            parked = path.with_name(f"{path.stem}.truncated{counter}{path.suffix}")
            counter += 1
        try:
            path.replace(parked)
        except OSError:
            # 改名失败（被占用等）时**不能留着它**：调用方接下来必然要重新下载，
            # 而本地已有的坏文件会挡住下载。此时才走到删除这一步。
            path.unlink(missing_ok=True)
    except OSError as error:  # 挪不动也不该把整条链路拖死
        logger.warning("搁置不完整的源视频失败：%s（%s）", path, error)
    return reason


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
            if candidate.is_file() and ".truncated" not in candidate.name:
                return candidate
    for root in roots:
        matches = [
            path
            for path in root.rglob(f"*{aweme_id}*")
            if path.is_file()
            and path.suffix.lower() in VIDEO_SUFFIXES
            and ".h3-converted" not in path.name
            and ".part." not in path.name
            and ".truncated" not in path.name
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


async def _fetch_source_direct(item: dict[str, Any]) -> tuple[Path, str, dict[str, Any]]:
    """直连路由：分享页取 video_id → `aweme/v1/play` 换 CDN 直链 → 下载。

    这条现在是**主路由**：下载器 REST 取详情用的 `/aweme/v1/web/aweme/detail/` 被抖音
    Argus 门禁（2026-09-14 起），先走它每条都要白等 3 次重试（~17 秒）再起一个下载器进程。
    """
    from . import douyin_direct

    path, meta = await asyncio.to_thread(douyin_direct.fetch, str(item["url"]), DOUYIN_OUTPUT)
    aweme_id = str(meta["awemeId"])
    raw_reason = await asyncio.to_thread(_park_truncated_source, Path(path))
    if raw_reason is not None:
        # 先验原始下载文件再转码：转码会把「能解出来的那部分」重新封装成一个帧数自洽的
        # 短片，之后再比对帧数就查不出来了（音频也会跟着短一截）。
        raise RuntimeError(f"{raw_reason}（直连下载没有拿到完整文件）")
    source = await ensure_download_playable(Path(path), aweme_id)
    metadata = {
        "desc": meta.get("desc") or "",
        "tags": list(meta.get("tags") or []),
        "nickname": meta.get("nickname") or "",
        "source": "direct",
    }
    return source, aweme_id, metadata


async def _fetch_source_via_service(
    batch_id: str, item_id: str, item: dict[str, Any]
) -> tuple[Path, str, dict[str, Any]]:
    """下载器 REST 路由（拿得到就顺带写 metadata 清单，方便以后本地复用）。"""
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
    raw = Path(result["path"])
    raw_reason = await asyncio.to_thread(_park_truncated_source, raw)
    if raw_reason is not None:
        raise RuntimeError(f"{raw_reason}（下载器报告的「成功」其实是半截文件）")
    source = await ensure_download_playable(raw, aweme_id)
    return source, aweme_id, _manifest_metadata(aweme_id)


# 下载器对**所有**详情失败都回这一句，用户照着刷登录往往没用（真实原因是详情接口被
# Argus 门禁 403，或分享页被换成壳页），所以这句话出现时要把真实原因补上。
DOWNLOADER_COOKIE_HINT = "作品详情或媒体下载失败"


def douyin_download_failure_note(
    direct: Exception | None, service: Exception | None
) -> str:
    """下载路由失败时给**能定位**的报错（别再只甩一句「Cookie 可能已失效」）。

    2026-09-16 用户实测：又看到这句「Cookie 可能已失效，或当前请求触发了抖音风控」，
    但真正的原因是我们自己的分享页正则写死了 `v0` 前缀，解析不出 video_id。

    2026-09-20 起默认只走下载器 REST（`direct=None`），所以抬头按**实际试过的路由**拼，
    不再写死「直连与下载器两条路由都没成功」。
    """
    parts: list[str] = []
    attempted: list[str] = []
    if direct is not None:
        attempted.append("直连")
        parts.append(f"直连路由：{str(direct).strip() or type(direct).__name__}")
    if service is not None:
        attempted.append("下载器")
        text = str(service).strip() or type(service).__name__
        if DOWNLOADER_COOKIE_HINT in text:
            text = f"{text}〔下载器取的 /aweme/v1/web/aweme/detail/ 被抖音 Argus 风控门禁〕"
        parts.append(f"下载器路由：{text}")
    detail = "；".join(parts) or "原因未知"
    routes = "与".join(attempted) or "全部"
    return (
        f"抖音下载失败（{routes}路由都没成功）。{detail}。"
        "请先在抖音里确认这条作品能正常播放；仍失败可稍后在批量里点「重新开始」重试本条。"
    )


async def _download(batch_id: str, item_id: str) -> Path:
    item = _item(batch_id, item_id)
    existing = Path(item.get("sourcePath") or "")
    if existing.is_file():
        # ⚠️ 登记过的 sourcePath 也要验一遍：半截文件留在这里，「重新开始」只会原地复现。
        reason = await asyncio.to_thread(_park_truncated_source, existing)
        if reason is None:
            return existing
        batch_store.add_item_log(
            batch_id, item_id, f"上次登记的源视频不完整（{reason}），已搁置坏文件，重新下载本条。"
        )
        _set_item(batch_id, item_id, sourcePath="")
    # 这条作品本地已经有了（之前下过、或另一条用过同一条素材）→ 直接用，不启动下载器
    cached = await asyncio.to_thread(cached_download_path, str(item.get("url") or ""))
    if cached is not None:
        reason = await asyncio.to_thread(_park_truncated_source, cached)
        if reason is not None:
            batch_store.add_item_log(
                batch_id, item_id, f"本地已有的源视频不完整（{reason}），已搁置坏文件，改为重新下载。"
            )
            cached = None
    if cached is not None:
        batch_store.set_item_milestone(batch_id, item_id, "download", status="running", progress=5)
        _set_item(batch_id, item_id, stage="download", status="running", error=None)
        return await _adopt_existing_source(batch_id, item_id, cached)
    batch_store.set_item_milestone(batch_id, item_id, "download", status="running", progress=5)
    _set_item(batch_id, item_id, stage="download", status="running", error=None)
    batch_store.add_item_log(batch_id, item_id, "正在下载抖音源视频……")
    direct_failure: Exception | None = None
    service_failure: Exception | None = None
    # 下载路由（用户 2026-09-20：「直连 CDN（断流自动续传）不要这个 直接用 REST」）：
    # **默认只走下载器 REST**。直连那条（分享页 → aweme/v1/play → CDN，`_fetch_source_direct`）
    # 代码保留，但要显式 `H3_DOUYIN_DIRECT=1` 才启用 —— 启用后它作为首选、失败再回退 REST。
    prefer_direct = (env_value("H3_DOUYIN_DIRECT") or "").strip().lower() in {"1", "true", "on", "yes"}
    try:
        if prefer_direct:
            try:
                source, aweme_id, metadata = await _fetch_source_direct(item)
                batch_store.add_item_log(batch_id, item_id, "直连下载成功（分享页 → aweme/v1/play → CDN）。")
            except Exception as error:  # noqa: BLE001 - 直连失败就换 REST
                direct_failure = error
                batch_store.add_item_log(
                    batch_id, item_id,
                    f"直连下载没有成功（{error}），改用下载器 REST 再试……",
                )
                try:
                    source, aweme_id, metadata = await _fetch_source_via_service(batch_id, item_id, item)
                    batch_store.add_item_log(batch_id, item_id, "下载器 REST 下载成功。")
                except Exception as service_error:  # noqa: BLE001 - 两条都失败才报错
                    service_failure = service_error
                    raise RuntimeError(
                        douyin_download_failure_note(direct_failure, service_failure)
                    ) from service_error
        else:
            # 默认路径：只走下载器 REST（它同时会写 metadata 清单，方便以后本地复用）
            try:
                source, aweme_id, metadata = await _fetch_source_via_service(batch_id, item_id, item)
                batch_store.add_item_log(batch_id, item_id, "下载器 REST 下载成功。")
            except Exception as error:  # noqa: BLE001
                service_failure = error
                raise RuntimeError(douyin_download_failure_note(None, service_failure)) from error

        # 转码兜底：两条路由都已验过**原始**下载文件，这里再验一遍最终要送进 ComfyUI 的
        # 那一份（转码/换封装出问题时同样拦下来，别等 Mie 循环跑到第 3 段才报 No frames）。
        final_reason = await asyncio.to_thread(_park_truncated_source, source)
        if final_reason is not None:
            raise RuntimeError(f"{final_reason}。坏文件已移走，请点「重新开始」重新下载本条。")

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


def _frame_detail_score(data: bytes) -> float:
    """Return a lightweight sharpness score without adding a CV-model dependency.

    The scene frame becomes the image-generation reference. A frame with
    motion-smeared hands can carry that defect into the generated portrait, so
    favour the frame with the clearest local edges.
    """
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("L")
    image.thumbnail((480, 480), Image.Resampling.LANCZOS)
    edges = image.filter(ImageFilter.FIND_EDGES)
    return float(ImageStat.Stat(edges).var[0])


def _extract_frame_bytes(source: Path, timestamp: float, *, flags: int) -> bytes | None:
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(source),
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ],
        capture_output=True,
        timeout=120,
        creationflags=flags,
    )
    return result.stdout if result.returncode == 0 and result.stdout else None


def extract_scene_frame(source: Path, target: Path, ratio: float = 0.38) -> Path:
    """Choose a clear scene frame and reject a hand-motion-blurred timestamp.

    A single fixed timestamp frequently lands on a gesture. Sampling nearby
    moments and retaining the sharpest frame prevents such frames becoming an
    image-generation reference.
    """
    duration = _video_duration_seconds(source)
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    ratios = (ratio, ratio - 0.12, ratio + 0.12, ratio - 0.06, ratio + 0.06)
    candidates: list[tuple[float, bytes]] = []
    for candidate_ratio in dict.fromkeys(max(0.04, min(0.96, value)) for value in ratios):
        frame = _extract_frame_bytes(source, duration * candidate_ratio, flags=flags)
        if frame:
            candidates.append((_frame_detail_score(frame), frame))
    if not candidates:
        raise RuntimeError("从源视频抽帧失败，无法生成候选人物图")
    target.write_bytes(max(candidates, key=lambda candidate: candidate[0])[1])
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


async def _run_prep(batch_id: str, item_id: str) -> None:
    """备料一条：下载 → 预审（分析 + 出图素材 + 候选图 + 文案）。

    2026-09-20 起**备料跑在后台任务里**（以前是内联在 runner 循环里 `await`，一跑就把循环
    卡住，出片期间没法继续备下一条）。用户口径：「第一条备好之后优先出片，出片的过程中
    一条一条备料」——所以 runner 最多同时持有一个备料任务 + 一个出片任务（**并发 1**）。
    """
    await _download(batch_id, item_id)
    refreshed = _item(batch_id, item_id)
    if refreshed.get("skipRequested") or refreshed.get("deleteRequested"):
        raise asyncio.CancelledError
    feedback = str(refreshed.get("revisionFeedback") or "")
    mode = str(refreshed.get("revisionMode") or "both")
    await _prepare_review(batch_id, item_id, feedback=feedback, mode=mode)


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
    locked_copy = locked_publish_copy(item)
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

    # 封面已经出好的条目：**文案锁住，一个字都不许改**
    # （用户 2026-09-19：「都用原来的啊 不然封面都得改」；见 `locked_publish_copy`）。
    if locked_copy:
        changed = [
            key for key in ("title", "introduction", "tags")
            if locked_copy.get(key) and result.get(key) != locked_copy[key]
        ]
        for key in ("title", "introduction", "tags"):
            if locked_copy.get(key):
                result[key] = locked_copy[key]
        if changed:
            batch_store.add_item_log(
                batch_id, item_id,
                "发布目录里已经出过封面，沿用原来的发布文案（"
                + "、".join(changed) + " 不许重写，否则封面就得重做）。",
            )

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

    # 已经有候选人物图就直接沿用：全自动链路里「重新开始」不该再烧一次出图钱
    # （要重出图走 /images 页面的「重新生成这一步」，那是明确的意图）。
    if provider in {"api", "local"} and mode != "image" and previous_image.is_file():
        image_path = previous_image
        batch_store.add_item_log(
            batch_id, item_id, "这条已经有候选人物图，直接沿用（没有重复出图）。"
        )
    # 本条没有候选图、但**发布目录里已经有一张出好的人物图** → 直接收编，不重出
    # （用户 2026-09-19：「对于已经有的不要重复得去生成」）。这类图是 `/images` 页面直接
    # 生成、只落进发布目录的，以前既会被白烧一次出图、又会因为「条目没有候选图」在下次
    # 刷新发布目录物料时被当成空的。
    elif mode != "image" and provider in {"api", "local"} and (
        adopted := _adopt_published_image(batch_id, item_id, item, work, revision)
    ) is not None:
        image_path = adopted
    elif mode == "copy" and previous_image.is_file():
        image_path = previous_image
    elif provider == "manual":
        if previous_image.is_file():
            # 手动模式永远不自动出图：已有成图就保留，用户上传新图即为替换。
            image_path = previous_image
        else:
            if previous_image.is_file():
                pass  # 已有成图，什么都不用说
            else:
                try:
                    rel = Path("data") / work.relative_to(DATA_DIR)
                except ValueError:
                    rel = work
                batch_store.add_item_log(
                    batch_id,
                    item_id,
                    "已备好出图素材（自动出图已关闭）："
                    f"提示词 {rel / '出图提示词.txt'}；"
                    f"图一（源视频抽帧）{rel / 'scene-frame.jpg'}；"
                    f"图二（身份参考）{IDENTITY_PATH}。"
                    "拿这三样去出图，出了把图上传回这一条即可。",
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
            # **显式记一条「请求已发出」**：中转站同尺寸也可能 40 秒到 4 分钟不等
            # （实测 019 56s / 023 62s / 020 251s），用户看不到「开始了没有」会以为卡住
            # （2026-09-19：「怎么确定发送了请求 感觉有的时候快有的时候慢 我都不知道开始了没有」）。
            image_started = time.monotonic()
            if provider == "local":
                batch_store.add_item_log(batch_id, item_id, "已向本地 Krea2 发出人物图请求（本地采样）……")
            else:
                batch_store.add_item_log(
                    batch_id, item_id,
                    f"已向中转站发出人物图请求（相似度门槛 {batch_image.PERSON_MIN_SIMILARITY}，"
                    f"不够会自动重做，最多 {batch_image.PERSON_MAX_ATTEMPTS} 次）……",
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
                # 人物图**自带身份自检**：相似度 <70 自动重做（最多 3 次），最后留分数最高的
                # 那张（用户 2026-09-19：「人物相似度 必须70%以上」）。与 /images 页面同一条实现。
                # 2026-09-20 起这条实现里还有**着装合规自检**（不合格继续重做，见
                # `batch_image.judge_compliance_safe`），结论记进条目日志。
                compliance_report: dict[str, object] = {}
                _, similarity = await batch_image.generate_person_image(
                    scene_image=scene,
                    identity_image=IDENTITY_PATH,
                    prompt=image_prompt,
                    ratio=ratio,
                    output_path=target_image,
                    on_progress=report,
                    compliance=compliance_report,
                )
                spent = int(time.monotonic() - image_started)
                if similarity >= 0:
                    batch_store.add_item_log(
                        batch_id, item_id,
                        f"候选人物图已生成：身份相似度 {similarity}/100，用时 {spent} 秒"
                        f"（门槛 {batch_image.PERSON_MIN_SIMILARITY}）。",
                    )
                else:
                    batch_store.add_item_log(
                        batch_id, item_id,
                        f"候选人物图已生成（用时 {spent} 秒）；身份自检这次没跑成，不影响出片。",
                    )
                if compliance_report.get("compliant") is False:
                    issues = "、".join(
                        str(item) for item in (compliance_report.get("issues") or [])[:3]
                    ) or "疑似擦边"
                    batch_store.add_item_log(
                        batch_id, item_id,
                        f"⚠️ 着装合规自检未通过（{issues}）：重做 {batch_image.PERSON_MAX_ATTEMPTS} 次"
                        "仍不合规，已先采用这张。建议换一个源视频，或在 /images 页面对这条"
                        "点「重新生成这一步」。",
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
    #    **封面已经出过的条目也不重写**：封面上印的就是这份文案，改了封面就作废
    #    （用户 2026-09-19：「都用原来的啊 不然封面都得改」）。
    if locked_copy:
        if result.get("reference_image_path"):
            batch_store.add_item_log(
                batch_id, item_id,
                "发布目录里已经出过封面，这次跳过「按最终画面重写文案」（封面上的字取自这份文案）。",
            )
    elif mode != "image" and result.get("reference_image_path"):
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

    # 图片生成自动化**已按用户要求关闭**（2026-09-16：「去除图片生成的自动化流程吧
    # 只保留生成字幕的流程」）：备料只把素材备齐（提示词 + 图一 + 图二）并停在审核点，
    # 人物图由用户自己出、自己上传。想临时恢复自动出图：`H3_AUTO_CHATGPT_IMAGE=1`。
    if _image_provider() == "manual" and not result.get("reference_image_path"):
        from . import chatgpt_image as _cgi
        if _cgi.auto_candidate_enabled():
            batch_store.add_item_log(
                batch_id, item_id, "已开启自动生成人物图：正在送 ChatGPT 桌面端生成候选图……"
            )
            await _cgi.auto_generate_candidate_image(batch_id, item_id)
        else:
            # 抽帧素材是**保留**的（用户 2026-09-16：「保留视频的人物抽帧 我要用它去生成图片」）：
            # 只关掉自动出图，图一/图二/提示词照样备齐，并把路径直接写进日志，省得去翻临时目录。
            try:
                rel = Path("data") / work.relative_to(DATA_DIR)
            except ValueError:
                rel = work
            batch_store.add_item_log(
                batch_id,
                item_id,
                "出图素材已就绪（自动出图已关闭，等你自己出图后上传回来）："
                f"提示词 {rel / '出图提示词.txt'}；"
                f"图一（源视频抽帧）{rel / 'scene-frame.jpg'}；"
                f"图二（身份参考）{IDENTITY_PATH}。",
            )

    # 封面自动化同样**默认关闭**（理由同上）：只有 `H3_AUTO_CHATGPT_COVER=1` 才自动出封面。
    if _cgi_auto_cover():
        _maybe_spawn_covers(batch_id, item_id)

    # 【全自动】封面走**中转站**（用户 2026-09-19：「全自动吧…现在直接用api生成」）：
    # 人物图与发布文案刚刚由 `deliver_review_materials()` 落进发布目录，现在出封面
    # 才能保证图上的标题/简介就是最终那一版。与出片**并行**（API 出图不吃显卡）。
    if full_auto_enabled() and result.get("reference_image_path"):
        spawn_api_covers(batch_id, item_id)

    # **自动放行出片**：默认也已关闭（图片自动化的一部分）——上传人物图后停在
    # 「等待你的确认」，点「确认并出片」才走视频链路。想恢复自动放行：`H3_AUTO_CONFIRM=1`。
    if _cgi_auto_confirm():
        fresh = _item(batch_id, item_id)
        if str((fresh.get("ai") or {}).get("reference_image_path") or "").strip():
            confirm_batch_items(batch_id, [item_id])
            batch_store.add_item_log(batch_id, item_id, "已自动放行出片（无需人工确认）。")


def _cgi_auto_cover() -> bool:
    from . import chatgpt_image as _cgi

    try:
        return bool(_cgi.auto_cover_enabled())
    except Exception:
        return False


def _cgi_auto_confirm() -> bool:
    """够不够格自动放行出片：**全自动链路开着就放行**，否则看 `H3_AUTO_CONFIRM`。"""
    if full_auto_enabled():
        return True
    from . import chatgpt_image as _cgi

    try:
        return bool(_cgi.auto_confirm_enabled())
    except Exception:
        return False


def cover_reference_image(item: dict[str, Any], folder: Path | None = None) -> Path | None:
    """封面要以哪张人物图为基准：**发布目录里那张 `人物图.*` 优先**。

    用户 2026-09-16：「生成的封面图背景要和人物图背景相同」——封面必须跟着发布目录里
    那张人物图（用户最终采用、屏幕上看到的那张）的背景走。实测 006 的 `人物图.png` 与
    `ai.reference_image_path` 指向的 ChatGPT 原图**不是同一张**，拿后者去生成封面，
    背景就跟发布目录里的人物图对不上了。
    """
    published = publish_published_image(folder)
    if published is not None:
        return published
    candidate = _deliverable_image(item)
    if candidate is not None:
        return candidate
    if IDENTITY_PATH.is_file():
        return IDENTITY_PATH
    return None


def publish_published_image(folder: Path | None) -> Path | None:
    """发布目录里已有的 `人物图.<后缀>`（按后缀顺序取第一张存在的）。"""
    if folder is None or not folder.is_dir():
        return None
    for name in _publish_image_names():
        if _present_file(folder, name):
            return folder / name
    return None


# 全自动链路里封面出图要**严格串行**（一条一条跑，避免同时打十几个中转站请求被限流）。
_COVER_LOCK = asyncio.Lock()


def spawn_api_covers(batch_id: str, item_id: str, *, only_missing: bool = True) -> bool:
    """【全自动】用**中转站**出这条的两张封面（后台任务，与出片并行）。

    时机很重要（踩过坑）：必须在「人物图 + 发布文案」都落进发布目录**之后**才出封面 ——
    封面上的大字就是 `发布文案.txt` 的标题、小字是简介，先出封面就会把旧标题画进图里。
    所以调用点是 `_prepare_review_work()` 里 `deliver_review_materials()` 之后，
    以及提交出片前的那道体检（`ensure_publish_images`）。

    `only_missing=True`：两张封面都在就直接返回，不白跑（重新开始时避免重复出图）。
    返回是否真的排上了任务。**不是** ChatGPT 桌面端那条路（那条仍归 `H3_AUTO_CHATGPT_COVER`）。
    """
    item = _item(batch_id, item_id)
    folder = _recorded_publish_folder(item) or _existing_publish_folder(item)
    if folder is None or not folder.is_dir():
        return False
    if publish_published_image(folder) is None:
        return False
    if not (folder / "发布文案.txt").is_file():
        return False
    if only_missing and all(
        any(_present_file(folder, name) for name in names)
        for names in PUBLISH_COVER_ALIASES.values()
    ):
        return False
    task = asyncio.create_task(_generate_covers_via_api(batch_id, item_id, folder))
    _COVER_TASKS.add(task)
    task.add_done_callback(_COVER_TASKS.discard)
    return True


async def _generate_covers_via_api(batch_id: str, item_id: str, folder: Path) -> None:
    """走中转站出两张封面：`B站4x3.png`（4:3）与 `抖音3x4.png`（3:4）。

    与 `/images` 页面「API 自动生成这一步」是同一个实现（`image_workbench._generate_cover`），
    失败只写日志标黄，**绝不影响出片**。
    """
    from . import image_workbench

    item = _item(batch_id, item_id)
    kind = str(item.get("kind") or "singing")
    async with _COVER_LOCK:
        for stage, label in (("bilibili", "B站封面"), ("douyin", "抖音封面")):
            if any(_present_file(folder, name) for name in PUBLISH_COVER_ALIASES[stage]):
                continue
            batch_store.add_item_log(batch_id, item_id, f"已向中转站发出{label}请求……")
            started = time.monotonic()
            try:
                await image_workbench._generate_cover(folder, stage, kind)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 封面失败不该把整条作品判失败
                batch_store.add_item_log(
                    batch_id, item_id,
                    f"{label}自动生成失败（不影响出片，用时 {int(time.monotonic() - started)} 秒）：{error}",
                )
                continue
            batch_store.add_item_log(
                batch_id, item_id,
                f"{label}已生成并放进发布目录（用时 {int(time.monotonic() - started)} 秒）。",
            )


def _maybe_spawn_covers(batch_id: str, item_id: str, *, only_missing: bool = False) -> bool:
    """若已开启封面自动化且该条的人物图+发布文案都齐了，就后台触发封面生成（ChatGPT 桌面端）。

    后台 fire-and-forget，不阻塞备料/出片；`_IMAGE_LOCK` 保证全局串行。
    `only_missing=True`：**两张封面都已在发布目录里就不触发**（正在出片前的那道检查用，
    免得每次重开都白跑一次 ChatGPT 出图）。返回是否真的触发了。
    """
    from . import chatgpt_image as _cgi
    if not _cgi.auto_cover_enabled():
        return False
    item = _item(batch_id, item_id)
    outputs = item.get("outputs") or {}
    folder = outputs.get("folder")
    if not folder:
        return False
    publish_folder = Path(folder)
    image = cover_reference_image(item, publish_folder)
    copy = publish_folder / "发布文案.txt"
    if image is None or not copy.is_file():
        return False
    if only_missing and all(_present_file(publish_folder, name) for name in PUBLISH_COVER_NAMES):
        return False
    task = asyncio.create_task(
        _cgi.auto_generate_covers(str(publish_folder), str(copy), str(image))
    )
    _COVER_TASKS.add(task)
    task.add_done_callback(_COVER_TASKS.discard)
    return True


async def ensure_publish_images(
    batch_id: str, item_id: str, *, reason: str = "", allow_spawn: bool = True
) -> dict[str, Any]:
    """检查发布目录里的**人物图与两张封面**，缺了什么就报出来。

    用户 2026-09-16 先要求「点击重新开始的时候要检查发布成品是否 人物图.png 和
    封面_抖音3x4.png 和 封面_B站4x3.png 是否完整 如果不完整看下是不是需要补充」，
    随后又要求「去除图片生成的自动化流程吧 只保留生成字幕的流程」——
    所以现在默认**只体检、不出图**：把缺的写进日志，图片由用户自己出/自己上传。

    `allow_spawn=True` 才会顺手触发 ChatGPT 桌面端出封面（现在只有显式调用才走得到，
    链路里已经不再用它自动补图；`H3_AUTO_CHATGPT_COVER=1` 时才会真的生成）。

    规则：
      · `人物图.*` 不在发布目录、但本条还有可用的候选图（`ai.reference_image_path`）→ 补写进去；
      · 封面缺任一张 → 只在 `allow_spawn` 时重开封面生成（`_IMAGE_LOCK` 串行，已有的那张会跳过）；
      · 都拿不到 → **只记日志**，不拦出片。
    返回 `{folder, image, missing, needs_covers, spawned}`。
    """
    state = await asyncio.to_thread(_sync_publish_images, batch_id, item_id)
    missing = list(state.get("missing") or [])
    if reason and missing:
        batch_store.add_item_log(
            batch_id, item_id, f"{reason}发布目录缺：{'、'.join(missing)}（图片由你自己出/自己上传）。"
        )
    spawned = False
    if allow_spawn and state.get("needs_covers"):
        if full_auto_enabled():
            # 全自动：封面走中转站（与备料后那次是同一实现，`only_missing` 保证已有的不重出）
            spawned = spawn_api_covers(batch_id, item_id)
            if spawned:
                batch_store.add_item_log(
                    batch_id, item_id, "正在用中转站补齐缺失的封面（已有的一张会跳过）……"
                )
        elif not spawned:
            spawned = _maybe_spawn_covers(batch_id, item_id, only_missing=True)
            if spawned:
                batch_store.add_item_log(
                    batch_id, item_id, "正在补齐缺失的封面图（ChatGPT 桌面端，已有的一张会跳过）……"
                )
    return {**state, "spawned": spawned}


def _publish_image_names() -> list[str]:
    """人物图可能带原后缀，按后缀列出候选名（交付用 `.png`，历史可能是 jpg/webp）。"""
    return ["人物图.png", "人物图.jpg", "人物图.jpeg", "人物图.webp"]


def _present_file(folder: Path, name: str) -> bool:
    path = folder / name
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def publish_image_state(folder: Path | None) -> dict[str, Any]:
    """发布目录里人物图与两张封面齐不齐（缺哪些、要不要重开封面生成）。

    发布文案不计入「必须补齐」（本地随时能重新生成，不需要 ChatGPT）。
    """
    if folder is None or not folder.is_dir():
        return {
            "folder": None,
            "image": None,
            "present": [],
            "missing": [PUBLISH_IMAGE_NAME, *PUBLISH_COVER_NAMES],
            "needs_covers": True,
        }
    present: list[str] = []
    image: str | None = None
    for name in _publish_image_names():
        if _present_file(folder, name):
            present.append(name)
            image = image or str(folder / name)
    missing: list[str] = []
    if image is None:
        missing.append(PUBLISH_IMAGE_NAME)  # 交付统一用 .png 这个名字
    needs_covers = False
    for stage, names in PUBLISH_COVER_ALIASES.items():
        found = next((name for name in names if _present_file(folder, name)), "")
        if found:
            present.append(found)
        else:
            missing.append(PUBLISH_COVER_NAMES[0 if stage == "bilibili" else 1])
            needs_covers = True
    return {
        "folder": str(folder),
        "image": image,
        "present": present,
        "missing": missing,
        "needs_covers": needs_covers,
    }


def _sync_publish_images(batch_id: str, item_id: str) -> dict[str, Any]:
    """线程里跑：把能补的人物图先补进发布目录，再回报还缺什么。"""
    item = _item(batch_id, item_id)
    ai = item.get("ai") or {}
    if not ai:
        return publish_image_state(None)
    folder = publish_folder(item)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        batch_store.add_item_log(batch_id, item_id, f"发布目录不可写（不影响出片）：{error}")
        return publish_image_state(None)
    state = publish_image_state(folder)
    if state["image"] is None:
        candidate = _deliverable_image(item)
        if candidate is not None:
            written = _write_publish_image(folder, candidate)
            if written is not None:
                batch_store.add_item_log(
                    batch_id, item_id, f"发布目录缺人物图，已补上：{written.name}。"
                )
                merged = {**(item.get("outputs") or {}), "image": str(written), "folder": str(folder.resolve())}
                _set_item(batch_id, item_id, outputs=merged)
                state = publish_image_state(folder)
    return state


async def _wait_for_free_pipeline(batch_id: str, item_id: str) -> None:
    """提交出片子任务之前，等流水线空出来（别的任务在跑时先别提交，省得 409 打转）。

    两个**实测踩出来的**边界（2026-09-16 用户：「批量点击重新开始怎么没有效果」）：

    * 「生成字幕」任务**不占流水线锁**（`subtitle_step` 只跑剪映 + ffmpeg，用户明确要求
      取消那把锁），但它照样是 `queued`/`running` 的 `store.active()`，于是整批出片会被
      一条卡住的字幕任务**无限期堵死**：条目日志停在「已有单链路任务运行，当前条目正在
      等待资源。」，runner 一直等它，界面看上去就是「点了重新开始没有任何反应」。
      所以这类任务直接跳过。
    * 兜底：万一真的有任务卡死把锁占着不放，这里也不许无限等 —— 等满
      `PIPELINE_FREE_WAIT_SECONDS` 就如实记一条日志交给提交逻辑（那边有 409 重试，
      实在不行会报出真实原因，绝不会静默挂住）。
    """
    deadline = _pipeline_wait_clock() + PIPELINE_FREE_WAIT_SECONDS
    announced = False
    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(f"{BATCH_SELF_URL}/api/comfy/queue")
                payload = response.json() if response.is_success else {}
        except Exception:  # noqa: BLE001
            # 自调用打不通时**既不能当成「流水线被占」、更不能把这条作品判失败**：
            # 2026-09-19 实测 8111 被 Windows 保留区间占住（winerror 10013）后，默认的
            # BATCH_SELF_URL（127.0.0.1:8111）连不上，这里抛 ConnectError 会让每一条
            # 已确认作品在提交前直接失败。记一条日志放行，交给提交逻辑 —— 那边本来就有
            # 409 重试（SUBMIT_BUSY_*），打不通自调用时它是更准的判据。
            logger.warning("查询流水线占用状态失败（按空闲放行）：%s", BATCH_SELF_URL, exc_info=True)
            return
        active = payload.get("app")
        if not active or active.get("status") not in {"queued", "running"}:
            return
        if str(active.get("kind") or "") == "subtitle":
            # 字幕任务不用 ComfyUI、也不持流水线锁：不该成为出片的等待理由
            return
        if not announced:
            batch_store.add_item_log(batch_id, item_id, "已有单链路任务运行，当前条目正在等待资源。")
            # 顺手把批次横幅也说清楚：这一条是**排队等出片**，不是已经开始出片了
            # （2026-09-17 用户看到两条「出片中」以为并发出片了）。横幅只是提示，
            # 取条目/写状态失败绝不能把等待循环弄断。
            try:
                waiting = _item(batch_id, item_id)
                batch_store.update(
                    batch_id,
                    notice=(
                        f"第 {int(waiting.get('index') or 0)} 条已确认，正在等前一条出片跑完"
                        "（单链路一次只跑一个视频）。"
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.debug("更新「排队等出片」横幅失败（忽略）", exc_info=True)
            announced = True
        if _pipeline_wait_clock() >= deadline:
            batch_store.add_item_log(
                batch_id,
                item_id,
                f"流水线被任务 {str(active.get('id') or '')[:8]} 占了超过 "
                f"{int(PIPELINE_FREE_WAIT_SECONDS // 60)} 分钟，本条改为直接提交"
                "（被占用的话会自动重试，不会把这一条判失败）。",
            )
            return
        await asyncio.sleep(PIPELINE_WAIT_POLL_SECONDS)


def _pipeline_wait_clock() -> float:
    """等待流水线的单调时钟（单独成函数：测试里换掉它就能**不用真等 30 分钟**）。"""
    return asyncio.get_running_loop().time()


# 等流水线空出来的上限（默认 30 分钟：真的出片/迁移任务可能要跑这么久，
# 但卡死的任务不能把整批拖到天荒地老）。
PIPELINE_FREE_WAIT_SECONDS = 1800.0
# 这个循环的轮询间隔
PIPELINE_WAIT_POLL_SECONDS = 4.0


# 提交视频子任务时若流水线正被别的任务占着（HTTP 409「已有任务正在运行」）：
# 等着重试，**绝不把这一条判失败**。`_wait_for_free_pipeline` 是提交前的一次检查，
# 但检查与提交之间仍有竞态（例：排队中的「生成字幕」正好在这一刻拿到流水线锁），
# 判失败会把整条作品的出片直接丢掉。
SUBMIT_BUSY_WAIT_SECONDS = 5
SUBMIT_BUSY_RETRIES = 60          # 最多等 5 分钟


IMAGE_PROVIDERS = {"auto", "api", "local", "frame", "manual"}


def _image_provider() -> str:
    """候选人物图来源：`api` 中转站（**全自动链路的默认**）/ `manual` 用户自己出图 /
    `local` 本地 Krea2 / `frame` 源视频取帧 / `auto`。

    **全自动开着时以「用中转站出图」为准**（用户 2026-09-19：「全自动吧。不要我去备料了。
    现在直接用api生成」）：机器上还留着老流程要求的 `H3_BATCH_IMAGE_PROVIDER=manual`
    （当初是为了避免批量备料和页面按钮**生成两遍**才钉死 manual 的），那条约定已被本条取代 ——
    留着它会把全自动悄悄降级成「只备料」，所以这里以 `H3_FULL_AUTO` 为准；
    只有显式指名 `local` / `frame` 这两个有明显用途的来源才听环境变量。
    全自动关掉（`H3_FULL_AUTO=0`）时完全回到老行为。
    """
    explicit = env_value("H3_BATCH_IMAGE_PROVIDER", "").strip().lower()
    if full_auto_enabled():
        if explicit in {"local", "frame"}:
            return explicit
        return "api" if batch_image.configured() else "manual"
    return explicit if explicit in IMAGE_PROVIDERS else "manual"


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
    # 等流水线的这段时间里，条目可能已经被「取消 / 跳过 / 删除」：**提交前的最后一道校验**。
    # 2026-09-17 实测事故：条目在提交途中被跳过，新子任务照样建了出来并排在流水线后面，
    # 于是它永远停在「排队中」，页面进度一动不动 —— 而真正占着 GPU 的是上一轮那个
    # 早已被取消的任务（它拿到锁后把 `cancelled` 覆盖成了 `running`）。
    current = _item(batch_id, item_id)
    if (
        current.get("skipRequested")
        or current.get("deleteRequested")
        or str(current.get("status") or "") in {"skipped", "deleted", "completed", "failed"}
    ):
        batch_store.add_item_log(
            batch_id, item_id, "本条在等流水线时已被取消/跳过，不再提交出片任务。"
        )
        raise asyncio.CancelledError
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
        _set_item(batch_id, item_id, actionPrompt=action, cameraPrompt=camera)
        batch_store.add_item_log(
            batch_id, item_id,
            "批量出片只出**原声**成片（不在这条链路上转音色）：交付后在 /rvc 页点"
            "「一键批量转换音色」补做音色版，再批量加字幕。",
        )
        data = {
            "action_prompt": action,
            "camera_prompt": camera,
            "ratio": ratio,
            # **批量唱歌只出原声成片**（2026-09-20）：RVC 音色转换搬到「一键批量转换音色」
            # 那个独立入口（用户要求：`最终成片.mp4` → 音色版 → 字幕版 → 发布）。
            # 这么做还有个硬好处：`use_rvc=0` 时链路**不会为了 RVC 关闭 ComfyUI**
            # （`_complete_without_rvc`），于是批量期间 ComfyUI 全程常驻、模型不用反复加载。
            "use_rvc": "0",
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
    response: httpx.Response | None = None
    for attempt in range(SUBMIT_BUSY_RETRIES):
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
        if response.status_code != 409 or attempt == SUBMIT_BUSY_RETRIES - 1:
            break
        # 409 = 流水线正被别的任务占着（例如用户刚提交的「生成字幕」正在剪映识别）。
        # 等着重试，**绝不把这一条判失败**——判失败会把整条作品的出片丢掉。
        if attempt == 0:
            batch_store.add_item_log(
                batch_id, item_id, "流水线正被其它任务占用，本条等待它结束后自动重试提交……"
            )
        await asyncio.sleep(SUBMIT_BUSY_WAIT_SECONDS)
    if response is None or not response.is_success:
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


def kind_label(kind: str) -> str:
    """发布目录/日志用的类型标签：唱歌 / 跳舞（未知类型返回空串，不拼标签）。"""
    return {"singing": "唱歌", "dance": "跳舞"}.get(str(kind or "").strip(), "")


def publish_chain(item: dict[str, Any]) -> dict[str, Any]:
    """这条作品发布目录里的**版本链条**与「该发哪个文件」（2026-09-20 用户定的链条）。

        最终成片.mp4（原声母版）
          → 最终成片_音色版.mp4（一键批量转换音色）
            → 最终成片_字幕版.mp4（一键批量加字幕 = **发布版**）

    跳舞没有音色这一步，发布版就是 `最终成片.mp4`。字幕版比它的输入旧 → 标记 `subtitleStale`
    （先烧字幕、后转音色会留下「音色版 + 原声字幕版」，看起来完整其实是旧音频）。

    返回 `{folder, clean, voice, subtitle, subtitleStale, needsVoice, publishFile, publishHint}`；
    目录还不存在就返回全 False + 空 publishFile（页面照常显示，只是都是灰的）。
    """
    folder = _recorded_publish_folder(item) or _existing_publish_folder(item)
    kind = str(item.get("kind") or "")
    needs_voice = kind == "singing"
    state: dict[str, Any] = {
        "folder": str(folder) if folder is not None else "",
        "clean": False,
        "voice": False,
        "subtitle": False,
        "subtitleStale": False,
        "needsVoice": needs_voice,
        "publishFile": "",
        "publishHint": "还没有成片",
    }
    if folder is None or not folder.is_dir():
        return state
    clean = folder / "最终成片.mp4"
    voice = folder / "最终成片_音色版.mp4"
    subtitle = folder / "最终成片_字幕版.mp4"
    state["clean"] = clean.is_file()
    state["voice"] = voice.is_file()
    state["subtitle"] = subtitle.is_file()
    source = voice if state["voice"] else clean
    if state["subtitle"] and source.is_file():
        try:
            state["subtitleStale"] = subtitle.stat().st_mtime < source.stat().st_mtime - 1
        except OSError:
            state["subtitleStale"] = False
    if state["subtitle"] and not state["subtitleStale"]:
        state["publishFile"] = "最终成片_字幕版.mp4"
        state["publishHint"] = "可发布：字幕版"
    elif needs_voice and state["voice"]:
        state["publishFile"] = "最终成片_音色版.mp4"
        state["publishHint"] = (
            "可发布：音色版（字幕版已过期，建议重烧字幕）"
            if state["subtitleStale"] else
            "可发布：音色版（还没加字幕）"
        )
    elif state["clean"]:
        state["publishFile"] = "最终成片.mp4"
        state["publishHint"] = (
            "可发布：原声母版（还没转音色）" if needs_voice else "可发布：最终成片"
        )
    return state


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_item_for_folder(folder: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """发布目录 → (批次, 条目)：先按 `outputs.folder` 精确匹配，再按目录名末尾作品号唯一命中。

    口径与原来的 `publish_watcher.match_item` 一致（2026-09-20 watcher 删除时搬进来）：
    批次按新到旧遍历，历史目录 / 已删除条目一律不碰。
    """
    folder = Path(folder)
    try:
        target = folder.resolve()
    except OSError:
        target = folder
    batches = batch_store.active_batches()
    for batch in batches:
        for item in batch.get("items") or []:
            if str(item.get("status") or "") == "deleted":
                continue
            recorded = str((item.get("outputs") or {}).get("folder") or "").strip()
            if not recorded:
                continue
            try:
                if Path(recorded).resolve() == target:
                    return batch, item
            except OSError:
                continue
    aweme = folder.name.rsplit("_", 1)[-1].strip()
    if aweme.isdigit():
        for batch in batches:
            hits = [
                item
                for item in batch.get("items") or []
                if str(item.get("status") or "") != "deleted"
                and str(item.get("awemeId") or "").strip() == aweme
            ]
            if len(hits) == 1:
                return batch, hits[0]
    return None


# 只有这三种状态的条目会被「谁写图谁登记」动到（与 watcher 当年的口径一致）。
ADOPTABLE_ITEM_STATUSES = {"awaiting_review", "confirmed", "failed"}


def adopt_published_portrait(
    folder: Path, *, image: Path | None = None
) -> dict[str, Any] | None:
    """把发布目录里的人物图**当场**登记成对应条目的候选图（替代已删掉的 `publish_watcher`）。

    用户 2026-09-20 选「谁写图谁登记」：`/images` 出图 / 上传成功后直接调这里，不再靠
    每 5 秒扫一遍发布目录的中间人（那个模块已删除）。规则沿用 watcher 那套：

      · 只碰 `awaiting_review`（挂图 + 放行）/ `confirmed`（只换图）/ `failed`（挂图 + 重跑）；
        `pending` / `revising` / `running` / `skipped` / `completed` / `deleted` 一律不动；
      · **同一张图只驱动一次**（`ai.portrait_watch.sha` 记账），重启后也不会重复跑；
      · 目录里那张与条目候选图 sha 相同、且不需要放行 → 什么都不做（避免无谓改写）。

    返回 `{batchId, itemId, action, title, needsWake}`；没有对应条目 / 不需要动时返回 None。
    `needsWake=True` 表示调用方应当叫醒那一批（自调用 `/api/batches/{id}/start`）。
    """
    folder = Path(folder)
    if image is None:
        image = publish_published_image(folder)
    if image is None:
        return None
    image = Path(image)
    if not image.is_file():
        return None
    found = _find_item_for_folder(folder)
    if found is None:
        return None
    batch, item = found
    batch_id = str(batch.get("id") or "")
    item_id = str(item.get("id") or "")
    if not batch_id or not item_id:
        return None
    status = str(item.get("status") or "")
    if status not in ADOPTABLE_ITEM_STATUSES:
        return None
    ai = item.get("ai") or {}
    if not ai:
        return None                       # 还没备料：别动
    try:
        sha = _file_sha256(image)
    except OSError:
        return None
    marker = ai.get("portrait_watch")
    if isinstance(marker, dict) and str(marker.get("sha") or "") == sha:
        return None                       # 这张图已经驱动过一次
    candidate = Path(str(ai.get("reference_image_path") or ""))
    candidate_ok = candidate.is_file()
    try:
        needs_upload = not candidate_ok or _file_sha256(candidate) != sha
    except OSError:
        needs_upload = True
    if not needs_upload and candidate.stem.endswith("_frame"):
        return None                       # 目录里那张就是备料留下的兜底取帧图
    if not needs_upload and status == "confirmed":
        return None                       # 没有新图、本条也已经放行：不用动

    action = {"awaiting_review": "confirm", "confirmed": "upload", "failed": "retry"}[status]
    adopted = candidate
    if needs_upload:
        revision = int(item.get("revision") or 0)
        work = DATA_DIR / "batches" / batch_id / item_id
        try:
            work.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        suffix = image.suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            suffix = ".png"
        target = work / f"candidate_r{revision}_upload{suffix}"
        try:
            shutil.copy2(image, target)
        except OSError:
            return None
        adopted = target

    def apply(row: dict[str, Any]) -> None:
        fields = row.setdefault("ai", {})
        if needs_upload:
            fields["reference_image_path"] = str(adopted)
        fields["portrait_watch"] = {
            "sha": sha, "at": now_iso(), "action": action, "image": image.name,
        }

    batch_store.mutate_item(batch_id, item_id, apply)
    batch_store.add_item_log(
        batch_id, item_id,
        f"图片工作台刚出的「{image.name}」已挂成这一条的候选人物图。",
    )
    needs_wake = False
    if action == "confirm":
        confirm_batch_items(batch_id, [item_id])
        batch_store.add_item_log(batch_id, item_id, "已自动确认并开始出片（等同你点「确认并出片」）。")
        needs_wake = True
    elif action == "retry":
        restart_item_to_run(batch_id, item_id)
        batch_store.add_item_log(batch_id, item_id, "已按新的人物图重跑这一条。")
        needs_wake = True
    return {
        "batchId": batch_id,
        "itemId": item_id,
        "action": action,
        "title": str(item.get("title") or folder.name),
        "needsWake": needs_wake,
    }


def _ideal_publish_folder(item: dict[str, Any]) -> Path:
    """发布目录的**理想名字**：`singing|dance/{三位编号}_{唱歌或跳舞}_{歌名或标题}_{作品号}`。

    类型标签（唱歌 / 跳舞）是用户 2026-09-18 的要求（「在对应的标签后面加上唱歌还是跳舞」，
    例：`001_唱歌_夜空中最亮的星_7686514674456574953`）。历史条目类型未知时不拼标签，
    保持旧格式 `{编号}_{标题}_{作品号}`；`_existing_publish_folder` 按作品号后缀认目录，
    新旧两种格式都能对上。

    **2026-09-20 两处新规矩**：
      ① 发布根下多一层类型子目录（`singing/` `dance/`，用户：「在发布目录里加一个跳舞文件夹
         一个唱歌文件夹各自放对应的内容」）；
      ② 编号**按类型各自从 001 起**（用户：「唱歌的序号和跳舞的序号都是独立的」）——优先用
         `kindIndex`（新建条目时按类型分配、永不变），没有这个字段的老条目继续用全局
         `index`：老批次的理想名因此一个字都不变，老目录不会被"顺手改名"。
    """
    ai = item.get("ai") or {}
    aweme_id = str(item.get("awemeId") or item.get("id"))
    base_name = _safe_name(str(ai.get("song_name") or ai.get("title") or item.get("title") or "作品"))
    label = kind_label(str(item.get("kind") or ""))
    number = int(item.get("kindIndex") or item.get("index") or 0)
    stem = (
        f"{number:03d}_{label}_{base_name}_{aweme_id}"
        if label
        else f"{number:03d}_{base_name}_{aweme_id}"
    )
    return BATCH_OUTPUT_ROOT / publish_subdir(str(item.get("kind") or "")) / stem


def locked_publish_copy(item: dict[str, Any]) -> dict[str, Any] | None:
    """发布目录里**已经出过封面**时，那条作品的发布文案不许再重写。

    用户 2026-09-19：「都用原来的啊 不然封面都得改」——封面上的大字就是 `title`、
    小字就是 `introduction`，两者在出封面那一刻就被**画进图里**了；之后再重写文案
    （重新备料会重跑预审、`write_copy` 也会按新图重写），封面立刻和文案对不上，
    用户只能把封面全部重做。实测事故：019 的 `成熟与天真之间，刚好是我` 在一次
    「重新开始」里被改成 `成熟里藏着天真的一支舞`，两张封面当场作废。

    所以规则是：**封面一旦落进发布目录，这份文案就锁住**；要改文案，就得连封面一起
    重做（用户显式要求时）。返回锁住的那份文案（`{title, introduction, tags}`），
    没有封面就返回 None（照旧可以自由生成/重写）。
    """
    folder = _recorded_publish_folder(item) or _existing_publish_folder(item)
    if folder is None or not folder.is_dir():
        return None
    names = PUBLISH_COVER_NAMES + ("B站4x3.png", "抖音3x4.png")
    if not any((folder / name).is_file() for name in names):
        return None
    ai = dict(item.get("ai") or {})
    locked = {
        key: ai.get(key) or item.get(key)
        for key in ("title", "introduction", "tags")
    }
    return locked if any(locked.values()) else None


def _adopt_published_image(
    batch_id: str, item_id: str, item: dict[str, Any], work: Path, revision: int
) -> Path | None:
    """把**发布目录里已经有的人物图**收编成本条的候选图（复制进条目目录），没有就返回 None。

    用户 2026-09-19：「对于已经有的不要重复得去生成」。`/images` 页面出图是直接写进发布目录的，
    条目上不登记 —— 重新备料时系统以为「没有候选图」，于是又烧一次出图（钱白花），
    而且发布目录那张在下次刷新物料时还会被当成空的。收编之后两件事一起解决。
    """
    folder = _recorded_publish_folder(item) or _existing_publish_folder(item)
    published = publish_published_image(folder)
    if published is None or not published.is_file():
        return None
    suffix = published.suffix.lower() if published.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
    try:
        adopted = work / f"candidate_r{revision}_adopted{suffix}"
        shutil.copy2(published, adopted)
    except OSError as error:
        batch_store.add_item_log(batch_id, item_id, f"发布目录里的人物图收编失败，将重新出图：{error}")
        return None
    batch_store.add_item_log(
        batch_id, item_id,
        "发布目录里已经有出好的人物图，直接收编成这一条的候选图（没有重复出图）。",
    )
    return adopted


def _publish_scan_roots(kind: str) -> list[Path]:
    """找目录时要扫的发布根：**类型子目录优先，然后是旧的扁平发布根**。

    2026-09-20 起发布根下多了 `singing/` `dance/` 两层；老目录还在扁平根里。用户
    2026-09-20 说「老项目不要用管」，所以这里只**复用**、绝不改名/搬移 —— 等他把老内容
    清空，扁平根那条自然就没人走了。
    """
    roots: list[Path] = []
    sub = publish_subdir(kind)
    if sub:
        roots.append(BATCH_OUTPUT_ROOT / sub)
    roots.append(BATCH_OUTPUT_ROOT)
    return roots


def _existing_publish_folder(item: dict[str, Any]) -> Path | None:
    """发布根里**已经属于同一条作品**的目录（之前批次留下的）。

    用户 2026-09-13：「现在加入队列目录又重复生成文件夹了……如果目录里已经有了最终成片
    说明已经生成过了……再次加入队列时候不要重复生成文件夹」。同一个作品号只允许一个目录：
    优先复用**已经有 `最终成片.mp4`** 的那个（说明这条出过片），其次复用最新的那个。
    """
    aweme = str(item.get("awemeId") or "").strip()
    if not aweme or not BATCH_OUTPUT_ROOT.is_dir():
        return None
    candidates: list[Path] = []
    for root in _publish_scan_roots(str(item.get("kind") or "")):
        if not root.is_dir():
            continue
        candidates.extend(
            path
            for path in root.iterdir()
            if path.is_dir() and path.name.endswith(f"_{aweme}")
        )
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


def publish_folder_for_source_job(source_job_id: str) -> tuple[Path, dict[str, Any]] | None:
    """这条**源成片**所属批量作品的发布目录（没有对应条目就 None）。

    生成字幕路由（`/subtitle`）要「生成完成放到对应的发布目录」：用户从「最近任务选」
    挑的成片就是某条批量作品的出片，条目里记着 `videoJobId`，顺着它拿到那一条自己的
    发布目录（与最终成片 / 人物图 / 发布文案同一个目录）。

    **这里故意不走 `publish_folder()`**：那个函数会顺手把目录改名到「理想编号名」
    （条目被删过之后 index 会变，实测同一条目的目录是 `006_…` 而现在的理想名是 `002_…`）。
    批量交付时改名是它自己的规矩，但用户只是来加个字幕，不该看到自己的发布目录突然换名 ——
    所以只认「已有的同作品号目录 → 记录过的目录 → 理想名（仅用于建新目录）」。
    """
    item = batch_store.item_by_video_job(source_job_id)
    if not item:
        return None
    folder = (
        _existing_publish_folder(item)
        or _recorded_publish_folder(item)
        or _ideal_publish_folder(item)
    )
    return folder, item


def subtitle_publish_folder(job_id: str, *, song: str = "", source_name: str = "") -> Path:
    """用户**本地上传**的视频没有对应作品：在发布根下按 `字幕_{歌名或源文件名}_{job短号}` 建一个。

    命名跟着批量那套（`{编号}_{歌名或标题}_{作品号}`）走，只是用 `字幕_` 前缀 + job 短号
    当身份，免得跟「作品号」那种真抖音作品混在一起。
    """
    stem = Path(str(source_name or "")).stem
    base_name = _safe_name(str(song or "").strip() or stem or "上传视频", fallback="上传视频")
    # 字幕只服务唱歌（跳舞没有歌词字幕这一步），所以落在 `singing/` 子目录里
    return BATCH_OUTPUT_ROOT / publish_subdir("singing") / f"字幕_{base_name}_{str(job_id)[:8]}"


def publish_folder_for_uploaded_video(source_video: Path | None) -> Path | None:
    """Locate an uploaded copy's original publish folder by exact file content.

    Browsers intentionally expose only the upload filename, not its original local path.  Most
    selected files here are an existing ``发布成品/{singing,dance}/.../最终成片.mp4``.  Match size
    first and SHA-256 only the small candidate set so the subtitle returns to that exact folder
    instead of creating an unrelated ``字幕_...`` directory. 老结构的扁平目录也一起认。
    """
    source = Path(source_video) if source_video else Path()
    if not source.is_file() or not BATCH_OUTPUT_ROOT.is_dir():
        return None
    size = source.stat().st_size

    def digest(path: Path) -> str:
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                value.update(chunk)
        return value.hexdigest()

    source_hash: str | None = None
    patterns = [f"*/最终成片.mp4"]                                  # 旧的扁平结构
    if publish_subdir("singing"):
        patterns.insert(0, f"{publish_subdir('singing')}/*/最终成片.mp4")
    if publish_subdir("dance"):
        patterns.insert(1, f"{publish_subdir('dance')}/*/最终成片.mp4")
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(BATCH_OUTPUT_ROOT.glob(pattern))
    for candidate in candidates:
        try:
            if candidate.stat().st_size != size:
                continue
            source_hash = source_hash or digest(source)
            if digest(candidate) == source_hash:
                return candidate.parent
        except OSError:
            continue
    return None


def deliver_subtitle_output(
    job_id: str,
    subtitle: Path,
    *,
    source_job_id: str = "",
    song: str = "",
    source_name: str = "",
    source_video: Path | None = None,
) -> Path:
    """把字幕版拷进「对应的发布目录」，返回落盘路径。

    · 源成片来自某条批量作品 → 拷进**那一条自己的发布目录**，文件名与批量链路一致
      （`最终成片_字幕版.mp4`）——两条链路产出同名，用户不用记两套命名；
    · 用户本地上传的视频 → 在发布根下建 `字幕_{歌名或源文件名}_{job短号}`，同样放
      `最终成片_字幕版.mp4`。

    只拷不删：任务目录里那份（`subtitleOutPath`）继续给页面回看/下载用。
    """
    subtitle = Path(subtitle)
    if not subtitle.is_file():
        raise FileNotFoundError(f"字幕版成片不存在：{subtitle}")
    found = publish_folder_for_source_job(source_job_id)
    folder = (
        found[0] if found
        else publish_folder_for_uploaded_video(source_video)
        or subtitle_publish_folder(job_id, song=song, source_name=source_name)
    )
    folder.mkdir(parents=True, exist_ok=True)
    return _copy_file(subtitle, folder / "最终成片_字幕版.mp4")


def _write_publish_image(folder: Path, image: Path | None) -> Path | None:
    """写 `人物图.<原后缀>`；换了图就清掉旧后缀，避免目录里留两张。

    ⚠️ **没有新图时什么都不做**（2026-09-19 事故，实测丢了 019/020/023 三张人物图）：
    原实现先**无条件**删掉目录里所有 `人物图.*`，再在 `image is None` 时直接 return ——
    于是「本条没有候选图」这种情况（发布目录里那张是 `/images` 页面直接生成的、
    只落在发布目录、没登记进条目的 `ai.reference_image_path`）会把**已经出好的人物图
    删干净且不补回来**，而且没有任何副本可恢复。现在改成**先确认有新图才动手删**，
    并且先写临时文件、成功后再替换，复制失败也不会把旧图弄丢。
    """
    if image is None:
        return None
    suffix = image.suffix.lower() if image.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
    target = folder / f"人物图{suffix}"
    staging = folder / f".人物图{suffix}.incoming"
    try:
        _copy_file(image, staging)
    except OSError:
        staging.unlink(missing_ok=True)
        raise
    for stale in folder.glob("人物图.*"):
        if stale.name == target.name:
            continue
        try:
            stale.unlink()
        except OSError:
            pass
    os.replace(staging, target)
    return target


def _item_frame(item: dict[str, Any]) -> Path | None:
    """本条的**源视频抽帧**（图一）。

    2026-09-16 用户：「保留视频的人物抽帧 我要用它去生成图片」「人物抽帧 也放到发布目录」——
    抽帧既要留在条目目录里当出图素材，也要复制一份进发布目录，方便直接拿去出图。
    """
    raw = str((item.get("ai") or {}).get("sceneFramePath") or "").strip()
    if raw and Path(raw).is_file():
        return Path(raw)
    fallback = DATA_DIR / "batches" / str(item.get("_batchId") or "") / str(item.get("id")) / "scene-frame.jpg"
    return fallback if fallback.is_file() else None


def _write_publish_frame(folder: Path, frame: Path | None) -> Path | None:
    """把源视频抽帧复制成发布目录里的 `抽帧.jpg`（幂等覆盖）。"""
    if frame is None:
        return None
    try:
        return _copy_file(frame, folder / PUBLISH_FRAME_NAME)
    except OSError:
        return None


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
        # 源视频抽帧也放一份进发布目录（用户 2026-09-16：「人物抽帧 也放到发布目录」）
        frame = _write_publish_frame(folder, _item_frame(item))
        if frame is not None:
            fresh["frame"] = str(frame)
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
    landed = ["发布文案"]
    if "image" in fresh:
        landed.insert(0, "人物图")
    if "frame" in fresh:
        landed.append(PUBLISH_FRAME_NAME)
    batch_store.add_item_log(
        batch_id,
        item_id,
        f"{'、'.join(landed)}已先放进发布目录：{folder}（最终成片跑完后再补）",
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
    # 源视频抽帧也补一份（用户 2026-09-16：「人物抽帧 也放到发布目录」）
    frame = _write_publish_frame(folder, _item_frame(item))
    if frame is not None:
        outputs["frame"] = str(frame)
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
    # 整批取消后顺手关掉 ComfyUI，别让它空转占显存（用户 2026-09-15：「停止的话 comfyui 你也要关闭」）
    await _stop_comfy_if_idle()
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


async def _stop_comfy_if_idle() -> bool:
    """没有别的活要干了就顺手关掉 ComfyUI，把显存让出来。

    用户 2026-09-15：「停止的话 comfyui 你也要关闭」——取消/跳过一条之后，
    ComfyUI 常常就那样空转占着显存（实测有从 19:31 一直开到晚上的情况）。

    直接打自家的 `POST /api/comfy/stop`，**复用它的安全判断**：
    还有别的条目在 running/revising/confirmed、或 ComfyUI 队列里还有 prompt 时它会回 409，
    这里就当「还不能关」静默跳过，不会误杀正在生成的图/视频。

    另外在这里先自查一遍批次：只要还有**没结束**的条目（排队/等确认/已放行/出片中/重新备料）
    就不关 —— 免得后面还要再用 ComfyUI 又把它拉起来，来回启停更慢。
    """
    try:
        state = batch_store.active()
    except Exception:
        state = None
    if state:
        busy = next(
            (
                item
                for item in state.get("items") or []
                if item.get("status") in {"pending", "awaiting_review", "confirmed", "running", "revising"}
            ),
            None,
        )
        if busy is not None:
            return False
    try:
        from .settings import COMFY_URL
    except Exception:
        # 兜底也要认 H3_COMFY_URL：ComfyUI 的端口由启动脚本选（默认 15188，
        # 不用会被 Windows 保留区间圈住的 8188）
        COMFY_URL = os.getenv("H3_COMFY_URL", "http://127.0.0.1:15188").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # **先掐掉残留的 prompt**：取消任务后 ComfyUI 队列里常常还留着它，
            # 而 `/api/comfy/stop` 见到队列非空会 409 拒绝关闭（实测），
            # 结果就是「明明取消了，ComfyUI 还在白烧显存」。
            # 上面已经确认批次里没有没结束的条目，所以这里中断是安全的。
            try:
                await client.post(f"{COMFY_URL}/interrupt")
                await client.post(f"{COMFY_URL}/queue", json={"clear": True})
            except Exception:
                logger.warning("中断 ComfyUI 残留任务失败（继续尝试关闭）", exc_info=True)
            resp = await client.post(f"{BATCH_SELF_URL}/api/comfy/stop")
    except Exception:
        logger.warning("自动关闭 ComfyUI 失败（忽略）", exc_info=True)
        return False
    if resp.status_code == 200:
        logger.info("已顺手关闭 ComfyUI，释放显存")
        return True
    return False


def _spawn_orphan_abandon(batch_id: str, item_id: str, *, deleted: bool) -> None:
    """runner 已经退出时，直接收尾一个「正在出片」的条目。

    2026-09-15 用户：「点击列表取消怎么没有效果」——批次因为**别的**条目失败而 `failed`、
    runner 已退出；这时点「取消」只把 `skipRequested` 写进状态（也会取消子任务），
    但**把条目落成 `skipped` 这一步在 runner 循环里**，没人做 → 条目永远卡在 `running`，
    页面上看着就像没生效。
    """
    task = asyncio.create_task(_abandon_without_runner(batch_id, item_id, deleted=deleted))
    _SALVAGE_TASKS.add(task)
    task.add_done_callback(_SALVAGE_TASKS.discard)


async def _abandon_without_runner(batch_id: str, item_id: str, *, deleted: bool) -> None:
    current = _item(batch_id, item_id)
    child_id = str(
        current.get("videoJobId") or (current.get("childJob") or {}).get("id") or ""
    )
    if child_id:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                await client.post(f"{BATCH_SELF_URL}/api/jobs/{child_id}/cancel")
        except Exception:
            logger.warning("取消子任务失败（继续收尾）：%s", child_id, exc_info=True)
        # 等子任务真的落定，成片若已生成 `_finish_abandoned` 会照样抢救进发布目录
        for _ in range(90):
            await asyncio.sleep(2)
            child = store.get(child_id)
            if not child or child.get("status") not in {"queued", "running", "cancelling"}:
                break
    await _finish_abandoned(batch_id, item_id, deleted=deleted)
    # 停止之后顺手关掉 ComfyUI（用户 2026-09-15：「停止的话 comfyui 你也要关闭」）
    await _stop_comfy_if_idle()


def heal_terminal_item_milestones(state: dict[str, Any] | None) -> bool:
    """终态条目上不许留着「进行中」的里程碑；返回是否真的改过（幂等，读批次时顺手体检）。

    2026-09-17 实测事故：条目已经是「已跳过」，它的 `video` 里程碑却还挂着 running
    （跳过之后 runner 又推进了一次，`finishedAt` 甚至早于 `startedAt`），页面上那一步
    永远转圈 —— 用户报的正是「批量里的状态是错的、进度也不同步」。这里只收尾**已经
    结束的条目**；还在跑（running/revising/等确认）的一律不碰。
    """
    if not state:
        return False
    batch_id = str(state.get("id") or "")
    if not batch_id:
        return False
    target_of = {"skipped": "skipped", "deleted": "skipped", "completed": "completed", "failed": "error"}
    changed = False
    for item in state.get("items") or []:
        target = target_of.get(str(item.get("status") or ""))
        if not target:
            continue
        for milestone in item.get("milestones") or []:
            if milestone.get("status") != "running":
                continue
            batch_store.set_item_milestone(
                batch_id, str(item.get("id") or ""), milestone["id"],
                status=target, currentNode=None, progress=None,
            )
            changed = True
    return changed


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
    """确认后的出片：**先排队等单链路空出来，再标成「出片中」**。

    2026-09-17 用户实测：「怎么我第一个视频还没有完成你就开始第二个视频了 这是不对的
    是单链路一个一个视频来生成处理」——旧顺序是先把条目切成 `running/video`，再去
    `_post_video_job` 里等流水线，于是整个等待期间页面上**同时有两条「出片中」**
    （第二条其实只是在排队等 GPU），看起来像并发出片。现在：等待期间它保持「已确认」，
    只有真的轮到它、要提交出片子任务了才切成「出片中」。真正的串行一直由 runner 的
    `video_task`（一次只有一个）+ `_wait_for_free_pipeline` 保证，这里改的是**状态是否诚实**。
    """
    item = _item(batch_id, item_id)
    # 源视频可能已经被清理掉（用户重新开始一条老任务时很常见）：先补下载，
    # 否则提交视频任务时会直接抛「文件不存在」，看起来像系统坏了。
    if not Path(str(item.get("sourcePath") or "")).is_file():
        batch_store.add_item_log(batch_id, item_id, "源视频已不在磁盘上，正在重新下载……")
        await _download(batch_id, item_id)          # `_download` 自己会把状态标成「下载中」
        _set_item(batch_id, item_id, status="confirmed", stage="confirmed")
    # 出片前**只体检、不出图**（2026-09-16 用户：「去除图片生成的自动化流程吧 只保留生成字幕的流程」）：
    # 缺人物图/封面只写日志标出来，交给用户自己出图/自己上传，链路不再自动触碰图片。
    try:
        await ensure_publish_images(batch_id, item_id, reason="出片前检查发布成品：", allow_spawn=False)
    except Exception:  # noqa: BLE001 - 检查本身不能拖累出片
        logger.exception("出片前检查发布成品失败（忽略）")

    # ① 批次级视频闸门：把“等资源 → 提交 → 生成 → 交付”整段包起来。
    #    以前只靠单个 runner 手里的 `video_task`；连续确认会触发 runner 换代，旧 runner
    #    的第一条还没结束，新 runner 就可能推进第二条。现在无论有几代 runner，第二条都
    #    必须等第一条 `_mark_delivered` 完成、释放这个锁以后才能进入视频链路。
    video_lock = _BATCH_VIDEO_LOCKS.setdefault(batch_id, asyncio.Lock())
    async with video_lock:
        current_batch = batch_store.get(batch_id) or {}
        current = _item(batch_id, item_id)
        if (
            current_batch.get("status") in {"failed", "paused", "cancelled"}
            or current.get("skipRequested")
            or current.get("deleteRequested")
            or str(current.get("status") or "") != "confirmed"
        ):
            # 等闸门期间被取消、暂停，或已经被别的 runner 处理过：绝不能重复提交。
            batch_store.add_item_log(
                batch_id, item_id, "本条等待视频单链路期间已被取消/跳过或状态已变化，不再重复提交视频任务。"
            )
            return

        # ② 单链路排队：**先等其它任务把全局流水线让出来**。等待期间条目保持「已确认」。
        await _wait_for_free_pipeline(batch_id, item_id)
        current = _item(batch_id, item_id)
        if (
            current.get("skipRequested")
            or current.get("deleteRequested")
            or str(current.get("status") or "") != "confirmed"
        ):
            batch_store.add_item_log(
                batch_id, item_id, "本条在排队等出片时已被取消/跳过或状态已变化，不再提交出片任务。"
            )
            return

        # ③ 轮到这一条了：这才切成「出片中」，并一直持锁到最终成片交付完成。
        _set_item(batch_id, item_id, status="running", stage="video", error=None, childJob=None)
        batch_store.set_item_milestone(batch_id, item_id, "video", status="running", progress=1)
        batch_store.add_item_log(batch_id, item_id, "已确认候选结果，开始执行视频生成单链路。")
        video_job = await _post_video_job(batch_id, item_id)
        _set_item(batch_id, item_id, videoJobId=video_job["id"], childJob=video_job)
        video_job = await _watch_child(batch_id, item_id, video_job["id"], "video")
        batch_store.set_item_milestone(batch_id, item_id, "video", status="completed", progress=100)
        _set_item(batch_id, item_id, stage="deliver", childJob=None)
        outputs = await _deliver(batch_id, item_id, video_job)
        await _mark_delivered(batch_id, item_id, outputs, note="发布文件已整理：")
    # 唱歌条目出片后**不再自动加字幕**（2026-09-16 用户：「现在的生成完成唱歌视频之后自动运行的
    # 字幕生成有问你不要自动生成了」）：字幕只在独立路由 /subtitle 上手动跑，用户自己挑要加字幕的成片。
    # `_add_subtitles_if_singing` 与 `subtitle_step.add_for_item` 保留（便于以后恢复），只是没人再调用。


async def _add_subtitles_if_singing(batch_id: str, item_id: str, outputs: dict[str, str]) -> None:
    """唱歌条目出片后自动生成 `最终成片_字幕版.mp4`。

    **2026-09-16 起批量链路不再调用它**（用户：「现在的生成完成唱歌视频之后自动运行的字幕生成
    有问你不要自动生成了」）：字幕统一改到独立路由 `/subtitle`（`subtitle_step.run_subtitle_job`）
    手动跑。函数与三条纪律原样保留，便于以后恢复自动链路：

      · **只给唱歌条目做**（跳舞条目的音乐不是主内容，用户没要求）；
      · **失败只记日志 + 标黄，绝不阻断整批**（降级原则与批量其它步骤一致）；
      · **剪映用完即关**（用户 2026-09-16：「完成后需要 关闭 剪映」——常驻会跟 ComfyUI 抢显存）。

    原设计放在 `_mark_delivered` **之后**：条目已经算完成，加字幕只是补一件产物，
    失败了也不该把这一条从「已完成」拉下来。
    """
    item = _item(batch_id, item_id)
    if str(item.get("kind") or "") != "singing":
        return
    clean = Path(str(outputs.get("videoFinal") or ""))
    if not clean.is_file():
        return
    try:
        from . import subtitle_step

        await subtitle_step.add_for_item(batch_id, item_id, clean)
    except Exception as error:                      # noqa: BLE001 —— 兜底，绝不影响出片
        batch_store.add_item_log(batch_id, item_id, f"加字幕步骤异常（不影响出片）：{error}")


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


# 出片/备料的**类型优先级**（用户 2026-09-20：「如果有唱歌和跳舞的视频，跳舞优先」）：
# 挑选下一条活时跳舞排在唱歌前面，同类型内部保持原顺序（= 各自的编号顺序）。
# 只影响"先跑谁"，不影响编号（`kindIndex` 按类型各自编）与发布目录名。
PRIORITY_KINDS = ("dance", "singing")


def _priority_sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按类型优先级稳定排序：跳舞 → 唱歌 → 其它（同类型内保持原顺序）。"""
    order = {kind: index for index, kind in enumerate(PRIORITY_KINDS)}
    return sorted(items, key=lambda item: order.get(str(item.get("kind") or ""), len(order)))


def _next_work(state: dict[str, Any], *, allow_confirmed: bool = True) -> dict[str, Any] | None:
    """下一个要跑的任务：先备料（pending / revising），再出片（confirmed）；**跳舞优先**。

    **队列不会自己跑**：只有用户点了「开跑」（`POST /api/batches/{id}/start`）才会 spawn
    runner（2026-09-10 用户：「加入队列并不是马上开跑，需要由我点击总的开跑按钮才开始」）。
    `awaiting_review` 是在等用户上传图片并确认，不算可跑任务。

    ⚠️ `run_batch` 的出片那一路用的是 `_next_confirmed`（"出片优先"），这个函数只用于
    备料与其它调用方；两边的类型优先级必须一致（都走 `_priority_sorted`）。
    """
    items = _priority_sorted(state.get("items") or [])
    for statuses in ({"pending", "revising"}, {"confirmed"}):
        if not allow_confirmed and statuses == {"confirmed"}:
            continue
        for item in items:
            if item.get("status") in statuses:
                return item
    return None


def _next_confirmed(state: dict[str, Any]) -> dict[str, Any] | None:
    """队列里**最早的一条「已确认、等出片」**（**跳舞优先**，同类型内按编号顺序）。

    与 `_next_work` 分开是有意的：那个函数是"先备料再出片"的顺序，而 runner 的 ③a 要的是
    **出片优先** —— 只要有一条已经备好，就立刻交给 GPU，别让它去等备料。
    类型优先级两边一致（用户 2026-09-20：「如果有唱歌和跳舞的视频，跳舞优先」）。
    """
    for item in _priority_sorted(state.get("items") or []):
        if str(item.get("status")) == "confirmed":
            return item
    return None


def _fail_item_and_batch(
    batch_id: str, item_id: str, message: str, *, generation: int | None = None
) -> None:
    """条目级失败：条目标错 + 整个批次停下等人工处理（与 runner 内联分支同一套收尾）。

    出片改到后台任务后，失败可能从任务里抛出来，收尾逻辑必须和原来内联时完全一致。

    `generation` 是调用它的那一代 runner：**被顶掉的旧 runner 不许再写失败** ——
    用户已经在「重新开始」之后跑新的一轮了，旧 runner 把一个已经重置成 confirmed 的
    条目改回 failed，会直接把新一轮的出片丢掉（2026-09-16 加）。
    """
    if generation is not None and batch_run_superseded(batch_id, generation):
        return
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


# 失败策略（用户 2026-09-20 定）：出片失败自动重试 1 次（共 2 次尝试）；
# 备料失败直接跳过；**连续 2 条失败**才熔断停批（避免把剩下十几条全毁一遍）。
MAX_ITEM_VIDEO_ATTEMPTS = 2
FAILURE_STREAK_LIMIT = 2


def _reset_failure_streak(batch_id: str) -> None:
    """一条真正交付成功后把「连续失败」清零（熔断只看**连续**失败）。"""
    state = batch_store.get(batch_id) or {}
    if int(state.get("failureStreak") or 0):
        batch_store.update(batch_id, failureStreak=0, lastFailure="")


def _soft_fail_item(
    batch_id: str, item_id: str, stage: str, message: str, *, generation: int | None = None
) -> bool:
    """一条失败但**不拖住整批**：标失败 + 记连续失败数；返回是否已熔断（True = 调用方 return）。

    与 `_fail_item_and_batch`（一条失败就停整批）的区别就是这一条：备料失败、出片重试后
    仍失败都走这里，批次继续往下跑；只有连续 `FAILURE_STREAK_LIMIT` 条失败才停下等人工 ——
    连续失败通常意味着环境坏了（ComfyUI 挂了 / 中转站欠费），继续跑只会把后面的全毁一遍。
    """
    if generation is not None and batch_run_superseded(batch_id, generation):
        return False        # 旧代 runner 不许写任何状态
    _set_item(batch_id, item_id, status="failed", stage="failed", error=message, childJob=None)
    active_milestone = next(
        (row for row in _item(batch_id, item_id)["milestones"] if row.get("status") == "running"),
        None,
    )
    if active_milestone:
        batch_store.set_item_milestone(
            batch_id, item_id, active_milestone["id"], status="error", currentNode=message
        )
    batch_store.add_item_log(
        batch_id, item_id, f"这一条失败（{stage}），已跳过、继续处理后面的条目：{message}"
    )
    state = batch_store.get(batch_id) or {}
    streak = int(state.get("failureStreak") or 0) + 1
    tripped = streak >= FAILURE_STREAK_LIMIT
    batch_store.update(batch_id, failureStreak=streak, lastFailure=f"{stage}：{message}"[:200])
    if tripped:
        batch_store.add_item_log(
            batch_id, item_id, f"连续 {streak} 条失败，已停下等人工处理（避免把后面的条目全毁一遍）。"
        )
        batch_store.update(
            batch_id,
            status="failed",
            stage="failed",
            runnerActive=False,
            notice=f"连续 {streak} 条失败（最近一次在「{stage}」），已停下等你处理。",
        )
    return tripped


def _retry_item_video_once(batch_id: str, item_id: str, message: str, *, attempts: int) -> None:
    """出片失败 → 把这一条放回 `confirmed` 再试一次（里程碑也重置回待办）。"""
    def apply(row: dict[str, Any]) -> None:
        row.update(
            videoAttempts=attempts + 1,
            status="confirmed",
            stage="confirmed",
            error=None,
            warning=None,
            childJob=None,
            videoJobId=None,
            finishedAt=None,
        )
        for milestone in row.get("milestones") or []:
            if milestone.get("id") == "video":
                milestone.update(status="pending", progress=0, currentNode=None, finishedAt=None)

    batch_store.mutate_item(batch_id, item_id, apply)
    batch_store.add_item_log(
        batch_id, item_id, f"出片失败，自动重试一次（第 {attempts + 2} 次尝试）：{message}"
    )


async def run_batch(batch_id: str) -> None:
    """批次 runner：**出片优先、备料并行一条** —— 第一条备好就开跑，出片期间继续备下一条。

    用户 2026-09-20 的最终口径：「**第一条备好之后优先出片，出片的过程中一条一条备料**」。
    所以 runner 手里最多同时有两个后台任务：

    - `video_task`（**全局只有一条**，出片是 GPU 独占的单链路）；
    - `prep_task`（**并发 1**，一次只备一条：下载 → 抽帧 → 分析 → 人物图 → 两张封面）。

    每一轮先看有没有「已确认」的条目 → 有就立刻出片（优先）；否则看有没有要备料的 →
    丢进后台备料，然后回到循环顶继续转。这样 GPU 一交付，下一条**已经备好**，不用再等
    2~5 分钟的备料（这一段以前是完全串在出片前面的，GPU 白等）。

    失败策略（用户同一天定）：备料失败跳过继续、出片失败自动重试 1 次、**连续 2 条失败
    熔断停批** —— 见 `_soft_fail_item`。

    **每一轮都认「代」**：用户点「重新开始」会推进代数（`take_over_batch_run`），
    这一代过期的 runner 立刻退出，把批次让给新 runner —— 否则旧 runner 一旦卡在某个
    子任务上，`_RUNNING_BATCHES` 就永远摘不掉，之后所有「重新开始」都变成静默 no-op。
    """
    generation = claim_batch_run(batch_id)
    if generation == RUN_CLAIM_BUSY:
        # 已经被别的 runner 占着：路由层会先 `take_over_batch_run` 再 spawn，走到这里说明
        # 是重复 spawn，直接退出即可（**绝不改任何状态**：那一批正由别人推进）。
        return
    video_task: asyncio.Task | None = None
    video_row: dict[str, Any] | None = None
    prep_task: asyncio.Task | None = None
    prep_row: dict[str, Any] | None = None
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
            # ⓪ 我这一代被顶掉了（用户点了重新开始 / 继续）：**立刻退出，不碰任何状态**。
            #    出片任务若还在跑也不去动它：新一代会按条目的当前状态重新安排。
            #    **备料任务则要取消并把条目放回 pending**：它跑在后台、结果只落在这一条自己
            #    身上，留着它会让条目永远停在 `running`（新一代看它是 running 就不挑它了）。
            if batch_run_superseded(batch_id, generation):
                _hand_over_stale_video_task(batch_id, video_task, video_row)
                _cancel_stale_prep_task(batch_id, prep_task, prep_row)
                return
            # ① 后台出片落定：在这里 await 一次，让失败/取消复用下面同一套收尾分支。
            if video_task is not None and video_task.done():
                finished, row = video_task, video_row
                video_task, video_row = None, None
                landed_id = str((row or {}).get("id") or "")
                try:
                    await finished
                except asyncio.CancelledError:
                    # await 期间可能已经被换了一代：那就什么都不许写（新一轮有自己的安排）
                    if batch_run_superseded(batch_id, generation):
                        return
                    current = _item(batch_id, landed_id)
                    await _finish_abandoned(
                        batch_id, current["id"], deleted=bool(current.get("deleteRequested"))
                    )
                except Exception as error:
                    if batch_run_superseded(batch_id, generation):
                        return
                    # 出片失败：先自动重试一次；仍失败就**只标这一条失败**、继续下一条
                    # （连续两条才熔断，见 `_soft_fail_item`）。
                    attempts = int((row or {}).get("videoAttempts") or 0)
                    if attempts + 1 < MAX_ITEM_VIDEO_ATTEMPTS:
                        _retry_item_video_once(batch_id, landed_id, str(error), attempts=attempts)
                    elif _soft_fail_item(
                        batch_id, landed_id, "出片", str(error), generation=generation
                    ):
                        return
                else:
                    # 出片正常返回却没把条目推进出 confirmed（异常路径之外不该发生）：
                    # 再挑一次就会无限重投出片任务，直接停下等人工处理。
                    landed = next(
                        (
                            item
                            for item in (batch_store.get(batch_id) or {}).get("items") or []
                            if item.get("id") == landed_id
                        ),
                        None,
                    )
                    if landed is not None and landed.get("status") == "confirmed":
                        if _soft_fail_item(
                            batch_id,
                            landed_id,
                            "出片",
                            "出片任务结束后条目仍停在「已确认」，已跳过避免重复出片",
                            generation=generation,
                        ):
                            return
                    else:
                        _reset_failure_streak(batch_id)      # 真交付成功了：连续失败清零
                continue
            # ①b 后台备料落定：备料失败**不拖住整批**（标黄跳过继续），取消/DuplicateItem
            #     仍然按老规矩收尾。
            if prep_task is not None and prep_task.done():
                finished, row = prep_task, prep_row
                prep_task, prep_row = None, None
                prep_id = str((row or {}).get("id") or "")
                try:
                    await finished
                except asyncio.CancelledError:
                    if batch_run_superseded(batch_id, generation):
                        return
                    current = _item(batch_id, prep_id)
                    await _finish_abandoned(
                        batch_id, current["id"], deleted=bool(current.get("deleteRequested"))
                    )
                except DuplicateItem as duplicate:
                    _skip_duplicate_item(batch_id, prep_id, str(duplicate))
                except Exception as error:
                    current = _item(batch_id, prep_id)
                    if current.get("skipRequested") or current.get("deleteRequested"):
                        await _finish_abandoned(
                            batch_id, prep_id, deleted=bool(current.get("deleteRequested"))
                        )
                    elif _soft_fail_item(
                        batch_id, prep_id, "备料", str(error), generation=generation
                    ):
                        return
                continue
            # ② 暂停不打断正在出片/正在备料的那一条（等它落定再停；备料是幂等的一整段，
            #    做到一半硬掐会留下半截结果，所以同样等它自然结束）。
            if state.get("pauseRequested") or state.get("status") == "paused":
                if video_task is not None or prep_task is not None:
                    await asyncio.wait(
                        {task for task in (video_task, prep_task) if task is not None}, timeout=3
                    )
                    continue
                batch_store.update(batch_id, status="paused", runnerActive=False, notice="批次已暂停。")
                return
            # ③a **出片优先**：没有出片在跑、队列里有「已确认」的条目 → 立刻开跑。
            #     用户 2026-09-20：「第一条备好之后优先出片」。注意这里**不能**用 `_next_work`：
            #     它是"先备料（pending/revising）、再出片"的顺序，会先挑到待备料的那一条，
            #     于是已经备好的条目反而要等备料做完才轮到 GPU。
            if video_task is None:
                pick = _next_confirmed(state)
                if pick is not None:
                    task = asyncio.create_task(_process_confirmed(batch_id, pick["id"]))
                    _VIDEO_TASKS.add(task)
                    task.add_done_callback(_VIDEO_TASKS.discard)
                    video_task, video_row = task, pick
                    batch_store.update(
                        batch_id,
                        currentItemId=pick["id"],
                        currentIndex=pick["index"],
                        notice=_running_notice(pick, state, prep_row),
                    )
                    continue
            # ③b **备料并行一条**：出片在跑时照样备下一条（一次只备一条，API 不会被洪峰打）。
            if prep_task is None:
                candidate = _next_work(state, allow_confirmed=False)
                if candidate is not None:
                    if candidate.get("skipRequested"):
                        await _finish_abandoned(
                            batch_id, candidate["id"], deleted=bool(candidate.get("deleteRequested"))
                        )
                        # 停下来就把 ComfyUI 关掉（批次里还有别的活时它会自己跳过）
                        await _stop_comfy_if_idle()
                        continue
                    task = asyncio.create_task(_run_prep(batch_id, candidate["id"]))
                    _PREP_TASKS.add(task)
                    task.add_done_callback(_PREP_TASKS.discard)
                    prep_task, prep_row = task, candidate
                    batch_store.update(
                        batch_id,
                        currentItemId=candidate["id"],
                        currentIndex=candidate["index"],
                        notice=_running_notice(candidate, state, video_row),
                    )
                    continue
            # ④ 两个后台任务都还在跑、暂时没有新活可挑：等它们落定（每 3 秒回到循环顶，
            #    暂停/取消/换代仍能响应）。
            pending_tasks = {task for task in (video_task, prep_task) if task is not None}
            if pending_tasks:
                await asyncio.wait(pending_tasks, timeout=3)
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
            failed = [row for row in items if row.get("status") == "failed"]
            batch_store.update(
                batch_id,
                status="completed",
                stage="completed",
                runnerActive=False,
                currentItemId=None,
                currentIndex=state.get("total") or 0,
                finishedAt=now_iso(),
                notice="全部条目已完成。"
                + (f" 有 {len(failed)} 条失败，可在表格里点「重新开始」补跑。" if failed else "")
                + (" 部分歌词字幕需要稍后重试。" if warnings else ""),
            )
            return
    finally:
        finish_batch_run(batch_id, generation)


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
    """本进程里还有没有在跑的东西（runner / 出片 / 备料 / 补交成片 / 被换代的旧出片）。

    `_STALE_VIDEO_TASKS`（被「重新开始」换代顶掉的旧 runner 留下的出片任务）也要算：
    它还在跑、还在写文件，这时候排自动关机就是把它连根砍掉。
    """
    return bool(
        _RUNNING_BATCHES or _VIDEO_TASKS or _ITEM_TASKS or _SALVAGE_TASKS or _STALE_VIDEO_TASKS
    )


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
