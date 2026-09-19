from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import secrets
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import httpx

logger = logging.getLogger("uvicorn.error")

# 后台「让 AI 换一张」任务（强引用，防止被 GC）；由 chatgpt_image 的 _IMAGE_LOCK 单链保护。
_REGEN_IMAGE_TASKS: set[asyncio.Task] = set()

from . import input_preview
from . import image_workbench
from .batch_store import batch_store
from . import batch_ai
from .batch_worker import (
    IDENTITY_PATH,
    MAX_LOCAL_SOURCE_BYTES,
    append_batch_items,
    batch_run_alive,
    cancel_item_work,
    cancel_pending_shutdown,
    confirm_batch_items,
    deliver_item_now,
    deliver_review_materials,
    ensure_shutdown_watcher,
    heal_terminal_item_milestones,
    image_ratio_note,
    locked_publish_copy,
    item_ratio,
    mark_items_deleted,
    mark_items_skipped,
    move_batch_item,
    new_batch_state,
    replace_item_source,
    replace_item_source_file,
    request_review_adjustment,
    reset_review_row,
    restart_batch_items,
    restart_item_to_run,
    resume_shutdown_watch,
    run_batch,
    salvage_abandoned_items,
    set_batch_shutdown_on_complete,
    set_item_migrate_mode,
    set_item_ratio,
    set_item_remove_subtitles,
    shutdown_disabled,
    shutdown_status,
    stage_media,
    take_over_batch_run,
)
from .douyin_mirror import all_jobs as mirror_jobs
from .douyin_mirror import get_job as mirror_get_job
from .douyin_mirror import upsert_jobs as mirror_upsert
from .douyin_preview import (
    ensure_download_playable,
    ensure_web_playable_at,
    playable_download_path,
    schedule_download_playable,
)
from .douyin_service import (
    DouyinServiceError,
    DouyinServiceOffline,
    _cookie_stats,
    douyin_service,
    is_douyin_url,
)
from . import publish_watcher
from .pipeline import (
    PipelineError,
    comfy_health,
    estimate_migrate_segments,
    estimate_singing_segments,
    media_metadata,
    pipeline_lock,
    resources,
    retry_voice,
    run_migrate_pipeline,
    resume_migrate_pipeline,
    run_pipeline,
    resume_singing_pipeline,
    run_rvc_job,
    run_upscale_job,
    upscale_target_size,
)
from .settings import (
    COMFY_INPUT,
    COMFY_OUTPUT,
    COMFY_URL,
    BATCH_OUTPUT_ROOT,
    DATA_DIR,
    DEFAULT_SINGING_CANVAS,
    FIXED_REFERENCE,
    MAX_DURATION_SECONDS,
    MIGRATE_REFERENCE,
    PROJECT_ROOT,
    RVC_MODEL,
    SINGING_WORKFLOW,
    UPSCALE_BATCH_FRAMES,
    UPSCALE_MODEL_X4,
    canvas_params,
    normalize_batch_ratio,
    required_paths,
    singing_canvas_params,
)
from .store import (
    initial_milestones,
    migrate_milestones,
    now_iso,
    rvc_milestones,
    store,
    subtitle_milestones,
    upscale_milestones,
)
from .subtitle_step import (  # noqa: E402
    BATCH_MILESTONES,
    request_restart,
    request_publish_batch_pause,
    reset_subtitle_job,
    resume_publish_batch,
    run_publish_subtitle_batch,
    run_subtitle_job,
    scan_singing_publish_videos,
)
from .jianying_recognize import kill_app  # noqa: E402
from .workflows import load_workflow, node_by_id


running_tasks: set[asyncio.Task] = set()
# 本进程真的拉起来过的任务 / 批次（重启后据此认出「数据库里还在 running、但早就没人跑」
# 的僵尸任务 —— 它们会一直占着 `store.active()`，把批量出片的等待循环无限期堵死）。
_SPAWNED_JOB_IDS: set[str] = set()
_SPAWNED_BATCH_IDS: set[str] = set()


def _spawn_owner_id(coroutine) -> str:
    """从后台协程里认出它属于哪个任务 / 批次（认不出来就返回空串）。

    只对 `run_*` / `retry_voice` 这几个「任务工厂」协程生效：它们的协程帧里有
    `job_id` / `batch_id` 参数。用在**服务启动自检**上 —— 数据库里还写着 running、
    本进程却从没 spawn 过的，就是上一个进程留下的僵尸任务。
    """
    frame = getattr(coroutine, "cr_frame", None)
    if frame is None:
        return ""
    name = str(getattr(getattr(coroutine, "cr_code", None), "co_name", "") or "")
    if not (name.startswith("run_") or name == "retry_voice"):
        return ""
    local = frame.f_locals
    # 顺序要紧：`run_batch(batch_id, ...)` 这种把批次当第一个参数的，`job_id` 根本不在
    # 局部变量里，所以按批次优先判断（两者同名的情况不可能出现）。
    for key in ("batch_id", "job_id"):
        value = str(local.get(key) or "")
        if value:
            return value
    return ""


def _spawn_is_batch(coroutine) -> bool:
    """这条后台协程代表的是**批次** runner 还是单个任务。"""
    frame = getattr(coroutine, "cr_frame", None)
    return bool(frame is not None and "batch_id" in frame.f_locals)


def spawn(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    task.add_done_callback(running_tasks.discard)
    running_tasks.add(task)
    # 记下这条协程对应哪个任务 / 批次（拿不到就放弃记录，绝不让记账本身影响任务）。
    try:
        owner = _spawn_owner_id(coroutine)
        is_batch = _spawn_is_batch(coroutine)
    except Exception:  # noqa: BLE001 - 记账失败不影响 spawn 本身
        return
    if not owner:
        return
    if is_batch:
        _SPAWNED_BATCH_IDS.add(owner)
    else:
        _SPAWNED_JOB_IDS.add(owner)


def _active_subtitle_job() -> dict[str, Any] | None:
    """正在跑 / 排队 / 取消中的「生成字幕」任务（单条或一键批量），没有就 None。

    单条与一键批量共用同一把互斥：剪映一次只能占一条。`store.active_all()` 只覆盖
    queued/running，这里再并上 `store.waiting()`（排队）与最新字幕任务的 cancelling 态。
    """
    for _job in store.active_all() + store.waiting():
        if _job.get("kind") in ("subtitle", "subtitle_batch"):
            return _job
    for _kind in ("subtitle", "subtitle_batch"):
        latest = store.latest(_kind)
        if latest and latest.get("status") == "cancelling":
            return latest
        # 一键批量暂停后也算「占用」：要先「继续」或取消，才能再开新的字幕任务，
        # 免得一条暂停的批量跟后面新起的单条/批量抢剪映
        if _kind == "subtitle_batch" and latest and latest.get("status") == "paused":
            return latest
    return None


# 成片入口（media key → (state 字段, 就绪标记, 显示名)）：只暴露「最终成片」与
# 「原版成片」两个入口；去字幕视频/迁移成片/高清成片等中间产物继续写状态与磁盘，
# 但不再出现在结果区与最近任务列表里。
OUTPUT_MEDIA_FIELDS = (
    ("final", "finalOutput", "finalReady", "最终成片"),
    ("original", "originalOutput", "originalReady", "原版成片"),
)

# 「从最近任务选」的 source_key 解析（含中间产物键：兼容历史任务与旧链接）
MEDIA_FIELD_BY_KEY = {
    "final": "finalOutput",
    "original": "originalOutput",
    "draft": "draftOutput",
    "clean": "cleanOutput",
    "enhanced": "enhancedOutput",
}


def _job_media_entries(state: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for key, field, flag, label in OUTPUT_MEDIA_FIELDS:
        if not state.get(flag) or not state.get(field):
            continue
        path = Path(state[field]).resolve()
        if not path.is_file():
            continue
        entries.append({
            "key": key,
            "label": label,
            "url": f"/api/jobs/{state['id']}/media/{key}",
        })
    return entries


def _upscale_target(width: int, height: int) -> tuple[int, int]:
    """按源宽高比就近选择 1080p 标准档（实现见 pipeline.upscale_target_size）。"""
    return upscale_target_size(width, height)


def transcode_source_to_30fps(source: Path, target: Path) -> None:
    """把高帧率（60fps 等）源转成 30fps 驱动视频（抽帧，时长不变，保留音频）。

    动作迁移逐帧生成：帧数减半 → 段数与总时长约减半，30fps 出片对抖音足够。
    """
    source = Path(source)
    target = Path(target)
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(source),
            "-vf", "fps=30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(target),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=600,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError("ffmpeg 30fps 转码失败: " + (detail[-1] if detail else f"exit {result.returncode}"))


# The Douyin downloader runs as a separate Python process (hundreds of MB of
# RAM). It is started ONLY by explicit user actions (submitting a download or
# using the login window) and is stopped again after this many seconds of
# inactivity so memory is freed for ComfyUI. Read endpoints serve the on-disk
# job mirror while it is offline.
IDLE_STOP_SECONDS = 60.0


async def _douyin_housekeeping() -> None:
    """Mirror jobs, normalize completed videos, stop the service when idle.

    Runs only while the download service is already connected, so it never
    starts that service on its own. After download activity has been idle for
    ``IDLE_STOP_SECONDS`` it terminates the service to release its memory;
    jobs already mirrored stay visible and playable from disk.
    """
    while True:
        try:
            if not await douyin_service.healthy():
                await asyncio.sleep(4)
                continue
            try:
                payload = await douyin_service.jobs()
            except DouyinServiceError:
                payload = {"jobs": []}
            live = payload.get("jobs", []) if isinstance(payload, dict) else []
            mirror_upsert(live)
            active = any(
                isinstance(job, dict) and job.get("status") in ("pending", "running")
                for job in live
            )
            if active:
                douyin_service.mark_activity()
            for job in live:
                if not isinstance(job, dict) or job.get("status") != "success":
                    continue
                result = douyin_service.result_for(job)
                if result:
                    schedule_download_playable(
                        Path(result["path"]),
                        str(result.get("awemeId") or ""),
                    )
            spawned = douyin_service.process is not None
            if spawned and not active and douyin_service.idle_seconds() > IDLE_STOP_SECONDS:
                logger.info("下载任务已结束且暂无操作，停止抖音下载服务以释放内存")
                await douyin_service.stop()
        except DouyinServiceError:
            pass  # download service is still starting or went away
        await asyncio.sleep(4)


def workflow_defaults() -> tuple[str, str]:
    try:
        workflow = load_workflow(SINGING_WORKFLOW)
        values = node_by_id(workflow, 480).get("widgets_values") or []
        return str(values[1] or ""), str(values[2] or "")
    except (OSError, ValueError, KeyError, IndexError):
        return "", ""


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 本进程是刚起来的：之前记的「我 spawn 过谁」一律作废（换进程了，谁都还没 spawn）。
    _SPAWNED_JOB_IDS.clear()
    _SPAWNED_BATCH_IDS.clear()
    batch_store.interrupt_active()
    # 数据库里还写着 queued/running、可这一轮根本没被 spawn 过的任务，全是上一个进程留下的
    # 僵尸（`interrupt_active()` 只认 `store.active()` 里最新的那一条，挡不住更老的那些）。
    # 2026-09-16 实测：一条僵尸字幕任务（剪映识别那一步随重启没了，状态永远停在 running）
    # 一直占着 `store.active()`，批量的出片等待循环被它无限期堵死 —— 条目全卡在
    # 「已有单链路任务运行，当前条目正在等待资源。」，用户看到的就是「点了重新开始没反应」。
    await _recover_or_interrupt_orphan_jobs()
    # 排队中的字幕任务在重启后没人再接单（runner 随进程一起没了）：标成 interrupted，
    # 页面就能给出「重新开始」（源成片还在任务目录里，不用重新上传）。
    for queued in store.waiting():
        store.update(
            queued["id"],
            status="interrupted",
            stage="failed",
            errorSummary="本地服务在排队期间重启，这次排队已作废",
            errorDetail="源成片仍在任务目录里，点「重新开始」即可重跑，不用重新上传。",
            finishedAt=now_iso(),
        )
    # 批次：这一轮没 spawn 过、却写着 runnerActive 的，runner 早就随进程没了。留着会一路
    # 「看着在跑、实际没人跑」，所以按服务重启的既有语义安全暂停（`interrupt_active()` 只在
    # 当前条目确实在跑时才标 failed，这里补上它漏掉的另一半）。
    for batch in batch_store.active_batches():
        batch_id = str(batch.get("id") or "")
        if batch_id in _SPAWNED_BATCH_IDS or not batch.get("runnerActive"):
            continue
        batch_store.update(
            batch_id,
            status="paused",
            pauseRequested=True,
            runnerActive=False,
            notice="本地服务重启，批次已安全暂停；点「继续」后从当前条目恢复。",
        )
    sweep_task = asyncio.create_task(_douyin_housekeeping())
    # 发布目录监听（2026-09-17 用户要求）：发布目录里出现新的 人物图.png →
    # 自动挂成候选人物图 + 自动「确认并出片」，不用他再点。开关在批量页面右上角
    # （落盘 data/publish_watch.json），H3_WATCH_PUBLISH_PORTRAIT=0/1 可强制。
    watch_task = asyncio.create_task(publish_watcher.watch_loop())
    # 本地服务重启后恢复「全部完成后自动关机」的看护：批次可能早就跑完了，
    # 重启前挂的关机记录也可能还在（Windows 的倒计时不随我们重启消失）。
    resume_shutdown_watch()
    try:
        yield
    finally:
        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass
        watch_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass
        await douyin_service.stop()


app = FastAPI(title="H3 MotionStudio", version="0.1.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    """任何未处理异常都回 **JSON** 并留下 traceback。

    2026-09-13 用户实测「Unexpected token 'I', "Internal S"... is not valid JSON」：FastAPI 默认把
    未处理异常回成纯文本 `Internal Server Error`，前端 `response.json()` 于是抛这句解析错误，
    真正的原因（哪个端点、什么异常）完全看不到 —— 而且当时后端由启动脚本拉起、输出没落盘，
    连 traceback 都没了。这里统一回 `{"detail": ...}`（HTTP 500）并把堆栈写进日志。
    """
    logger.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"服务内部错误（{type(exc).__name__}）：{str(exc)[:200] or '没有更多信息'}",
            "path": str(request.url.path),
        },
    )


class DouyinDownloadRequest(BaseModel):
    url: str


class BatchCreateRequest(BaseModel):
    singingUrls: list[str] = Field(default_factory=list)
    danceUrls: list[str] = Field(default_factory=list)
    # 分组默认比例（页面上的选择）。每条视频仍可单独改：歌曲默认 4:3、跳舞默认 9:16。
    singingRatio: str | None = None
    danceRatio: str | None = None
    # 加入队列后是否立即开跑（页面就是这种：点「加入队列」直接开始准备）
    autoStart: bool = False
    # 整批跑完后是否自动关机（页面开关，默认关闭）
    shutdownOnComplete: bool = False


class BatchShutdownRequest(BaseModel):
    """「所有任务完成后自动关机」开关（批次级，默认关闭）。"""

    enabled: bool


class BatchItemRatioRequest(BaseModel):
    ratio: str


class BatchItemSubtitlesRequest(BaseModel):
    """跳舞条目的「是否去除字幕」开关。"""

    removeSubtitles: bool


class BatchItemMigrateModeRequest(BaseModel):
    """跳舞条目的迁移模式：animation=动作迁移（默认）/ replacement=人物替换。"""

    mode: str


class BatchAppendRequest(BaseModel):
    """给已有批次追加任务：批次跑着、暂停着、等审核时都能加。"""

    singingUrls: list[str] = Field(default_factory=list)
    danceUrls: list[str] = Field(default_factory=list)
    singingRatio: str | None = None
    danceRatio: str | None = None
    autoStart: bool = False


class BatchItemSourceRequest(BaseModel):
    """替换这一条的源视频（放错槽位 / 贴错链接时不用删了重加）。"""

    url: str
    kind: str | None = None


class BatchAdjustRequest(BaseModel):
    feedback: str
    mode: str = "both"


class BatchRegenImageRequest(BaseModel):
    """「让 AI 换一张」的可选修改意见（不填就是重新生成一张全新的）。"""

    feedback: str = ""


class BatchItemMoveRequest(BaseModel):
    """队列里上移 / 下移一条还没开始的条目（调处理顺序）。"""

    direction: str


class BatchItemIdsRequest(BaseModel):
    """批量操作：一次确认 / 跳过 / 删除多条条目（出片仍严格一条一条）。"""

    itemIds: list[str] = Field(default_factory=list)


def _batch_or_404(batch_id: str) -> dict[str, Any]:
    state = batch_store.get(batch_id)
    if not state:
        raise HTTPException(404, "批次不存在")
    return state


def _batch_item_or_404(batch_id: str, item_id: str) -> dict[str, Any]:
    state = _batch_or_404(batch_id)
    for item in state.get("items") or []:
        if item.get("id") == item_id:
            return item
    raise HTTPException(404, "批次条目不存在")


def _resume_batch(batch_id: str, notice: str) -> dict[str, Any]:
    """让这一批**立刻跑起来**（用户点了准备任务 / 继续 / 确认 / 重新开始都会走这里）。

    已经有活 runner 时只更新队列状态，**绝不换代再起一个**。用户连续确认多条时，旧实现
    每确认一次都 `take_over_batch_run + spawn`，第一条视频仍在旧 runner 里生成，新 runner
    却已经开始推进第二条，破坏了视频链路串行。当前 runner 会在这一条结束后自然捡起新状态。

    只有确实没有 runner 时才推进代数并 spawn；这仍保留服务重启、失败后继续与卡住批次
    自愈所需的接管能力。
    """
    runner_alive = batch_run_alive(batch_id)
    state = batch_store.update(
        batch_id,
        status="running",
        stage="running",
        pauseRequested=False,
        runnerActive=False,
        finishedAt=None,
        notice=notice,
    )
    if not runner_alive:
        take_over_batch_run(batch_id)
        spawn(run_batch(batch_id))
    return state


def _pending_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in state.get("items") or []
        if item.get("status") in {"pending", "revising", "confirmed"}
    ]


def _interrupt_orphan_jobs() -> None:
    """把「数据库里还写着 queued/running、本进程却没 spawn 过」的任务标成 interrupted。

    这些是上一个进程留下的僵尸：它的 asyncio 任务已经随进程消失，可状态还写着在跑，
    于是一直占着 `store.active()` —— 批量的出片等待循环（`_wait_for_free_pipeline`）
    就被它无限期堵住。标成 interrupted 之后：① 不再挡任何提交；② 页面上会出现
    「重新开始」（源成片还在任务目录里，不用重新上传）；③ 真在跑的那一条不会被误伤
    （它就在 `_SPAWNED_JOB_IDS` 里）。
    """
    try:
        rows = store.active_all()
    except Exception:  # noqa: BLE001 - 启动自检失败不能让服务起不来
        logger.exception("检查残留任务失败（忽略）")
        return
    for state in rows:
        job_id = str(state.get("id") or "")
        if not job_id or job_id in _SPAWNED_JOB_IDS:
            continue
        if state.get("status") not in {"queued", "running"}:
            continue
        store.update(
            job_id,
            status="interrupted",
            stage="failed",
            errorSummary="本地服务曾在任务运行时重启",
            errorDetail="任务状态已保留。源成片或中间成片仍在任务目录里，点「重新开始」即可重跑。",
            finishedAt=now_iso(),
        )
        store.add_log(job_id, "本地服务重启后这一条已没有人继续跑，已标记为中断（可重新开始）。")
        logger.warning("残留任务 %s（%s）已被标记为中断。", job_id[:8], state.get("kind"))


async def _resume_orphan_and_batch(job_id: str) -> None:
    """Reconnect a child job, then restore its batch delivery/runner."""
    owner: tuple[str, str] | None = None
    for batch in batch_store.active_batches():
        for item in batch.get("items") or []:
            if str(item.get("videoJobId") or "") == job_id:
                owner = (str(batch.get("id") or ""), str(item.get("id") or ""))
                break
        if owner:
            break

    if owner:
        # Keep the ordinary batch runner asleep while this recovery watcher owns
        # the current video.  Otherwise `_heal_stalled_batch` may advance to the
        # next confirmed row, which then looks interrupted on another restart.
        batch_store.update(
            owner[0], status="paused", stage="paused", pauseRequested=True,
            runnerActive=False, recoveryActive=True,
            notice="正在续接重启前的生成；后续已确认条目保持等待，完成后自动继续。",
        )

    state = store.get(job_id) or {}
    resume = resume_migrate_pipeline if state.get("kind") == "migrate" else resume_singing_pipeline
    resume_task = asyncio.create_task(resume(job_id))
    # The old batch runner used to mirror the live child state into `childJob`.
    # It died with the backend process, so the recovery watcher must take over
    # that mirror too; otherwise the batch page appears to lose logs/steps.
    while not resume_task.done():
        child = store.get(job_id)
        if owner and child:
            batch_store.mutate_item(
                owner[0], owner[1],
                lambda row, snapshot=child: row.update(
                    status="running", stage="video", childJob=snapshot, error=None, finishedAt=None
                ),
            )
        await asyncio.sleep(2)
    await resume_task
    child = store.get(job_id) or {}
    if owner:
        batch_store.mutate_item(owner[0], owner[1], lambda row: row.update(childJob=child))
    if child.get("status") != "completed":
        return
    if owner:
        try:
            await deliver_item_now(owner[0], owner[1])
            batch_store.update(owner[0], recoveryActive=False)
            _resume_batch(owner[0], "已接回重启前的生成任务并完成交付，继续处理队列。")
        except Exception:
            batch_store.update(owner[0], recoveryActive=False)
            logger.exception("恢复任务完成后整理批次交付失败：%s/%s", owner[0], owner[1])


async def _recover_or_interrupt_orphan_jobs() -> None:
    """Recover live ComfyUI prompts; interrupt only genuine orphan records."""
    try:
        rows = store.active_all()
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{COMFY_URL}/queue")
            response.raise_for_status()
            payload = response.json()
        live_ids = {
            str(entry[1])
            for label in ("queue_running", "queue_pending")
            for entry in (payload.get(label) or [])
            if isinstance(entry, list) and len(entry) > 1
        }
    except Exception:
        logger.exception("检查 ComfyUI 残留任务失败，退回安全中断逻辑")
        _interrupt_orphan_jobs()
        return

    recovered: set[str] = set()
    for state in rows:
        job_id = str(state.get("id") or "")
        kind = str(state.get("kind") or "")
        prompt_id = str((state.get("promptIds") or {}).get(kind) or "")
        if kind in {"singing", "migrate"} and prompt_id and prompt_id in live_ids:
            recovered.add(job_id)
            store.add_log(job_id, "检测到重启前的 ComfyUI 任务仍在运行，正在自动接回，无需重新开始。")
            spawn(_resume_orphan_and_batch(job_id))

    # Mark every non-recoverable active record as interrupted without touching
    # the jobs just adopted by this process.
    for state in rows:
        job_id = str(state.get("id") or "")
        if not job_id or job_id in recovered or job_id in _SPAWNED_JOB_IDS:
            continue
        if state.get("status") not in {"queued", "running"}:
            continue
        store.update(
            job_id, status="interrupted", stage="failed",
            errorSummary="本地服务曾在任务运行时重启",
            errorDetail="ComfyUI 中未找到仍在运行的对应任务，可点「重新开始」重跑。",
            finishedAt=now_iso(),
        )
        store.add_log(job_id, "本地服务重启后未找到仍在运行的 ComfyUI 任务，已标记为中断。")


def _heal_stalled_batch(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """批次卡在「运行中」但这一轮根本没有 runner 在跑时，把它重新拉起来。

    2026-09-16 实测的那种死法：本地服务重启 / 上一轮 runner 被换掉之后，数据库里
    还留着 `status="running"` + `runnerActive=False`，条目全躺在「已确认」等着出片，
    可没有任何东西会来推进它们 —— 页面每 3.5 秒轮询一次，用户看到的永远是原地不动。
    读批次状态时顺手体检一次：确实是「该跑却没人跑」就把 runner 重新 spawn 起来。

    安全边界：只有 `running` 才自愈。`paused`（用户暂停 / 重启保护）与
    `awaiting_review`（等你确认）**绝不**偷偷继续；`runnerActive=True` 且这一轮真的
    spawn 过（`_SPAWNED_BATCH_IDS`）说明 runner 活得好好的，不动它。

    除了「补 runner」，这里顺手把**已经结束的条目上还挂着「进行中」的里程碑**收尾
    （2026-09-17 实测：条目早已「已跳过」，`video` 那一步却还在转圈）。
    """
    if not state:
        return state
    try:
        if heal_terminal_item_milestones(state):
            state = batch_store.get(str(state.get("id") or "")) or state
    except Exception:  # noqa: BLE001 - 体检失败不能把「读状态」这个请求带崩
        logger.exception("收尾终态条目的里程碑失败（忽略）")
    if state.get("status") != "running":
        return state
    batch_id = str(state.get("id") or "")
    if not batch_id:
        return state
    if state.get("runnerActive") and batch_id in _SPAWNED_BATCH_IDS:
        return state
    if batch_run_alive(batch_id):
        return state
    try:
        take_over_batch_run(batch_id)
        spawn(run_batch(batch_id))
        logger.warning("批次 %s 卡在运行中但没有 runner，已自动重新拉起。", batch_id[:8])
    except Exception:  # noqa: BLE001 - 自愈失败不能把「读状态」这个请求带崩
        logger.exception("重新拉起卡住的批次失败（忽略）")
    return state


async def _prewarm_comfy() -> None:
    """点「启动」时先把 ComfyUI 拉起来（用户 2026-09-10 要求）。

    在 `pipeline_lock` 里启动，避免和正在跑的任务同时拉起两个 ComfyUI 抢同一个端口；
    预热失败只记日志不拦批次——备料阶段（下载 + 分析 + 备图）本来就不需要 ComfyUI，
    真正出片时 pipeline 会再 ensure 一次并报出真实错误。
    """
    try:
        async with pipeline_lock:
            await resources.ensure_comfy()
    except Exception:  # noqa: BLE001 - 预热是尽力而为，不能拖累队列
        logger.warning("ComfyUI 预热失败（不影响备料，出片时会重试启动）", exc_info=True)


def _wake_batch(batch_id: str, notice: str) -> dict[str, Any]:
    """按需唤醒 runner：只有**用户主动开始**的批次才会继续跑。

    页面上的开始动作是输入区的「准备任务」（入队 + 立即开始）；API 侧可以不带
    `autoStart` 只入队，那时这里只改状态和提示：
    - `running`：runner 还活着，会自己捡起新条目；**要是没人活着就补一个**
      （跳过的条目被重新开始时走的就是这条路，绝不能只是改个提示语就完事）；
    - `paused`：用户主动暂停，不偷偷继续；
    - 其它（queued / awaiting_review / completed / failed）：等用户再点「准备任务」
      （或 API 调 `/start`）。
    """
    state = batch_store.get(batch_id) or {}
    status = state.get("status")
    if status == "running":
        if not batch_run_alive(batch_id):
            take_over_batch_run(batch_id)
            batch_store.update(batch_id, pauseRequested=False, runnerActive=False)
            spawn(run_batch(batch_id))
        return batch_store.update(batch_id, notice=notice)
    if status == "paused" or state.get("pauseRequested"):
        return batch_store.update(
            batch_id, notice=f"{notice} 批次处于暂停，点「继续」后开始处理。"
        )
    return batch_store.update(batch_id, notice=f"{notice} 点「准备任务」后开始处理。")


@app.get("/api/batches/latest")
async def latest_batch():
    state = _heal_stalled_batch(batch_store.latest())
    if not state:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return JSONResponse(state, headers={"Cache-Control": "no-store"})


# ---------------- 发布目录监听（页面右上角勾选框） ----------------
class PublishWatchRequest(BaseModel):
    enabled: bool


@app.get("/api/watch/publish-portrait")
async def publish_watch_state():
    """监听开关状态（页面勾选框用）。"""
    return JSONResponse(
        {
            "enabled": publish_watcher.enabled(),
            "interval": publish_watcher.interval(),
            "root": str(publish_watcher.publish_root()),
            "lastScan": publish_watcher.last_scan(),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/watch/publish-portrait")
async def set_publish_watch(request: PublishWatchRequest):
    """页面勾选框：开/关发布目录监听（落盘 data/publish_watch.json，立即生效）。"""
    publish_watcher.set_enabled(bool(request.enabled))
    return JSONResponse(
        {"enabled": publish_watcher.enabled()}, headers={"Cache-Control": "no-store"}
    )


@app.post("/api/watch/publish-portrait/scan")
async def scan_publish_watch():
    """立刻扫一次（不想等那 5 秒时用）。返回这一轮真正处理了哪几条。"""
    handled = await publish_watcher.scan_now()
    return JSONResponse(
        {
            "enabled": publish_watcher.enabled(),
            "handled": [
                {"itemId": action.item_id, "title": action.title,
                 "image": action.image.name, "mode": action.mode}
                for action in handled
            ],
            "lastScan": publish_watcher.last_scan(),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/batches")
async def create_batch(request: BatchCreateRequest):
    active = batch_store.active()
    if active:
        raise HTTPException(409, "已有未完成批次，请先继续、完成或删除其中的条目")
    singing = [url.strip() for url in request.singingUrls if url.strip()]
    dance = [url.strip() for url in request.danceUrls if url.strip()]
    urls = singing + dance
    if not urls:
        raise HTTPException(400, "请至少填写一条抖音链接")
    if len(urls) > 50:
        raise HTTPException(400, "一次最多处理 50 条链接")
    invalid = next((url for url in urls if not is_douyin_url(url)), None)
    if invalid:
        raise HTTPException(400, f"不是有效的抖音链接：{invalid[:80]}")
    try:
        singing_ratio = normalize_batch_ratio(request.singingRatio, "singing")
        dance_ratio = normalize_batch_ratio(request.danceRatio, "dance")
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    state = new_batch_state(singing, dance, singing_ratio, dance_ratio, request.shutdownOnComplete)
    batch_store.create(state)
    if request.shutdownOnComplete:
        # 开关在建批次时就已经打开：立刻挂上看护任务（批次跑完那一刻才真的排关机）
        ensure_shutdown_watcher()
    if request.autoStart:
        # 点「加入队列」直接开跑：预热 ComfyUI 后按队列逐条准备（下载 → 出图/文案 → 等确认）
        spawn(_prewarm_comfy())
        return _resume_batch(
            state["id"], f"已加入队列 {len(state['items'])} 条，正在下载并生成人物图与发布文案。"
        )
    # 只入队（API 用）：等用户点「开跑」
    return batch_store.update(
        state["id"],
        notice=f"已把 {len(state['items'])} 条任务加入队列，点「开跑」后开始处理。",
    )


@app.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str):
    return _heal_stalled_batch(_batch_or_404(batch_id))


@app.post("/api/batches/{batch_id}/start")
async def start_batch(batch_id: str):
    """点「开跑」：整个队列开始按顺序处理（队列不会自己跑）。

    队列流程（用户 2026-09-10 确认）：下载抖音视频 → 生成人物图素材与发布文案 → 等你的确认 →
    确认后才出片。预处理失败只记日志不拦批次——备料阶段本来不需要 ComfyUI，
    真正出片时 pipeline 会再 ensure 一次并报出真实错误。
    """
    state = _batch_or_404(batch_id)
    if state.get("status") == "cancelled":
        raise HTTPException(409, "批次已取消，请新建一个批次")
    if not _pending_items(state):
        raise HTTPException(409, "队列里没有待处理的任务")
    spawn(_prewarm_comfy())
    return _resume_batch(batch_id, "已开跑：正在预热 ComfyUI 并按队列逐条处理。")


@app.post("/api/batches/{batch_id}/items")
async def append_batch_items_endpoint(batch_id: str, request: BatchAppendRequest):
    """随时给已有批次追加任务（用户 2026-09-10：「可以让我随时添加新的任务」）。

    批次完成、失败、暂停、等审核时都能加：新条目排在队尾，已删除的同链接可以再加，
    未删除的重复链接自动跳过。只有「已取消」的批次不能复活，需要新建批次。
    """
    state = _batch_or_404(batch_id)
    if state.get("status") == "cancelled":
        raise HTTPException(409, "批次已取消，请新建一个批次")
    singing = [url.strip() for url in request.singingUrls if url.strip()]
    dance = [url.strip() for url in request.danceUrls if url.strip()]
    invalid = next((url for url in singing + dance if not is_douyin_url(url)), None)
    if invalid:
        raise HTTPException(400, f"不是有效的抖音链接：{invalid[:80]}")
    try:
        result = append_batch_items(
            batch_id, singing, dance, request.singingRatio, request.danceRatio
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    added = int(result.get("added") or 0)
    duplicates = int(result.get("duplicates") or 0)
    note = f"已加入 {added} 条新任务，已排到队尾。"
    if duplicates:
        note += f"（跳过 {duplicates} 条已经在队列里的重复链接）"
    if not added:
        return batch_store.update(batch_id, notice=note)
    state = batch_store.get(batch_id) or state
    if request.autoStart and state.get("status") != "paused" and not state.get("pauseRequested"):
        # 点「加入队列」直接开跑：预热 ComfyUI 后按队列顺序准备（下载 → 出图/文案 → 等确认）
        spawn(_prewarm_comfy())
        return _resume_batch(batch_id, f"{note} 正在下载并生成人物图与发布文案。")
    # 没启动过 / 暂停中的批次只入队，不因为「加任务」就把流程跑起来
    return _wake_batch(batch_id, note)


@app.post("/api/batches/{batch_id}/pause")
async def pause_batch(batch_id: str):
    state = _batch_or_404(batch_id)
    if state.get("status") in {"completed", "cancelled"}:
        raise HTTPException(409, "批次已经结束")
    return batch_store.update(
        batch_id,
        status="paused",
        pauseRequested=True,
        notice="已请求暂停；若当前正在生成视频，会在这一条安全结束后暂停。",
    )


@app.post("/api/batches/{batch_id}/resume")
async def resume_batch(batch_id: str):
    state = _batch_or_404(batch_id)
    if state.get("status") == "awaiting_review":
        raise HTTPException(409, "当前条目正在等待审核，请先确认、调整、跳过或删除")
    if state.get("status") == "completed":
        raise HTTPException(409, "批次已经完成")
    return _resume_batch(batch_id, "批次已继续运行。")


@app.post("/api/batches/{batch_id}/shutdown-on-complete")
async def set_shutdown_on_complete(batch_id: str, request: BatchShutdownRequest):
    """「所有任务完成后自动关机」开关（批次级，**默认关闭**）。

    打开后只是挂上看护任务：真的排关机必须等批次里**没有任何条目还要处理**（排队 / 等你确认 /
    已放行待出片 / 出片中都不算完成），并且至少有一条真的出完了片。倒计时期间（默认 60 秒）
    随时可以「取消关机」，见 `POST /api/system/shutdown/cancel`。
    """
    _batch_or_404(batch_id)
    try:
        return set_batch_shutdown_on_complete(batch_id, bool(request.enabled))
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/system/shutdown")
async def get_shutdown_status():
    """页面顶部的关机倒计时：有没有在倒计时、还剩多少秒。"""
    return JSONResponse(shutdown_status(), headers={"Cache-Control": "no-store"})


@app.post("/api/system/shutdown/cancel")
async def cancel_shutdown():
    """撤销已经排好的自动关机（`shutdown /a`）。"""
    return JSONResponse(
        cancel_pending_shutdown(notice="已取消自动关机。"), headers={"Cache-Control": "no-store"}
    )


@app.post("/api/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    """整批取消：停掉在跑的预审/子任务，未完成的条目全部标记跳过，让用户能开新批次。

    没有这个接口时，failed 批次会被 `batch_store.active()` 一直挡住新建，
    用户只能逐条删除才能继续。
    """
    state = _batch_or_404(batch_id)
    if state.get("status") in {"completed", "cancelled"}:
        raise HTTPException(409, "批次已经结束")
    current_id = state.get("currentItemId")
    if current_id:
        cancel_item_work(batch_id, str(current_id))
    for item in state.get("items") or []:
        child = item.get("childJob") or {}
        child_id = child.get("id")
        if child_id and child.get("status") in {"queued", "running", "cancelling"}:
            try:
                await cancel_job(str(child_id))
            except HTTPException as error:
                if error.status_code not in {404, 409}:
                    raise

    def stop_everything(row: dict[str, Any]) -> None:
        for item in row.get("items") or []:
            if item.get("status") in {"completed", "skipped", "deleted"}:
                continue
            item.update(
                status="skipped",
                stage="skipped",
                finishedAt=now_iso(),
                childJob=None,
            )
            for milestone in item.get("milestones") or []:
                if milestone.get("status") in {"pending", "running"}:
                    milestone["status"] = "skipped"

    batch_store.mutate(batch_id, stop_everything)
    # 已经出片、只是还没整理的条目照样补进发布目录（用户 2026-09-14：取消不能丢成片）
    saved = await salvage_abandoned_items(batch_id)
    note = "批次已取消，可以开始新的批次。"
    if saved:
        note += f" 其中 {saved} 条已经出片，成片与人物图已整理进发布目录。"
    return batch_store.update(
        batch_id,
        status="cancelled",
        stage="cancelled",
        pauseRequested=False,
        runnerActive=False,
        currentItemId=None,
        finishedAt=now_iso(),
        notice=note,
    )


@app.post("/api/batches/{batch_id}/items/{item_id}/confirm")
async def confirm_batch_item(batch_id: str, item_id: str):
    item = _batch_item_or_404(batch_id, item_id)
    if item.get("status") != "awaiting_review":
        raise HTTPException(409, "当前条目不在待确认状态")
    if not str((item.get("ai") or {}).get("reference_image_path") or "").strip():
        raise HTTPException(409, "请先添加上这一条的候选人物图，再确认出片")
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
    return _resume_batch(batch_id, "已确认，正在开始生成这一条视频。")


@app.post("/api/batches/{batch_id}/items/{item_id}/regen-image")
async def regen_batch_item_image(
    batch_id: str, item_id: str, request: BatchRegenImageRequest | None = None,
):
    """「让 AI 换一张」：后台重新生成该条候选人物图并上传替换。

    - 不带意见：把图一+图二+提示词再送一次，生成一张全新的。
    - 带 feedback：把当前候选图 + 图二 + 提示词 + 修改意见一起送 ChatGPT 重出一版。
    受 chatgpt_image 的全局 `_IMAGE_LOCK` 单链保护；生成期间条目 `ai.image_generating=true`，
    前端据此显示「正在生成…」。失败标黄停下等人工。需要 H3_AUTO_CHATGPT_IMAGE=1。
    """
    from . import chatgpt_image as _cgi
    item = _batch_item_or_404(batch_id, item_id)
    status = str(item.get("status") or "")
    if status in {"completed", "deleted"}:
        raise HTTPException(409, "当前条目已经结束")
    if status in {"running", "revising"}:
        raise HTTPException(409, "当前条目正在出片或重新备料，不能换图（先「取消」或「回到确认」）")
    if not _cgi.auto_candidate_enabled():
        raise HTTPException(409, "图片自动化未开启：请先设 H3_AUTO_CHATGPT_IMAGE=1")
    if _cgi.is_generating(batch_id, item_id):
        raise HTTPException(409, "这一条正在生成候选图，请稍候")

    feedback = (request.feedback if request else "") or ""
    task = asyncio.create_task(
        _cgi.auto_regenerate_candidate_image(batch_id, item_id, feedback)
    )
    _REGEN_IMAGE_TASKS.add(task)
    task.add_done_callback(_REGEN_IMAGE_TASKS.discard)
    return _batch_or_404(batch_id)


@app.post("/api/batches/{batch_id}/items/{item_id}/ratio")
async def set_batch_item_ratio(
    batch_id: str, item_id: str, request: BatchItemRatioRequest
):
    """逐条改画布比例：歌曲默认 4:3、跳舞默认 9:16。

    用户 2026-09-14：「未开始前的任务都允许修改」——`pending`/`awaiting_review`/`confirmed`/
    `failed`/`skipped` 都能改，只有正在出片（running/revising）和已出片（completed）拒绝，
    那时请先走「回到确认」。
    """
    _batch_item_or_404(batch_id, item_id)
    try:
        return set_item_ratio(batch_id, item_id, request.ratio)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/batches/{batch_id}/items/{item_id}/remove-subtitles")
async def set_batch_item_remove_subtitles(
    batch_id: str, item_id: str, request: BatchItemSubtitlesRequest
):
    """跳舞条目：改「是否去除字幕」（出片前先跑一遍 ProPainter 去字幕）。

    这个值原先只由预审模型决定、页面上只能看；用户 2026-09-14 要求可以自己改，
    规则与画布比例一致——没开始出片就能改，出片时按当前值提交迁移工作流。
    """
    _batch_item_or_404(batch_id, item_id)
    try:
        return set_item_remove_subtitles(batch_id, item_id, request.removeSubtitles)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/batches/{batch_id}/items/{item_id}/migrate-mode")
async def set_batch_item_migrate_mode(
    batch_id: str, item_id: str, request: BatchItemMigrateModeRequest
):
    """跳舞条目：改「迁移模式」（动作迁移 / 人物替换）。

    用户 2026-09-15：「跳舞可以选择人物迁移吗 现在是动作迁移 生成的效果不好我想看下人物迁移会是
    什么效果」——批量提交跳舞任务时以前把 mode 写死成 animation，现在逐条可选；规则与画布比例
    一致（没开始出片就能改），出片时原样提交给迁移工作流（节点 #353：false=动作迁移 / true=人物替换）。
    """
    _batch_item_or_404(batch_id, item_id)
    try:
        return set_item_migrate_mode(batch_id, item_id, request.mode)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/batches/{batch_id}/items/{item_id}/reopen-review")
async def reopen_batch_item_review(batch_id: str, item_id: str):
    """回到「等待你的确认」：换图 / 改比例 / 改去除字幕后重新确认出片。

    用户 2026-09-14：「我希望可以回到等待你的确认的页面，有可能我需要重新修改内容，
    现在回不去」——出片之后（含正在出片）必须能退回来重做。

    - 没在出片（pending / confirmed / failed / skipped / completed）：直接把条目重置回
      审核点，`reviewApproved` 清空，重新点确认才会再出片；
    - 正在出片（running）：先安全取消子任务（和「跳过」同一条链路），runner 收尾时再把它
      退回审核点，避免和服务里的任务状态打架。用户明确要求允许这么干。
    """
    item = _batch_item_or_404(batch_id, item_id)
    status = str(item.get("status") or "")
    if status == "deleted":
        raise HTTPException(409, "这一条已经删除，不能回到确认")
    if status == "awaiting_review":
        return _batch_or_404(batch_id)
    if status == "revising":
        raise HTTPException(409, "这一条正在重新备料，等它停下来再回到确认")

    decision: dict[str, Any] = {}

    def apply(row: dict[str, Any]) -> None:
        child = row.get("childJob") or {}
        busy = str(row.get("status") or "") == "running" or str(child.get("status") or "") in {
            "queued",
            "running",
            "cancelling",
        }
        decision["busy"] = busy
        if busy:
            # 交给 runner 收尾：_finish_abandoned 看到 reopenRequested 就退回审核点
            row.update(reopenRequested=True, skipRequested=True)
        else:
            reset_review_row(row)

    batch_store.mutate_item(batch_id, item_id, apply)
    if decision.get("busy"):
        cancel_item_work(batch_id, item_id)
        child = item.get("childJob") or {}
        child_id = child.get("id")
        if child_id and child.get("status") in {"queued", "running", "cancelling"}:
            try:
                await cancel_job(str(child_id))
            except HTTPException as error:
                if error.status_code not in {404, 409}:
                    raise
        batch_store.add_item_log(batch_id, item_id, "正在安全取消当前出片，取消后回到等待确认。")
        return batch_store.update(
            batch_id, notice="正在安全取消当前出片，取消后这一条会回到「等待你的确认」。"
        )
    batch_store.add_item_log(
        batch_id, item_id, "已回到「等待你的确认」，可以换图、改比例或改去除字幕后重新确认。"
    )
    state = batch_store.get(batch_id) or {}
    if state.get("status") in {"completed", "cancelled", "failed"}:
        return batch_store.update(
            batch_id,
            status="awaiting_review",
            stage="review",
            currentItemId=item_id,
            runnerActive=False,
            finishedAt=None,
            notice="已回到「等待你的确认」。",
        )
    return batch_store.update(batch_id, notice="已回到「等待你的确认」。")


@app.post("/api/batches/{batch_id}/items/{item_id}/source")
async def replace_batch_item_source(
    batch_id: str, item_id: str, request: BatchItemSourceRequest
):
    """换掉这一条的源视频：放错槽位（唱歌视频贴进了跳舞口）或贴错链接时不用删了重加。

    用户 2026-09-13：确认页能看到「本条源视频」之后追加「要有让我可以替换的操作」。
    替换会把这一条退回 `pending` 重新下载 + 备料（旧的分析/提示词/文案都作废），
    类型可以一起改；已经出片或正在出片的条目要先「取消出片」或「回到确认」。
    """
    _batch_item_or_404(batch_id, item_id)
    try:
        replace_item_source(batch_id, item_id, request.url, request.kind)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return _resume_batch(batch_id, "已替换源视频，正在重新下载并准备候选图与发布文案。")


@app.post("/api/batches/{batch_id}/items/{item_id}/source-file")
async def replace_batch_item_source_file(
    batch_id: str, item_id: str, file: UploadFile = File(...)
):
    """用**本机选择的视频文件**替换这一条的源视频：只换视频，其余内容一律不动。

    用户 2026-09-15：「替换源视频可以让我进行本地选择」+「其他内容都不需要改变只需要改变视频
    而且，所有定义好的内容都不需要变」——和换抖音链接不同，这条路不清空重备料：
    标题/简介/标签、候选人物图、画布比例与去除字幕、动作或迁移提示词、里程碑与当前状态
    全部保持原样，只是出片时驱动的那个视频换成这个文件。
    """
    _batch_or_404(batch_id)
    item = _batch_item_or_404(batch_id, item_id)
    status = str(item.get("status") or "")
    if status in {"running", "revising"}:
        raise HTTPException(409, "这一条正在出片或重新备料，先「取消」再换源视频")
    if status in {"completed", "deleted"}:
        raise HTTPException(409, "这一条已经结束，不能换源视频")
    suffix = Path(file.filename or "source.mp4").suffix.lower()
    if suffix not in VIDEO_UPLOAD_SUFFIXES:
        raise HTTPException(400, "请选择 MP4、MOV、MKV 或 WebM 视频文件。")

    folder = (DATA_DIR / "batches" / batch_id / item_id).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    # 同名不同后缀的历史文件先清掉，避免条目目录里留两份源视频
    for stale in folder.glob("source-local.*"):
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass
    target = folder / f"source-local{suffix}"
    # 分块落盘：本机录像可能很大，不能整段读进内存（ComfyUI 还在占显存/内存）
    total = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_LOCAL_SOURCE_BYTES:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(400, "视频文件超过 1 GB，请换一个更小的文件。")
                handle.write(chunk)
    except HTTPException:
        raise
    except OSError as error:
        target.unlink(missing_ok=True)
        raise HTTPException(500, f"保存视频失败：{error}") from error
    if not total:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "上传的视频是空文件")

    try:
        replace_item_source_file(batch_id, item_id, target)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return _batch_or_404(batch_id)


@app.post("/api/batches/{batch_id}/items/{item_id}/adjust")
async def adjust_batch_item(batch_id: str, item_id: str, request: BatchAdjustRequest):
    try:
        state = request_review_adjustment(batch_id, item_id, request.feedback, request.mode)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    spawn(run_batch(batch_id))
    return state


@app.post("/api/batches/{batch_id}/items/{item_id}/retry")
async def retry_batch_item(batch_id: str, item_id: str):
    """重新开始这条任务：失败 / 已跳过 / **已出片**的条目都能重来。

    用户 2026-09-14：「跳过的视频允许我重新开始」+「开始中的任务允许取消重新开始」——
    取消出片后落在 `skipped`，出片完成后落在 `completed`，两种都要能直接再出一版，
    否则用户只剩「删除这一条」可选。

    - 已经确认过（有审核通过的候选图**且源视频还在磁盘上**）的条目直接回到 `confirmed`，
      只重跑视频链路，不再重复下载与备料；
    - 还没确认的、或者源视频已经被清理掉的，回到 `pending`，从下载/备料重新走一遍。
    两种都会清掉跳过/删除请求、错误与上一次的发布文件记录，并重置「生成最终视频」
    里程碑，让页面上的流程重新变成待办而不是已跳过。
    """
    item = _batch_item_or_404(batch_id, item_id)
    if item.get("status") not in {"failed", "skipped", "completed"}:
        raise HTTPException(409, "只有失败、已跳过或已出片的条目可以重新开始")

    # 重置逻辑与批量「重新开始」共用同一份（batch_worker.restart_item_to_run），
    # 免得单条与批量两边的行为慢慢跑偏。
    direct = restart_item_to_run(batch_id, item_id)
    batch_store.add_item_log(
        batch_id,
        item_id,
        "已重新开始这一条，正在按当前结果继续出片。" if direct else "已重新开始这一条，将从下载与备料重做。",
    )
    return _resume_batch(batch_id, "已重新开始跳过的条目。")

@app.post("/api/batches/{batch_id}/items/{item_id}/skip")
async def skip_batch_item(batch_id: str, item_id: str):
    item = _batch_item_or_404(batch_id, item_id)
    if item.get("status") in {"completed", "skipped", "deleted"}:
        raise HTTPException(409, "当前条目已经结束")
    batch_store.mutate_item(batch_id, item_id, lambda row: row.update(skipRequested=True))
    cancel_item_work(batch_id, item_id)
    # 正在出片的那一条：光设 `skipRequested` 远远不够 —— 子任务在 ComfyUI 里照跑不误，
    # 用户看到的就是「点了取消没效果」（2026-09-15 用户：「点击列表取消怎么没有效果」）。
    # 跟删除端点走同一条路：**真的把子任务取消掉**，pipeline 会在下一个检查点收尾
    # （2026-09-17 事故里，取消只写状态、任务拿到锁又把自己翻回 running 跑了几十分钟）。
    child = item.get("childJob") or {}
    child_id = str(item.get("videoJobId") or child.get("id") or "")
    if child_id and item.get("status") in {"running", "revising"}:
        try:
            await cancel_job(child_id)
        except HTTPException as error:
            if error.status_code not in {404, 409}:
                raise
    if item.get("status") not in {"running", "revising"}:
        def mark_skipped(row: dict[str, Any]) -> None:
            row.update(status="skipped", stage="skipped", finishedAt=now_iso(), childJob=None)
            for milestone in row.get("milestones") or []:
                if milestone.get("status") in {"pending", "running"}:
                    milestone["status"] = "skipped"
        batch_store.mutate_item(batch_id, item_id, mark_skipped)
    return _wake_batch(batch_id, "当前条目已跳过。")


@app.delete("/api/batches/{batch_id}/items/{item_id}")
async def delete_batch_item(batch_id: str, item_id: str):
    item = _batch_item_or_404(batch_id, item_id)
    if item.get("status") == "deleted":
        return _batch_or_404(batch_id)
    child = item.get("childJob") or {}
    child_id = child.get("id")
    child_status = child.get("status")
    batch_store.mutate_item(batch_id, item_id, lambda row: row.update(deleteRequested=True))
    cancel_item_work(batch_id, item_id)
    if child_id and child_status in {"queued", "running", "cancelling"}:
        try:
            await cancel_job(str(child_id))
        except HTTPException as error:
            if error.status_code not in {404, 409}:
                raise
    if item.get("status") not in {"running", "revising"}:
        def mark_deleted(row: dict[str, Any]) -> None:
            row.update(status="deleted", stage="deleted", finishedAt=now_iso(), childJob=None)
            for milestone in row.get("milestones") or []:
                if milestone.get("status") in {"pending", "running"}:
                    milestone["status"] = "skipped"
        batch_store.mutate_item(batch_id, item_id, mark_deleted)
    return _wake_batch(batch_id, "条目已删除。")


@app.post("/api/batches/{batch_id}/items/{item_id}/move")
async def move_batch_item_endpoint(batch_id: str, item_id: str, request: BatchItemMoveRequest):
    """队列里上移 / 下移一条还没开始的条目（调的是处理顺序，出片严格按队列顺序跑）。

    用户 2026-09-15：批量页改成后台表格交互后要能自己调整顺序。正在出片 / 已经出片的
    条目锁死不能动（动了会打乱正在跑的链子），`move_batch_item` 会直接报 400。
    """
    try:
        return move_batch_item(batch_id, item_id, request.direction)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(404, str(error)) from error


@app.post("/api/batches/{batch_id}/items/confirm-many")
async def confirm_batch_items_endpoint(batch_id: str, request: BatchItemIdsRequest):
    """批量确认：一次放行多条「等待确认」且已有候选人物图的条目。

    用户 2026-09-15：批量页批量勾选后一键确认。放行只是排队等出片（出片仍严格一条一条），
    缺候选图 / 不在待确认状态的条目不会放行，原因写进 notice 让页面提示。
    """
    _batch_or_404(batch_id)
    if not request.itemIds:
        raise HTTPException(400, "请先勾选要确认的条目")
    result = confirm_batch_items(batch_id, request.itemIds)
    confirmed = int(result.get("confirmed") and len(result["confirmed"]) or 0)
    skipped = result.get("skipped") or []
    if not confirmed:
        reason = (skipped[0].get("reason") if skipped else "没有可确认的条目")
        return batch_store.update(batch_id, notice=f"没有可确认的条目：{reason}。")
    note = f"已确认 {confirmed} 条，正在按顺序出片。"
    if skipped:
        note += f"（{len(skipped)} 条未放行：{skipped[0].get('reason')}）"
    return _resume_batch(batch_id, note)


@app.post("/api/batches/{batch_id}/items/retry-many")
async def retry_batch_items_endpoint(batch_id: str, request: BatchItemIdsRequest):
    """批量重新开始：失败 / 已跳过 / 已出片的条目一次全部重来。

    用户 2026-09-15：「批量选择选择后加个批量重新开始」。规则与单条「重试 / 重新开始」
    完全一致（共用 `batch_worker.restart_item_to_run`）：已确认过且源视频还在的只重跑出片，
    其余的从下载与备料重做；正在出片的要先「取消」，已删除的不能复活，还没出片的不需要
    重开 —— 后几类逐个记原因返回，页面提示「哪几条没重开、为什么」。出片仍严格一条一条。
    """
    _batch_or_404(batch_id)
    if not request.itemIds:
        raise HTTPException(400, "请先勾选要重新开始的条目")
    result = restart_batch_items(batch_id, request.itemIds)
    restarted = result.get("restarted") or []
    rejected = result.get("rejected") or []
    if not restarted:
        reason = rejected[0].get("reason") if rejected else "没有可重新开始的条目"
        return batch_store.update(batch_id, notice=f"没有可重新开始的条目：{reason}。")
    note = f"已重新开始 {len(restarted)} 条，正在按顺序重新出片。"
    if rejected:
        note += f"（{len(rejected)} 条没重开：{rejected[0].get('reason')}）"
    return _resume_batch(batch_id, note)


@app.post("/api/batches/{batch_id}/items/skip-many")
async def skip_batch_items_endpoint(batch_id: str, request: BatchItemIdsRequest):
    """批量跳过：没在出片的直接跳过；正在出片的请求安全取消，落定后自动落为已跳过。"""
    _batch_or_404(batch_id)
    if not request.itemIds:
        raise HTTPException(400, "请先勾选要跳过的条目")
    result = mark_items_skipped(batch_id, request.itemIds)
    marked = int(result.get("marked") and len(result["marked"]) or 0)
    busy = result.get("busy") or []
    note = f"已跳过 {marked} 条。"
    if busy:
        note += f" 其中 {len(busy)} 条正在出片，已请求安全取消，稍后自动落为已跳过（成片已生成的会保留）。"
    return _wake_batch(batch_id, note)


@app.post("/api/batches/{batch_id}/items/delete-many")
async def delete_batch_items_endpoint(batch_id: str, request: BatchItemIdsRequest):
    """批量删除：没在出片的直接删除；正在出片的先安全取消子任务，成片已生成的照常抢救进发布目录。"""
    _batch_or_404(batch_id)
    if not request.itemIds:
        raise HTTPException(400, "请先勾选要删除的条目")
    result = mark_items_deleted(batch_id, request.itemIds)
    marked = int(result.get("marked") and len(result["marked"]) or 0)
    busy = result.get("busy") or []
    for item_id in busy:
        item = _batch_item_or_404(batch_id, item_id)
        child = item.get("childJob") or {}
        child_id = child.get("id")
        if child_id and child.get("status") in {"queued", "running", "cancelling"}:
            try:
                await cancel_job(str(child_id))
            except HTTPException as error:
                if error.status_code not in {404, 409}:
                    raise
    note = f"已删除 {marked} 条。"
    if busy:
        note += f" 其中 {len(busy)} 条正在出片，已请求安全取消，已生成的成片会保留。"
    return _wake_batch(batch_id, note)


@app.get("/api/batches/{batch_id}/items/{item_id}/image")
async def batch_item_image(batch_id: str, item_id: str):
    item = _batch_item_or_404(batch_id, item_id)
    path = Path((item.get("ai") or {}).get("reference_image_path") or "").resolve()
    if not path.is_file():
        raise HTTPException(404, "候选图片不存在")
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@app.post("/api/batches/{batch_id}/items/{item_id}/image")
async def upload_batch_item_image(
    batch_id: str, item_id: str, file: UploadFile = File(...)
):
    """用户把在 GPT 聊天里做好的图片上传回来，作为该条目的候选人物图。

    默认出图方式是 `manual`：批量只备料（提示词 + 图一 + 图二），成图由用户提供。
    上传后立刻按这张图重写发布文案，保证图文一致，然后仍停在审核点。
    """
    state = _batch_or_404(batch_id)
    item = _batch_item_or_404(batch_id, item_id)
    status = str(item.get("status") or "")
    if status in {"completed", "deleted"}:
        raise HTTPException(409, "当前条目已经结束")
    # 2026-09-15 用户：「只要状态是未完成的任务都可以进行编辑，当然正在运行的那条不允许编辑」——
    # 出片中/重新备料的条目不能换图（否则成片用的是旧图、文案却按新图重写，图文脱节）
    if status in {"running", "revising"}:
        raise HTTPException(409, "当前条目正在出片或重新备料，不能换图（先「取消」或「回到确认」）")
    suffix = Path(file.filename or "candidate.png").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, "只支持 PNG / JPG / WEBP 图片")

    folder = (DATA_DIR / "batches" / batch_id / item_id).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    revision = int(item.get("revision") or 0)
    target = folder / f"candidate_r{revision}_upload{suffix if suffix != '.jpeg' else '.jpg'}"
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "上传的图片是空文件")
    if len(payload) > 25 * 1024 * 1024:
        raise HTTPException(400, "图片超过 25MB")
    target.write_bytes(payload)

    ai = dict(item.get("ai") or {})
    ai["reference_image_path"] = str(target)

    # 图文一致：文案要看着用户给的那张图来写。
    # ⚠️ **封面已经出过的条目不重写文案**（用户 2026-09-19：「都用原来的啊 不然封面都得改」）：
    # 封面上印的就是这份文案，重写会让封面全部作废。全自动链路里发布目录监听也会走这个接口，
    # 少了这道闸，监听就会在封面出好之后把标题改掉。
    locked_copy = locked_publish_copy(item)
    rewrite_note = ""
    if locked_copy:
        for key in ("title", "introduction", "tags"):
            if locked_copy.get(key):
                ai[key] = locked_copy[key]
        rewrite_note = "发布目录里已经出过封面，沿用原来的发布文案（不重写）。"
    elif batch_ai.configured():
        try:
            copy = await batch_ai.write_copy(
                kind=str(item.get("kind") or "singing"),
                candidate_image=target,
                song_name=str(ai.get("song_name") or ""),
                song_mood=str(ai.get("song_mood") or ""),
                description=str((item.get("sourceMetadata") or {}).get("desc") or ""),
            )
            for key in ("title", "introduction", "tags"):
                if copy.get(key):
                    ai[key] = copy[key]
            rewrite_note = "发布文案已按这张图重写。"
        except Exception as error:  # 文案重写失败不影响放行，只记一条日志
            rewrite_note = f"文案重写失败，沿用原文案：{error}"
    else:
        rewrite_note = "未配置文本模型，沿用原文案。"

    ai["tags"] = [
        str(tag).strip().lstrip("#") for tag in ai.get("tags") or [] if str(tag).strip()
    ][:5]
    # 简介 / 标签缺了就自动生成（模型没配、模型返回空、或原分析降级成空文案时都会缺）
    metadata = item.get("sourceMetadata") or {}
    filled = batch_ai.ensure_copy_fields(
        ai,
        kind=str(item.get("kind") or "singing"),
        description=str(metadata.get("desc") or ""),
        source_tags=[str(tag) for tag in metadata.get("tags") or []],
    )
    if filled:
        rewrite_note = f"{rewrite_note} {'与'.join(filled)}为空，已自动生成。".strip()

    def apply(row: dict[str, Any]) -> None:
        row["ai"] = ai
        # 标题只有一个来源：`ai.title`（发布文案/发布目录都用它）。`write_copy` 可能刚改过它，
        # 这里同步到条目标题，免得左侧队列和审核面板显示两个不同的标题。
        if str(ai.get("title") or "").strip():
            row["title"] = str(ai["title"]).strip()
        row["warning"] = None

    batch_store.mutate_item(batch_id, item_id, apply)
    batch_store.add_item_log(batch_id, item_id, f"已收到你上传的候选图：{target.name}。{rewrite_note}")
    # 换图/重写文案后，发布目录里的「人物图 + 发布文案」同步刷新（审核点已先落盘）
    await asyncio.to_thread(deliver_review_materials, batch_id, item_id)
    # 图的比例和本条选定的画布比例不一致时提示（不拦截：最终构图由工作流缩放处理）
    mismatch = image_ratio_note(target, item_ratio(_batch_item_or_404(batch_id, item_id)))
    if mismatch:
        batch_store.add_item_log(batch_id, item_id, mismatch)
    return _batch_or_404(batch_id)


@app.get("/api/batches/{batch_id}/items/{item_id}/stage/{key}")
async def batch_item_stage(batch_id: str, item_id: str, key: str, download: bool = Query(False)):
    """只读回看某一阶段的产物，供用户逐阶段检查生成结果（不提供任何修改入口）。

    key：`source` 源视频 / `candidate` 候选人物图 / `draft` 迁移草稿 /
    `clean` 去字幕视频 / `original` 原版成片 / `enhanced` 二采高清 /
    `final` 最终成片 / `lyrics` 歌词字幕版。
    """
    item = _batch_item_or_404(batch_id, item_id)
    ai = item.get("ai") or {}
    if key == "source":
        raw = str(item.get("sourcePath") or "")
    elif key == "candidate":
        raw = str(ai.get("reference_image_path") or "")
    else:
        media = dict(item.get("stageMedia") or {})
        if not media:
            # 兼容旧状态：早期版本的条目没有 stageMedia，直接从子任务（视频/歌词）
            # 的状态里现算一次，这样已经跑完的历史条目也能回看。
            for job_id in (item.get("videoJobId"), item.get("lyricJobId")):
                if not job_id:
                    continue
                child = store.get(str(job_id))
                if child:
                    media.update(stage_media(child))
        raw = str(media.get(key) or "")
    path = Path(raw)
    if not raw or not path.is_file():
        raise HTTPException(404, "该阶段的产物还不存在")
    # 源视频预览要能直接在浏览器里播：本机选的视频可能是 HEVC 等浏览器不支持的编码，
    # 这里按需转一份 H.264 副本（出片链路仍然用用户给的原文件；抖音源本来就是 H.264，不会重转）。
    if key == "source" and not download:
        preview = DATA_DIR / "batch-source-previews" / f"{batch_id}-{item_id}.mp4"
        path = await ensure_web_playable_at(path, preview)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # inline：让浏览器直接播放/预览，而不是触发下载。**必须 no-store**：换了源视频之后
    # 浏览器若还在放缓存里的旧视频，用户会以为没换成功（2026-09-15 用户实测反馈）。
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name if download else None,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/batches/{batch_id}/items/{item_id}/material/{key}")
async def batch_item_material(batch_id: str, item_id: str, key: str, download: bool = Query(False)):
    """出图素材：`scene` = 图一（源视频取帧，造型/场景参考）、`identity` = 图二（原型身份图）。"""
    item = _batch_item_or_404(batch_id, item_id)
    ai = item.get("ai") or {}
    if key == "scene":
        raw = str(ai.get("sceneFramePath") or "")
        name = "图一_源视频参考帧.jpg"
    elif key == "identity":
        raw = str(IDENTITY_PATH)
        name = "图二_原型身份图.png"
    else:
        raise HTTPException(404, "素材不存在")
    path = Path(raw)
    if not raw or not path.is_file():
        raise HTTPException(404, "素材尚未生成")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=name if download else None)


@app.get("/api/batches/{batch_id}/items/{item_id}/output/{key}")
async def batch_item_output(batch_id: str, item_id: str, key: str, download: bool = Query(False)):
    """发布目录里的成品：最终成片 / 人物图 / 发布文案。

    `videoNoLyrics` / `videoWithLyrics` 是 2026-09-14 之前的旧键，历史条目仍能点开。
    """
    item = _batch_item_or_404(batch_id, item_id)
    if key not in {"videoFinal", "videoNoLyrics", "videoWithLyrics", "image", "copy"}:
        raise HTTPException(404, "输出文件不存在")
    raw = (item.get("outputs") or {}).get(key)
    if not raw:
        raise HTTPException(404, "输出文件尚未生成")
    path = Path(raw).resolve()
    try:
        path.relative_to(BATCH_OUTPUT_ROOT.resolve())
    except ValueError:
        raise HTTPException(404, "输出文件不存在") from None
    if not path.is_file():
        raise HTTPException(404, "输出文件不存在")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name if download else None)


@app.post("/api/batches/{batch_id}/items/{item_id}/deliver")
async def redeliver_batch_item(batch_id: str, item_id: str):
    """重新整理发布文件：给「成片已经生成、但发布目录里没有」的条目补一次交付。

    用户 2026-09-14：「最终成片没有在指定目录中出现」——旧版本在跳过/取消时会把已经
    跑完的成片丢掉，这个接口让历史条目不用重跑整条链路就能把成片与人物图补回目录。
    """
    _batch_item_or_404(batch_id, item_id)
    try:
        return await deliver_item_now(batch_id, item_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/batches/{batch_id}/items/{item_id}/open-output")
async def open_batch_item_output(batch_id: str, item_id: str):
    item = _batch_item_or_404(batch_id, item_id)
    raw = (item.get("outputs") or {}).get("folder")
    if not raw:
        raise HTTPException(404, "发布文件夹尚未生成")
    folder = Path(raw).resolve()
    try:
        folder.relative_to(BATCH_OUTPUT_ROOT.resolve())
    except ValueError:
        raise HTTPException(404, "发布文件夹不存在") from None
    if not folder.is_dir():
        raise HTTPException(404, "发布文件夹不存在")
    await asyncio.to_thread(os.startfile, folder)
    return _batch_or_404(batch_id)


@app.websocket("/api/batches/{batch_id}/ws")
async def batch_websocket(websocket: WebSocket, batch_id: str):
    state = batch_store.get(batch_id)
    if not state:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    await websocket.send_json(state)
    queue = batch_store.subscribe(batch_id)
    try:
        while True:
            await websocket.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        batch_store.unsubscribe(batch_id, queue)


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    health = await comfy_health()
    defaults = workflow_defaults()
    missing = [name for name, path in required_paths().items() if not path.exists()]
    version = None
    if health:
        version = (health.get("system") or {}).get("comfyui_version")
    return {
        "comfyuiConnected": bool(health),
        "comfyuiVersion": version,
        "fixedReferenceUrl": "/assets/fixed-reference.png",
        "defaultAction": defaults[0],
        "defaultCamera": defaults[1],
        "maxDurationSeconds": int(MAX_DURATION_SECONDS),
        "environmentReady": not missing,
        "missingRequirements": missing,
        "resourceMode": "strict-single-chain",
    }


@app.get("/api/system/stats")
async def system_stats() -> dict[str, Any]:
    """CPU / 内存 / 磁盘 / 以太网吞吐 / GPU（含显存与温度）采样。

    采样依赖 nvidia-smi / psutil，任何一项失败都不该让页面吃到 500（前端拿到非 JSON 就只会
    报「Unexpected token」）。失败时退回可用部分 + `degraded` 说明。
    """
    from .system_stats import collect_system_stats

    try:
        return await asyncio.to_thread(collect_system_stats)
    except Exception as error:  # noqa: BLE001
        logger.warning("系统采样失败：%s", error, exc_info=True)
        return {
            "degraded": f"{type(error).__name__}: {error}"[:200],
            "cpu": None,
            "memory": None,
            "disks": [],
            "net": None,
            "gpu": [],
        }


@app.get("/api/jobs/latest")
async def latest_job(kind: str | None = Query(None)):
    state = store.latest(kind)
    if not state:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return JSONResponse(state, headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/recent")
async def recent_jobs():
    """最近任务及可用成片，供「二采放大」「生成字幕」选择输入。必须在 {job_id} 路由前注册。"""
    jobs: list[dict[str, Any]] = []
    for state in store.recent(8):
        title = state.get("sourceName") or state["id"]
        kind = state.get("kind") or "singing"
        jobs.append({
            "id": state["id"],
            "kind": kind,
            "status": state["status"],
            "title": title,
            # 生成字幕页据此预填歌名（歌词任务的 songName 就是用户当时搜的那首）
            "songName": state.get("songName") or "",
            "createdAt": state.get("createdAt"),
            "media": _job_media_entries(state),
        })
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    return state


@app.post("/api/jobs")
async def create_job(
    video: UploadFile = File(...),
    reference_image: UploadFile = File(...),
    action_prompt: str = Form(""),
    camera_prompt: str = Form(""),
    duration: float | None = Form(None),
    ratio: str = Form(DEFAULT_SINGING_CANVAS),
    use_rvc: str = Form("1"),
    use_upscale: str = Form("1"),
):
    active = store.active()
    if active:
        raise HTTPException(409, f"已有任务正在运行：{active['id'][:8]}")

    try:
        singing_canvas_params(ratio)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error

    # RVC 音色转换开关（默认开启）：关闭后跳过整个 RVC 流程，成片保留原声
    use_rvc_on = use_rvc in {"1", "true", "on", "yes"}
    # 二采放大开关（默认开启）：在关闭 ComfyUI 之前先做 4× 超分收 1080p，RVC 用高清成片
    use_upscale_on = use_upscale in {"1", "true", "on", "yes"}

    if duration and duration > MAX_DURATION_SECONDS + 0.25:
        raise HTTPException(400, f"视频不能超过 {int(MAX_DURATION_SECONDS)} 秒")

    extension = Path(video.filename or "input.mp4").suffix.lower()
    if extension not in {".mp4", ".mov", ".mkv", ".webm"}:
        raise HTTPException(400, "只支持 MP4、MOV、MKV 或 WebM 视频")

    image_extension = Path(reference_image.filename or "reference.png").suffix.lower()
    if image_extension not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, "人物图片只支持 PNG、JPG、JPEG 或 WebP")

    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    input_name = f"motionstudio_{job_id}_source{extension}"
    input_path = COMFY_INPUT / input_name
    reference_input_name = f"motionstudio_{job_id}_reference{image_extension}"
    reference_input_path = COMFY_INPUT / reference_input_name
    try:
        with input_path.open("wb") as destination:
            while chunk := await video.read(1024 * 1024):
                destination.write(chunk)
        with reference_input_path.open("wb") as destination:
            while chunk := await reference_image.read(1024 * 1024):
                destination.write(chunk)
    finally:
        await video.close()
        await reference_image.close()

    metadata = await media_metadata(input_path)
    actual_duration = metadata.get("duration")
    if actual_duration and actual_duration > MAX_DURATION_SECONDS + 0.25:
        input_path.unlink(missing_ok=True)
        reference_input_path.unlink(missing_ok=True)
        raise HTTPException(400, f"视频时长为 {actual_duration:.1f} 秒，不能超过 {int(MAX_DURATION_SECONDS)} 秒")

    created_at = now_iso()
    state = {
        "id": job_id,
        "kind": "singing",
        "canvas": ratio,
        "status": "queued",
        "stage": "upload",
        "createdAt": created_at,
        "updatedAt": created_at,
        "sourceName": video.filename or input_name,
        "sourceSize": input_path.stat().st_size,
        "sourceDuration": actual_duration,
        # H3 唱歌分段预估：每段约 15 秒、段间 22 帧续接（工作流懒加载只跑所需前 N 段）
        "estimatedSegments": estimate_singing_segments(actual_duration),
        "currentSegment": None,
        "sourceInputName": input_name,
        "sourcePath": str(input_path.resolve()),
        "referenceName": reference_image.filename or reference_input_name,
        "referenceSize": reference_input_path.stat().st_size,
        "referenceInputName": reference_input_name,
        "referencePath": str(reference_input_path.resolve()),
        "actionPrompt": action_prompt,
        "cameraPrompt": camera_prompt,
        "useRvc": use_rvc_on,
        "useUpscale": use_upscale_on,
        "currentNodeId": None,
        "currentNodeTitle": "等待启动 ComfyUI",
        "progress": 0,
        "progressValue": None,
        "progressMax": None,
        "milestones": initial_milestones(use_rvc_on, use_upscale_on),
        "logs": [{"time": created_at, "message": f"已接收人物图片与演唱视频：{reference_image.filename or reference_input_name} / {video.filename or input_name} · 画布 {ratio}" + ("" if use_rvc_on else " · RVC 音色转换已关闭，成片将保留原声") + ("" if use_upscale_on else " · 二采放大已关闭，成片保持原始分辨率")}],
        "errorSummary": None,
        "errorDetail": None,
        "originalReady": False,
        "enhancedReady": False,
        "finalReady": False,
        "originalOutput": None,
        "enhancedOutput": None,
        "finalOutput": None,
        "output": None,
        "promptIds": {},
    }
    store.create(state)
    spawn(run_pipeline(job_id))
    return state


@app.post("/api/jobs/migrate")
async def create_migrate_job(
    video: UploadFile = File(...),
    reference_image: UploadFile | None = File(None),
    ratio: str = Form("9:16"),
    remove_subtitles: str = Form("0"),
    mode: str = Form("animation"),
    content_prompt: str = Form(""),
    video_prompt: str = Form(""),
    image_prompt: str = Form(""),
    use_upscale: str = Form("1"),
):
    active = store.active()
    if active:
        raise HTTPException(409, f"已有任务正在运行：{active['id'][:8]}")

    try:
        canvas_params(ratio)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if mode not in {"animation", "replacement"}:
        raise HTTPException(400, "迁移模式只能是 animation（动作迁移）或 replacement（人物替换）")
    clean_on = remove_subtitles in {"1", "true", "on", "yes"}
    # 二采放大开关（默认开启）：迁移成片再做 4× 超分收 1080p 档，结果即最终成片
    upscale_on = use_upscale in {"1", "true", "on", "yes"}

    extension = Path(video.filename or "input.mp4").suffix.lower()
    if extension not in VIDEO_UPLOAD_SUFFIXES:
        raise HTTPException(400, "只支持 MP4、MOV、MKV 或 WebM 视频")

    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    input_name = f"motionstudio_{job_id}_source{extension}"
    input_path = COMFY_INPUT / input_name

    reference_name: str | None = None
    reference_path: Path | None = None
    if reference_image is not None and reference_image.filename:
        image_extension = Path(reference_image.filename).suffix.lower()
        if image_extension not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(400, "人物图片只支持 PNG、JPG、JPEG 或 WebP")
        reference_name = f"motionstudio_{job_id}_reference{image_extension}"
        reference_path = COMFY_INPUT / reference_name
    else:
        if not MIGRATE_REFERENCE.is_file():
            raise HTTPException(400, "未上传人物参考图，且内置默认人物图不存在，请上传人物图")
        reference_name = MIGRATE_REFERENCE.name
        reference_path = MIGRATE_REFERENCE

    try:
        with input_path.open("wb") as destination:
            while chunk := await video.read(1024 * 1024):
                destination.write(chunk)
        if reference_image is not None and reference_image.filename:
            reference_image.file.seek(0)
            with reference_path.open("wb") as destination:
                while chunk := await reference_image.read(1024 * 1024):
                    destination.write(chunk)
    finally:
        await video.close()
        if reference_image is not None:
            await reference_image.close()

    metadata = await media_metadata(input_path)
    created_at = now_iso()
    driver_input_name = input_name
    driver_path = input_path
    source_size = input_path.stat().st_size
    transcode_note = None
    if (metadata.get("fps") or 0) > 31:
        # 高帧率源（60fps 等）：抽帧到 30fps 作为驱动视频，段数/时长约减半；
        # 原始上传文件保留在 input 目录，出片与预览使用降帧驱动。
        driver_path = COMFY_INPUT / f"motionstudio_{job_id}_source30.mp4"
        try:
            await asyncio.to_thread(transcode_source_to_30fps, input_path, driver_path)
            metadata = await media_metadata(driver_path)
            driver_input_name = driver_path.name
            transcode_note = f"源视频 {metadata.get('fps')}fps（{metadata.get('frames')} 帧）→ 已自动转 30fps 驱动（{metadata.get('frames')} 帧），分段时长约减半"
        except Exception as error:  # 转码失败不阻塞：退回原始 60fps 文件
            transcode_note = f"30fps 自动转码失败（{error}），将使用原始视频"
            driver_path = input_path
            driver_input_name = input_name
    source_duration = metadata.get("duration")
    state = {
        "id": job_id,
        "kind": "migrate",
        "canvas": ratio,
        "migrateMode": mode,
        "removeSubtitles": clean_on,
        "useUpscale": upscale_on,
        "contentPrompt": content_prompt,
        "videoPrompt": video_prompt,
        "imagePrompt": image_prompt,
        "status": "queued",
        "stage": "upload",
        "createdAt": created_at,
        "updatedAt": created_at,
        "sourceName": video.filename or input_name,
        "sourceSize": source_size,
        "sourceDuration": source_duration,
        "sourceFps": metadata.get("fps"),
        # 长视频分段预估：每段 81 帧、重叠 5 帧 → 运行时可显示"第 X / N 段"
        "estimatedSegments": estimate_migrate_segments(metadata.get("frames")),
        "currentSegment": None,
        "sourceInputName": driver_input_name,
        "sourcePath": str(driver_path.resolve()),
        "referenceName": reference_image.filename if reference_image and reference_image.filename else reference_name,
        "referenceUploaded": bool(reference_image is not None and reference_image.filename),
        "referenceSize": reference_path.stat().st_size if reference_path.is_file() else None,
        "referenceInputName": reference_name,
        "referencePath": str(reference_path.resolve()),
        "currentNodeId": None,
        "currentNodeTitle": "等待启动 ComfyUI",
        "progress": 0,
        "progressValue": None,
        "progressMax": None,
        "milestones": migrate_milestones(clean_on, mode, ratio, upscale_on),
        "logs": [{
            "time": created_at,
            "message": (
                f"已接收动作视频{('与人物参考图' if reference_image and reference_image.filename else '（将使用内置默认人物图）')}："
                f"{video.filename or input_name} · 画布 {ratio}"
                + ("" if upscale_on else " · 二采放大已关闭，迁移成片即最终成片")
            ),
        }] + ([{
            "time": now_iso(),
            "message": transcode_note,
        }] if transcode_note else []),
        "errorSummary": None,
        "errorDetail": None,
        "originalReady": False,
        "cleanReady": False,
        "draftReady": False,
        "enhancedReady": False,
        "finalReady": False,
        "originalOutput": None,
        "cleanOutput": None,
        "draftOutput": None,
        "enhancedOutput": None,
        "finalOutput": None,
        "output": None,
        "promptIds": {},
    }
    store.create(state)
    spawn(run_migrate_pipeline(job_id))
    return state


@app.post("/api/jobs/upscale")
async def create_upscale_job(
    video: UploadFile | None = File(None),
    source_job_id: str = Form(""),
    source_key: str = Form("final"),
):
    """独立二采放大：上传视频或引用最近任务成片，固定 4× 放大后收 1080p 档。"""
    active = store.active()
    if active:
        raise HTTPException(409, f"已有任务正在运行：{active['id'][:8]}")
    if video is None and not source_job_id:
        raise HTTPException(400, "请上传视频或选择最近任务成片")

    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    target = COMFY_INPUT / f"motionstudio_{job_id}_upscale_src.mp4"

    source_name_text = ""
    if video is not None and video.filename:
        extension = Path(video.filename).suffix.lower()
        if extension not in VIDEO_UPLOAD_SUFFIXES:
            raise HTTPException(400, "只支持 MP4、MOV、MKV 或 WebM 视频")
        target = COMFY_INPUT / f"motionstudio_{job_id}_upscale_src{extension}"
        try:
            with target.open("wb") as destination:
                while chunk := await video.read(1024 * 1024):
                    destination.write(chunk)
        finally:
            await video.close()
        source_name_text = video.filename
    else:
        source_state = store.get(source_job_id)
        field = MEDIA_FIELD_BY_KEY.get(source_key)
        source_path = Path(source_state.get(field) or "") if source_state and field else Path()
        if not source_state or not source_path.is_file():
            raise HTTPException(400, "所选任务的成片不存在，请重新选择")
        target = COMFY_INPUT / f"motionstudio_{job_id}_upscale_src{source_path.suffix.lower() or '.mp4'}"
        await asyncio.to_thread(shutil.copy2, source_path, target)
        source_name_text = f"{source_state['id'][:8]} {source_key}"

    metadata = await media_metadata(target)
    width = int(metadata.get("width") or 1440)
    height = int(metadata.get("height") or 1080)
    scale = _upscale_target(width, height)
    # 二采放大固定 4×：不再提供 2× 选项，统一用 RealESRGAN_x4plus。
    multiplier = "4x"
    model = UPSCALE_MODEL_X4
    source_frames = int(metadata.get("frames") or 0) or None
    estimated_segments = (
        max(1, (source_frames + UPSCALE_BATCH_FRAMES - 1) // UPSCALE_BATCH_FRAMES)
        if source_frames
        else None
    )
    created_at = now_iso()
    state = {
        "id": job_id,
        "kind": "upscale",
        "multiplier": multiplier,
        "upscaleModel": model,
        "targetWidth": scale[0],
        "targetHeight": scale[1],
        "status": "queued",
        "stage": "upload",
        "createdAt": created_at,
        "updatedAt": created_at,
        "sourceName": source_name_text,
        "sourceSize": target.stat().st_size,
        "sourceDuration": metadata.get("duration"),
        "sourceFps": metadata.get("fps"),
        "estimatedSegments": estimated_segments,
        "currentSegment": None,
        "sourceInputName": target.name,
        "sourcePath": str(target.resolve()),
        "currentNodeId": None,
        "currentNodeTitle": "等待启动 ComfyUI",
        "progress": 0,
        "progressValue": None,
        "progressMax": None,
        "milestones": upscale_milestones(),
        "logs": [{
            "time": created_at,
            "message": (
                f"已接收放大源：{source_name_text} · {width}×{height} · 倍数 {multiplier}（固定） → 输出 {scale[0]}×{scale[1]}"
                + (f" · 预计 {estimated_segments} 段（每段 {UPSCALE_BATCH_FRAMES} 帧分批）" if estimated_segments else "")
            ),
        }],
        "errorSummary": None,
        "errorDetail": None,
        "originalReady": False,
        "cleanReady": False,
        "draftReady": False,
        "enhancedReady": False,
        "finalReady": False,
        "originalOutput": None,
        "cleanOutput": None,
        "draftOutput": None,
        "enhancedOutput": None,
        "finalOutput": None,
        "output": None,
        "promptIds": {},
    }
    store.create(state)
    spawn(run_upscale_job(job_id))
    return state


# ---------------------------------------------------------------------------
# 独立 RVC 音色转换：上传/引用成片 → 关闭 ComfyUI → Demucs 分离 → 音色转换 → 最终版
# ---------------------------------------------------------------------------


@app.post("/api/jobs/rvc")
async def create_rvc_job(
    video: UploadFile | None = File(None),
    source_job_id: str = Form(""),
    source_key: str = Form("original"),
):
    """独立 RVC 音色转换：上传视频或引用最近任务成片，转换为默认音色后输出最终版。"""
    active = store.active()
    if active:
        raise HTTPException(409, f"已有任务正在运行：{active['id'][:8]}")
    if video is None and not source_job_id:
        raise HTTPException(400, "请上传视频或选择最近任务成片")

    COMFY_INPUT.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    target = COMFY_INPUT / f"motionstudio_{job_id}_rvc_src.mp4"

    source_name_text = ""
    if video is not None and video.filename:
        extension = Path(video.filename).suffix.lower()
        if extension not in VIDEO_UPLOAD_SUFFIXES:
            raise HTTPException(400, "只支持 MP4、MOV、MKV 或 WebM 视频")
        target = COMFY_INPUT / f"motionstudio_{job_id}_rvc_src{extension}"
        try:
            with target.open("wb") as destination:
                while chunk := await video.read(1024 * 1024):
                    destination.write(chunk)
        finally:
            await video.close()
        source_name_text = video.filename
    else:
        source_state = store.get(source_job_id)
        field = MEDIA_FIELD_BY_KEY.get(source_key)
        source_path = Path(source_state.get(field) or "") if source_state and field else Path()
        if not source_state or not source_path.is_file():
            raise HTTPException(400, "所选任务的成片不存在，请重新选择")
        target = COMFY_INPUT / f"motionstudio_{job_id}_rvc_src{source_path.suffix.lower() or '.mp4'}"
        await asyncio.to_thread(shutil.copy2, source_path, target)
        source_name_text = f"{source_state['id'][:8]} {source_key}"

    metadata = await media_metadata(target)
    created_at = now_iso()
    state = {
        "id": job_id,
        "kind": "rvc",
        "voiceModel": RVC_MODEL.stem,
        "status": "queued",
        "stage": "upload",
        "createdAt": created_at,
        "updatedAt": created_at,
        "sourceName": source_name_text,
        "sourceSize": target.stat().st_size,
        "sourceDuration": metadata.get("duration"),
        "sourceFps": metadata.get("fps"),
        "sourceInputName": target.name,
        "sourcePath": str(target.resolve()),
        "currentNodeId": None,
        "currentNodeTitle": "等待启动音色转换",
        "progress": 0,
        "progressValue": None,
        "progressMax": None,
        "milestones": rvc_milestones(),
        "logs": [{
            "time": created_at,
            "message": (
                f"已接收待转换成片：{source_name_text} · {metadata.get('width')}×{metadata.get('height')}"
                f" · 目标音色 {RVC_MODEL.stem}（先关闭 ComfyUI 再执行 Demucs 分离与转换）"
            ),
        }],
        "errorSummary": None,
        "errorDetail": None,
        "originalReady": True,
        "cleanReady": False,
        "draftReady": False,
        "enhancedReady": False,
        "finalReady": False,
        "originalOutput": str(target.resolve()),
        "cleanOutput": None,
        "draftOutput": None,
        "enhancedOutput": None,
        "finalOutput": None,
        "output": None,
        "promptIds": {},
    }
    store.create(state)
    spawn(run_rvc_job(job_id))
    return state


# ---------------------------------------------------------------------------
# 生成字幕：剪映识别歌词时间轴 + 网易云官方歌词核对文字 + 剪映手书烧录
# （和批量唱歌条目出片后那条链路是同一条 `subtitle_step.burn_for_video`）
# ---------------------------------------------------------------------------
SUBTITLE_OUT_DIR = COMFY_OUTPUT / "video" / "H3_MotionStudio"


@app.post("/api/jobs/subtitle")
async def create_subtitle_job(
    video: UploadFile | None = File(None),
    source_job_id: str = Form(""),
    source_key: str = Form("final"),
    song_name: str = Form(""),
):
    """生成字幕任务：上传/选择成片 → 剪映「识别歌词」拿时间轴 → 官方歌词逐句核对文字

    （时间轴一字不动）→ ffmpeg + libass 按剪映手书观感烧录出字幕版成片。
    不经过 ComfyUI/RVC，但会独占一次剪映（打开草稿时动一次鼠标，跑完自动关闭）。

    **这条链路不用 ComfyUI，也不再等 `pipeline_lock`**（用户 2026-09-16：
    「不要锁这个啊 和生成字幕有什么关系 取消这个锁」）——只用剪映 + ffmpeg，提交即跑，
    不会被出片/迁移任务堵住。唯一保留的互斥是「同时只允许一条字幕任务」（下面的守卫）。
    """
    if video is None and not source_job_id:
        raise HTTPException(400, "请上传视频或选择最近任务成片")
    # 同一时间只留一条字幕任务（页面在跑时按钮本来就是禁用的，这里防接口直连）。
    # 单条与「一键批量」共用同一把互斥：剪映一次只能占一条。
    _busy_subtitle = _active_subtitle_job()
    if _busy_subtitle:
        raise HTTPException(
            409, f"已有一条「生成字幕」任务在跑或排队：{_busy_subtitle['id'][:8]}"
        )
    queue_note = None

    job_id = uuid.uuid4().hex
    job_dir = DATA_DIR / "subtitle" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    target = job_dir / "source.mp4"

    source_name_text = ""
    if video is not None and video.filename:
        extension = Path(video.filename).suffix.lower()
        if extension not in VIDEO_UPLOAD_SUFFIXES:
            raise HTTPException(400, "只支持 MP4、MOV、MKV 或 WebM 视频")
        target = job_dir / f"source{extension}"
        try:
            with target.open("wb") as destination:
                while chunk := await video.read(1024 * 1024):
                    destination.write(chunk)
        finally:
            await video.close()
        source_name_text = video.filename
    else:
        source_state = store.get(source_job_id)
        field = MEDIA_FIELD_BY_KEY.get(source_key)
        source_path = Path(source_state.get(field) or "") if source_state and field else Path()
        if not source_state or not source_path.is_file():
            raise HTTPException(400, "所选任务的成片不存在，请重新选择")
        target = job_dir / f"source{source_path.suffix.lower() or '.mp4'}"
        await asyncio.to_thread(shutil.copy2, source_path, target)
        source_name_text = f"{source_state['id'][:8]} {source_key}"

    metadata = await media_metadata(target)
    # 产物是「字幕版」独立文件，绝不动用户给的无字幕成片（无字幕版是以后重新烧字幕的唯一输入）
    out_path = SUBTITLE_OUT_DIR / f"{job_id}_字幕版.mp4"
    song = song_name.strip()
    created_at = now_iso()
    logs = [{
        "time": created_at,
        "message": (
            f"已接收字幕源：{source_name_text}"
            + (f" · {metadata.get('width')}×{metadata.get('height')}" if metadata.get("width") else "")
            + (f" · 《{song}》" if song else " · 没有歌名，用识别出的歌词反查官方歌词")
        ),
    }]
    if queue_note:
        logs.append({"time": created_at, "message": f"{queue_note}。"})
    state = {
        "id": job_id,
        "kind": "subtitle",
        # 提交即跑：不再依赖 pipeline_lock，也就没有 waiting 排队态
        "status": "queued",
        "stage": "upload",
        "createdAt": created_at,
        "updatedAt": created_at,
        "sourceName": source_name_text,
        "sourceSize": target.stat().st_size,
        "sourceDuration": metadata.get("duration"),
        "sourceFps": metadata.get("fps"),
        "sourcePath": str(target.resolve()),
        # 源是哪条任务（"从最近任务选"才有）：出片后据此把字幕版放进**那一条自己的发布目录**
        "sourceJobId": source_job_id or "",
        "sourceKey": source_key if source_job_id else "",
        "songName": song,
        "subtitleSource": str(target.resolve()),
        "subtitleDir": str(job_dir),
        "subtitleOutPath": str(out_path),
        "subtitleOutDir": str(SUBTITLE_OUT_DIR),
        "currentNodeId": None,
        "currentNodeTitle": "等待开始识别",
        # 还没开跑没有真实进度，绝不写 0（那会被读成「卡在 0%」）
        "progress": None,
        "progressValue": None,
        "progressMax": None,
        "milestones": subtitle_milestones(),
        "logs": logs,
        "errorSummary": None,
        "errorDetail": None,
        "originalReady": True,
        "originalOutput": str(target.resolve()),
        "finalReady": False,
        "finalOutput": None,
        "output": None,
        "promptIds": {},
    }
    store.create(state)
    spawn(run_subtitle_job(job_id))
    return state


@app.post("/api/jobs/subtitle/publish-batch")
async def create_subtitle_publish_batch():
    """一键给发布根里所有**缺字幕版**的唱歌视频生成字幕（逐条串行，一次占一个剪映）。

    扫描 `E:\\AI_Exports\\H3-MotionStudio\\发布成品` 下带「唱歌」标签、有无字幕成片
    `最终成片.mp4`、且还没有 `最终成片_字幕版.mp4` 的目录；把每条排进队列，后台逐条
    剪映识别时间轴 + 官方歌词核对 + 剪映手书烧录（与独立路由同一条 `burn_for_video`），
    产物直接落回各自的发布目录，与无字幕版并存。**已存在字幕版的不重复生成**。
    """
    # 与单条「生成字幕」共用同一把互斥：剪映一次只能占一条
    _busy_subtitle = _active_subtitle_job()
    if _busy_subtitle:
        raise HTTPException(
            409, f"已有一条「生成字幕」任务在跑或排队：{_busy_subtitle['id'][:8]}"
        )
    queue = scan_singing_publish_videos()
    job_id = uuid.uuid4().hex
    created_at = now_iso()
    logs = [{
        "time": created_at,
        "message": (
            f"一键批量加字幕：扫描到 {len(queue)} 条缺字幕版的唱歌视频。"
            if queue
            else "发布根里没有需要补字幕的唱歌视频（都已存在字幕版或没有无字幕成片）。"
        ),
    }]
    state = {
        "id": job_id,
        "kind": "subtitle_batch",
        "status": "queued",
        "stage": "subtitle",
        "createdAt": created_at,
        "updatedAt": created_at,
        "sourceName": f"发布成品批量（{len(queue)} 条）",
        "sourceSize": 0,
        "sourceDuration": None,
        "sourceFps": None,
        "sourcePath": "",
        "sourceJobId": "",
        "sourceKey": "",
        "songName": None,
        "subtitleQueue": queue,
        "subtitleIndex": 0,
        "subtitleDone": 0,
        "subtitleFailed": 0,
        "subtitleStarted": False,
        "pauseRequested": False,
        "subtitleSource": str(BATCH_OUTPUT_ROOT),
        "subtitleDir": "",
        "subtitleOutPath": "",
        "subtitleOutDir": str(BATCH_OUTPUT_ROOT),
        "currentNodeId": None,
        "currentNodeTitle": (
            f"共 {len(queue)} 条唱歌视频待加字幕" if queue else "没有待加字幕的视频"
        ),
        "progress": None,
        "progressValue": None,
        "progressMax": None,
        "milestones": BATCH_MILESTONES,
        "logs": logs,
        "errorSummary": None,
        "errorDetail": None,
        "originalReady": False,
        "originalOutput": None,
        "finalReady": False,
        "finalOutput": None,
        "output": None,
        "promptIds": {},
    }
    store.create(state)
    spawn(run_publish_subtitle_batch(job_id))
    return state


@app.post("/api/jobs/{job_id}/pause")
async def pause_subtitle_batch(job_id: str):
    """暂停一键批量（在**下一条开始之前**生效，当前这条跑完即停）。"""
    updated = request_publish_batch_pause(job_id)
    if updated is None:
        raise HTTPException(400, "只有「一键批量」任务支持暂停")
    return updated


@app.post("/api/jobs/{job_id}/resume")
async def resume_subtitle_batch(job_id: str):
    """继续一条已暂停的一键批量，从暂停处接着生成字幕。"""
    updated = resume_publish_batch(job_id)
    if updated is None:
        raise HTTPException(400, "只有「已暂停」的一键批量任务可以继续")
    return updated


@app.post("/api/jobs/{job_id}/open-publish")
async def open_subtitle_publish_folder(job_id: str):
    """打开字幕版所在的发布目录（与批量的 `open-output` 同一条规矩：必须在发布根内）。"""
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    raw = str(state.get("subtitlePublishPath") or "").strip()
    if not raw:
        raise HTTPException(404, "还没有放进发布目录")
    folder = Path(raw).resolve().parent
    try:
        folder.relative_to(BATCH_OUTPUT_ROOT.resolve())
    except ValueError:
        raise HTTPException(404, "发布目录不存在") from None
    if not folder.is_dir():
        raise HTTPException(404, "发布目录不存在")
    await asyncio.to_thread(os.startfile, folder)
    return store.get(job_id)


@app.post("/api/jobs/{job_id}/retry")
async def retry_subtitle_job(job_id: str):
    """重新开始一条「生成字幕」任务（用户 2026-09-16：「失败了 给我一个重新开始按钮」）。

    源成片已经在任务目录里（`data/subtitle/{job_id}/source*`），所以**不用重新上传**。
    正在跑（或卡在剪映调用里）的那一次先作废：打上 `restartRequested` 标记，并强杀剪映
    —— 卡住的 UIA/win32 调用会立刻返回，runner 收尾时看到标记就自己重排一轮
    （**不在端点里直接重置再 spawn**：旧任务的失败分支会把新任务刚写的状态覆盖掉）。
    """
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    if (state.get("kind") or "") != "subtitle":
        raise HTTPException(400, "只有「生成字幕」任务支持重新开始")
    source = Path(str(state.get("subtitleSource") or ""))
    if not source.is_file():
        raise HTTPException(400, "源成片已不在任务目录里，请重新选择视频再提交")

    if state.get("status") in ("waiting", "queued", "running", "cancelling"):
        request_restart(job_id)
        store.add_log(job_id, "收到重新开始请求：先中止当前这一次识别……")
        await asyncio.to_thread(kill_app)
        return store.get(job_id)

    reset_subtitle_job(job_id)
    spawn(run_subtitle_job(job_id))
    return store.get(job_id)


@app.post("/api/jobs/{job_id}/retry-voice")
async def retry_job_voice(job_id: str):
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    if store.active() and store.active()["id"] != job_id:
        raise HTTPException(409, "另一个任务正在运行")
    if not (state.get("enhancedReady") or state.get("originalReady")):
        raise HTTPException(400, "没有可用于音色转换的成片")
    spawn(retry_voice(job_id))
    return store.update(job_id, status="queued", stage="handoff")


@app.get("/api/comfy/queue")
async def comfy_queue():
    """当前 ComfyUI 队列 + 应用内活动任务摘要（用于任务队列面板）。"""
    health = await comfy_health()
    queue: dict[str, Any] = {"connected": bool(health), "running": [], "pending": []}
    if health:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{COMFY_URL}/queue")
                payload = response.json()
            for label in ("queue_running", "queue_pending"):
                key = "running" if label == "queue_running" else "pending"
                for item in payload.get(label) or []:
                    entry = {"promptId": str(item[1])}
                    try:
                        first = next(iter((item[2] or {}).values()), None)
                        entry["node"] = f"{first.get('class_type') or '?'}"
                    except (AttributeError, StopIteration):
                        entry["node"] = "?"
                    queue[key].append(entry)
        except (httpx.HTTPError, ValueError):
            queue["connected"] = False
    active = store.active()
    app_job = None
    if active:
        milestones = active.get("milestones") or []
        running_step = next((m for m in milestones if m.get("status") == "running"), None)
        app_job = {
            "id": active["id"],
            "kind": active.get("kind") or "singing",
            "status": active["status"],
            "stage": active.get("stage"),
            "canvas": active.get("canvas"),
            "migrateMode": active.get("migrateMode"),
            "sourceName": active.get("sourceName"),
            "progress": active.get("progress"),
            "currentNodeTitle": active.get("currentNodeTitle"),
            "startedAt": active.get("startedAt") or active.get("createdAt"),
            "runningMilestone": running_step.get("label") if running_step else None,
            "promptIds": list((active.get("promptIds") or {}).values()),
        }
    return {"connected": queue["connected"], "running": queue["running"], "pending": queue["pending"], "app": app_job}


class ComfyQueueAction(BaseModel):
    action: str


@app.post("/api/comfy/queue")
async def comfy_queue_action(request: ComfyQueueAction):
    if request.action != "clear-pending":
        raise HTTPException(400, "不支持的操作")
    if not await comfy_health():
        raise HTTPException(409, "ComfyUI 未运行")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{COMFY_URL}/queue", json={"clear": True})
    except httpx.HTTPError as error:
        raise HTTPException(502, f"清空队列失败：{error}") from error
    return {"cleared": True}


@app.post("/api/comfy/stop")
async def comfy_stop():
    """手动关闭 ComfyUI，立刻释放显存（用户 2026-09-10：「关闭启动的 comfyui」）。

    有任务在跑时拒绝执行——正在生成的图/视频会因为进程被杀而报废：
    - jobs 表里有 queued/running 的单链路任务；
    - 批次里有条目处于 running/revising/confirmed（可能正在出片或用本地出图）；
    - ComfyUI 自己的队列里还有 running/pending 的 prompt。
    """
    active = store.active()
    if active and active.get("status") in {"queued", "running"}:
        raise HTTPException(409, "有任务正在运行，请先取消或等它结束，再关闭 ComfyUI")
    batch = batch_store.active()
    if batch:
        busy = next(
            (
                item
                for item in batch.get("items") or []
                if item.get("status") in {"running", "revising", "confirmed"}
            ),
            None,
        )
        if busy:
            raise HTTPException(
                409, f"批量第 {busy.get('index')} 条正在执行，请先等它结束再关闭 ComfyUI"
            )
    if not await comfy_health():
        return {"stopped": False, "message": "ComfyUI 未运行"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            payload = (await client.get(f"{COMFY_URL}/queue")).json()
        queued = len(payload.get("queue_running") or []) + len(payload.get("queue_pending") or [])
    except (httpx.HTTPError, ValueError):
        queued = 0
    if queued:
        raise HTTPException(409, "ComfyUI 队列里还有任务，请先清空或等它跑完")
    try:
        stopped = await resources.shutdown_comfy()
    except PipelineError as error:
        raise HTTPException(502, f"{error.summary}：{error.detail}") from error
    return {
        "stopped": stopped,
        "message": "ComfyUI 已关闭，显存已释放。" if stopped else "ComfyUI 本来就是关闭状态。",
    }


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """取消当前任务：清掉排队中的本任务 prompt，中断正在运行的 prompt。

    中断后 pipeline 感知到 cancelled 状态即收尾（里程碑置为跳过），
    不会把任务改写成失败，也不会关闭 ComfyUI。
    """
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    if state.get("status") == "waiting":
        # 「生成字幕」在排队（还没拿到 pipeline_lock）：直接收尾即可，
        # **绝不能**走下面那条路 —— 它会把正在跑的出片任务的中断逻辑也一起触发。
        store.update(
            job_id,
            status="cancelled",
            cancelRequested=True,
            stage="cancelled",
            currentNodeId=None,
            currentNodeTitle=None,
            progress=None,
            errorSummary=None,
            errorDetail=None,
            finishedAt=now_iso(),
        )
        store.add_log(job_id, "已取消：还在排队、没有开始识别，也没有动正在跑的出片任务。")
        return store.get(job_id)
    if state.get("status") not in ("queued", "running", "cancelling"):
        raise HTTPException(409, "任务已结束，无法取消")
    active = store.active()
    if active and active["id"] != job_id:
        raise HTTPException(409, "另一个任务正在运行")

    store.update(
        job_id,
        status="cancelling",
        # `cancelRequested` 是**不可被覆盖的取消印记**：任务在等 `pipeline_lock` 时被取消，
        # 拿到锁那一刻的入口代码若不查状态就会把 `cancelled` 覆盖回 `running` 继续跑
        # （2026-09-17 实测事故），有印记才拦得住。见 `pipeline.is_job_cancelled`。
        cancelRequested=True,
        errorSummary=None,
        errorDetail=None,
        currentNodeTitle="正在取消任务…",
    )
    store.add_log(job_id, "收到取消请求，正在中断 ComfyUI……")

    prompt_ids = {str(value) for value in (state.get("promptIds") or {}).values()}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if await comfy_health():
                queue_response = await client.get(f"{COMFY_URL}/queue")
                payload = queue_response.json()
                running_ids = [str(item[1]) for item in payload.get("queue_running") or []]
                pending_ids = [str(item[1]) for item in payload.get("queue_pending") or []]
                # 队列里属于本任务的排队 prompt 无法单独移除，全部清空等待队列
                if pending_ids and set(pending_ids) <= prompt_ids and pending_ids:
                    await client.post(f"{COMFY_URL}/queue", json={"clear": True})
                    store.add_log(job_id, "已清空排队中的本任务片段。")
                elif pending_ids:
                    store.add_log(job_id, "等待队列包含其它任务，未自动清空（可手动处理）。")
                if any(pid in prompt_ids for pid in running_ids):
                    await client.post(f"{COMFY_URL}/interrupt", json={})
                    store.add_log(job_id, "已向 ComfyUI 发送中断指令。")
                elif running_ids:
                    store.add_log(job_id, "ComfyUI 正在运行其它队列任务，未中断它。")
    except httpx.HTTPError:
        pass

    # 状态置为 cancelled 后，pipeline 会在下一个检查点感知并收尾（里程碑跳过），
    # 不会改写为失败、不会关闭 ComfyUI。
    store.update(job_id, status="cancelled", cancelRequested=True, finishedAt=now_iso())
    return store.get(job_id)


@app.websocket("/api/jobs/{job_id}/ws")
async def job_websocket(websocket: WebSocket, job_id: str):
    state = store.get(job_id)
    if not state:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    await websocket.send_json(state)
    queue = store.subscribe(job_id)
    try:
        while True:
            next_state = await queue.get()
            await websocket.send_json(next_state)
            if next_state["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
    except WebSocketDisconnect:
        pass
    finally:
        store.unsubscribe(job_id, queue)


@app.get("/api/jobs/{job_id}/media/{kind}")
async def job_media(job_id: str, kind: str, download: bool = Query(False)):
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    key_map = {
        "original": "originalOutput",
        "clean": "cleanOutput",
        "draft": "draftOutput",
        "enhanced": "enhancedOutput",
        "final": "finalOutput",
    }
    key = key_map.get(kind)
    if not key or not state.get(key):
        raise HTTPException(404, "对应成片尚未生成")
    path = Path(state[key]).resolve()
    if not path.is_file():
        raise HTTPException(404, "成片文件不存在")
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    filename = path.name if download else None
    return FileResponse(path, media_type=media_type, filename=filename)


def _job_input_path(state: dict[str, Any], kind: str) -> Path:
    field = {"video": "sourcePath", "reference": "referencePath"}.get(kind)
    if not field:
        raise HTTPException(404, "输入文件不存在")
    raw_path = state.get(field)
    if not raw_path:
        raise HTTPException(404, "输入文件不存在")
    try:
        path = Path(raw_path).resolve()
        path.relative_to(COMFY_INPUT.resolve())
    except (OSError, ValueError):
        raise HTTPException(404, "输入文件不存在") from None
    if not path.is_file():
        raise HTTPException(404, "输入文件不存在")
    return path


@app.get("/api/jobs/{job_id}/input/video/preview")
async def job_input_video_preview(job_id: str):
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    source = _job_input_path(state, "video")
    target = DATA_DIR / "job-input-previews" / f"{job_id}.mp4"
    path = await ensure_web_playable_at(source, target)
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return FileResponse(path, media_type=media_type)


@app.get("/api/jobs/{job_id}/input/{kind}")
async def job_input(job_id: str, kind: str):
    """Serve persisted source files so a reopened workspace can recover its draft."""
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    path = _job_input_path(state, kind)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


async def _resolve_job(job_id: str) -> dict[str, Any] | None:
    """Live job from the downloader service, falling back to the mirror.

    Read paths never start the (memory-heavy) service: when it is offline or
    does not know the job anymore, the persisted mirror answers instead.
    """
    try:
        job = await douyin_service.job(job_id)
    except (DouyinServiceOffline, DouyinServiceError):
        job = None
    if job:
        mirror_upsert([job])
        return job
    return mirror_get_job(job_id)


@app.get("/api/douyin/status")
async def douyin_status():
    return await douyin_service.status()


@app.post("/api/douyin/download")
async def douyin_download(request: DouyinDownloadRequest):
    url = request.url.strip()
    if not url:
        raise HTTPException(400, "请输入抖音作品链接")
    if not is_douyin_url(url):
        raise HTTPException(400, "当前只支持抖音链接")
    try:
        job = await douyin_service.submit(url)
    except DouyinServiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    douyin_service.mark_activity()
    # Normalized payload: completed jobs (e.g. already downloaded) come back
    # with result and mediaUrl, and preview conversion starts immediately.
    return douyin_job_payload(job)


@app.get("/api/douyin/login/status")
async def douyin_login_status():
    try:
        payload = await douyin_service.auth_status()
    except DouyinServiceOffline:
        ready, count = _cookie_stats()
        return {
            "state": "idle",
            "message": "下载服务未运行：提交下载任务或打开登录窗口时才会启动",
            "error": None,
            "cookieReady": ready,
            "cookieCount": count,
            "missing": [],
        }
    except DouyinServiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    if isinstance(payload, dict) and payload.get("state") in ("opening", "waiting"):
        douyin_service.mark_activity()
    return payload


@app.post("/api/douyin/login/start")
async def douyin_login_start():
    try:
        payload = await douyin_service.start_login()
    except DouyinServiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    douyin_service.mark_activity()
    return payload


@app.post("/api/douyin/login/finish")
async def douyin_login_finish():
    try:
        payload = await douyin_service.finish_login()
    except DouyinServiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    douyin_service.mark_activity()
    return payload


@app.post("/api/douyin/login/cancel")
async def douyin_login_cancel():
    try:
        payload = await douyin_service.cancel_login()
    except DouyinServiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    douyin_service.mark_activity()
    return payload


def douyin_job_payload(job: dict[str, Any]) -> dict[str, Any]:
    result = douyin_service.result_for(job)
    job_id = str(job.get("job_id") or "")
    status = "completed" if job.get("status") == "success" else job.get("status")
    playable_ready = False
    if result:
        source = Path(result["path"])
        playable_ready = playable_download_path(source) is not None
        if not playable_ready:
            schedule_download_playable(source, str(result.get("awemeId") or ""))
    return {
        **job,
        "status": status,
        "result": (
            {
                **result,
                "playableReady": playable_ready,
                "mediaUrl": f"/api/douyin/jobs/{job_id}/media",
                "downloadUrl": f"/api/douyin/jobs/{job_id}/media?download=1",
            }
            if result
            else None
        ),
    }


def _settle_stale(job: dict[str, Any], live_ids: set[str] | None) -> dict[str, Any]:
    """Mark mirror-only pending/running jobs as failed.

    The downloader service only ever stops when idle, so a job that is still
    "active" in the mirror but no longer known to the service died with a
    restart (e.g. the H3 backend itself restarted and took the child process
    down). Turn it into a visible failure instead of letting the UI poll a
    ghost job forever.
    """
    if job.get("status") not in ("pending", "running"):
        return job
    if live_ids is not None and job.get("job_id") in live_ids:
        return job
    settled = dict(job)
    settled["status"] = "failed"
    settled["error"] = "下载进程已停止（本地服务重启以释放内存），请重新提交该链接"
    return settled


@app.get("/api/douyin/jobs")
async def douyin_jobs():
    live_ids: set[str] | None = None
    try:
        payload = await douyin_service.jobs()
    except DouyinServiceOffline:
        pass
    except DouyinServiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    else:
        jobs_raw = payload.get("jobs", []) if isinstance(payload, dict) else []
        mirror_upsert(jobs_raw)
        live_ids = {str(job.get("job_id")) for job in jobs_raw if isinstance(job, dict)}
    # Union with the mirror: after the service restarts (start/stop per use)
    # its memory is empty, but previously completed jobs must stay listed.
    settled = [_settle_stale(job, live_ids) for job in mirror_jobs()]
    return {"jobs": [douyin_job_payload(job) for job in settled]}


@app.get("/api/douyin/jobs/{job_id}")
async def douyin_job(job_id: str):
    job = await _resolve_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return douyin_job_payload(_settle_stale(job, None))


@app.get("/api/douyin/jobs/{job_id}/media")
async def douyin_job_media(job_id: str, download: bool = Query(False)):
    job = await _resolve_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    result = douyin_service.result_for(job)
    if not result:
        raise HTTPException(404, "下载文件尚未生成")
    source = Path(result["path"])
    path = await ensure_download_playable(source, str(result["awemeId"]))
    if playable_download_path(path) is None:
        raise HTTPException(500, "视频兼容格式转换失败，请检查 ffmpeg")
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return FileResponse(path, media_type=media_type, filename=path.name if download else None)


VIDEO_UPLOAD_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}


@app.post("/api/uploads/preview")
async def create_upload_preview(video: UploadFile = File(...)):
    """Store the picked singing video and prepare a browser-playable copy.

    HEVC/H.265 files (typical Douyin downloads) are transcoded to H.264 so
    the upload-card preview can play; the actual pipeline still receives the
    original file untouched at submit time.
    """
    suffix = Path(video.filename or "").suffix.lower()
    if suffix not in VIDEO_UPLOAD_SUFFIXES:
        raise HTTPException(400, "请选择 MP4、MOV、MKV 或 WebM 视频文件。")
    upload_id = secrets.token_hex(6)
    try:
        video.file.seek(0)
        await asyncio.to_thread(input_preview.save_upload, upload_id, suffix, video.file)
    except OSError as exc:
        raise HTTPException(500, f"预览文件保存失败：{exc}") from exc
    await asyncio.to_thread(input_preview.prune_old_uploads)
    return await input_preview.start_preview(upload_id)


@app.get("/api/uploads/{upload_id}/status")
async def get_upload_preview_status(upload_id: str):
    if not input_preview.valid_upload_id(upload_id):
        raise HTTPException(404, "预览不存在")
    payload = await input_preview.preview_status(upload_id)
    if not payload:
        raise HTTPException(404, "预览不存在")
    return payload


@app.get("/api/uploads/{upload_id}/preview")
async def get_upload_preview_media(upload_id: str):
    if not input_preview.valid_upload_id(upload_id):
        raise HTTPException(404, "预览不存在")
    path = await input_preview.resolve_preview(upload_id)
    if not path:
        raise HTTPException(404, "预览不存在")
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return FileResponse(path, media_type=media_type)


@app.get("/api/image-workbench/items")
async def image_workbench_items():
    return {"items": image_workbench.list_items()}


@app.get("/api/image-workbench/{folder_id}/asset/{key}")
async def image_workbench_asset(folder_id: str, key: str):
    try:
        path = image_workbench.asset_path(image_workbench.resolve_folder(folder_id), key)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "image/png")


@app.get("/api/image-workbench/{folder_id}/prompt/{stage}")
async def image_workbench_prompt(folder_id: str, stage: str, kind: str = Query("singing")):
    try:
        folder = image_workbench.resolve_folder(folder_id)
        return {"prompt": image_workbench.prompt(folder, stage, kind)}
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/image-workbench/{folder_id}/materials/{stage}")
async def image_workbench_materials(folder_id: str, stage: str, kind: str = Query("singing")):
    try:
        folder = image_workbench.resolve_folder(folder_id)
        payload = image_workbench.material_zip(folder, stage, kind)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    filename = f"{folder.name}_{stage}_ChatGPT素材.zip"
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.post("/api/image-workbench/{folder_id}/upload/{stage}")
async def image_workbench_upload(folder_id: str, stage: str, kind: str = Query("singing"), file: UploadFile = File(...)):
    payload = await file.read()
    if len(payload) > 30 * 1024 * 1024:
        raise HTTPException(413, "图片不能超过 30MB")
    try:
        saved = image_workbench.save_upload(image_workbench.resolve_folder(folder_id), stage, payload, kind)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "saved": saved}


@app.post("/api/image-workbench/{folder_id}/watch/{stage}")
async def image_workbench_watch(folder_id: str, stage: str, kind: str = Query("singing")):
    try:
        folder = image_workbench.resolve_folder(folder_id)
        if stage not in {"person", "bilibili", "douyin"}:
            raise ValueError("图片类型无效")
        watch_id = await image_workbench.watch_downloads(folder, stage, kind)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"watchId": watch_id}


@app.get("/api/image-workbench/watch/{watch_id}")
async def image_workbench_watch_status(watch_id: str):
    state = image_workbench.WATCHES.get(watch_id)
    if not state:
        raise HTTPException(404, "监听任务不存在")
    return state


@app.post("/api/image-workbench/{folder_id}/automatic")
async def image_workbench_automatic(
    folder_id: str,
    force: bool = Query(False),
    stage: str | None = Query(None, description="只跑这一步：person / bilibili / douyin；留空=整条三步"),
):
    """自动出图。用户 2026-09-19：「也可以让我选择每一张的生成」——
    带 `stage` 就只生成那一张（人物图 / B站 / 抖音各自可单独点），不带就三步一次跑完。"""
    try:
        image_workbench.resolve_folder(folder_id)
        return image_workbench.start_automatic(folder_id, force=force, stage=stage)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/image-workbench/automatic-all")
async def image_workbench_automatic_all():
    if not image_workbench.batch_image.configured():
        raise HTTPException(400, "第三方图片接口尚未配置")
    return {"queued": image_workbench.start_all_missing()}


@app.post("/api/image-workbench/automatic-stop")
async def image_workbench_automatic_stop():
    """停止自动出图（用户 2026-09-19：「启动后如果我要暂停怎么办」）。

    软停止：正在跑的那一步跑完就停（中转站的 HTTP 请求没法中断），排队的条目立刻取消。
    """
    return {"cancelled": image_workbench.stop_automatic()}


dist_dir = PROJECT_ROOT / "dist" / "client"
if dist_dir.is_dir():
    def _spa_index() -> FileResponse:
        """SPA 入口必须每次回源：index.html 里引用的是带 hash 的 bundle，
        一旦被浏览器缓存住，刷新页面也只会继续跑旧前端（2026-09-14 实测：
        改了三次前端，用户页面还停在最早的 bundle，新按钮全看不到）。"""
        return FileResponse(dist_dir / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/migrate", include_in_schema=False)
    async def migrate_frontend():
        return _spa_index()

    @app.get("/batch", include_in_schema=False)
    async def batch_frontend():
        return _spa_index()

    @app.get("/singing", include_in_schema=False)
    async def singing_frontend():
        return _spa_index()

    @app.get("/upscale", include_in_schema=False)
    async def upscale_frontend():
        return _spa_index()

    @app.get("/rvc", include_in_schema=False)
    async def rvc_frontend():
        return _spa_index()

    @app.get("/subtitle", include_in_schema=False)
    async def subtitle_frontend():
        return _spa_index()

    @app.get("/images", include_in_schema=False)
    async def images_frontend():
        return _spa_index()

    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="frontend")
