from __future__ import annotations

import asyncio
import logging
import mimetypes
import secrets
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import httpx

logger = logging.getLogger("uvicorn.error")

from . import input_preview
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
from .pipeline import (
    PipelineError,
    comfy_health,
    estimate_migrate_segments,
    media_metadata,
    retry_enhance,
    retry_voice,
    run_migrate_pipeline,
    run_pipeline,
    run_upscale_job,
)
from .settings import (
    COMFY_INPUT,
    COMFY_URL,
    DATA_DIR,
    DEFAULT_SINGING_CANVAS,
    FIXED_REFERENCE,
    MAX_DURATION_SECONDS,
    MIGRATE_REFERENCE,
    PROJECT_ROOT,
    SINGING_WORKFLOW,
    UPSCALE_MODEL_X2,
    UPSCALE_MODEL_X4,
    canvas_params,
    required_paths,
    singing_canvas_params,
)
from .store import initial_milestones, migrate_milestones, now_iso, store, upscale_milestones
from .workflows import load_workflow, node_by_id


running_tasks: set[asyncio.Task] = set()


def spawn(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    running_tasks.add(task)
    task.add_done_callback(running_tasks.discard)


# 任务成片字段映射（media key → (state 字段, 就绪标记, 显示名)）
OUTPUT_MEDIA_FIELDS = (
    ("final", "finalOutput", "finalReady", "最终成片"),
    ("original", "originalOutput", "originalReady", "原版成片"),
    ("draft", "draftOutput", "draftReady", "迁移成片"),
    ("clean", "cleanOutput", "cleanReady", "去字幕视频"),
    ("enhanced", "enhancedOutput", "enhancedReady", "高清成片"),
)


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
    """按源宽高比就近选择 1080p 标准档（4:3/16:9 横屏，3:4/9:16 竖屏）。"""
    aspect = width / max(1, height)
    if aspect >= 1:
        candidates = ((4 / 3, 1440, 1080), (16 / 9, 1920, 1080))
    else:
        candidates = ((3 / 4, 1080, 1440), (9 / 16, 1080, 1920))
    return min(candidates, key=lambda item: abs(aspect - item[0]))[1:]


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
    active = store.active()
    if active:
        store.update(
            active["id"],
            status="interrupted",
            stage="failed",
            errorSummary="本地服务曾在任务运行时重启",
            errorDetail="任务状态已保留。请根据已生成的中间成片重新提交或重试音色转换。",
            finishedAt=now_iso(),
        )
    sweep_task = asyncio.create_task(_douyin_housekeeping())
    try:
        yield
    finally:
        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass
        await douyin_service.stop()


app = FastAPI(title="H3 MotionStudio", version="0.1.0", lifespan=lifespan)


class DouyinDownloadRequest(BaseModel):
    url: str


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
    """CPU / 内存 / 磁盘 / 以太网吞吐 / GPU（含显存与温度）采样。"""
    from .system_stats import collect_system_stats

    return await asyncio.to_thread(collect_system_stats)


@app.get("/api/jobs/latest")
async def latest_job(kind: str | None = Query(None)):
    state = store.latest(kind)
    if not state:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return JSONResponse(state, headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/recent")
async def recent_jobs():
    """最近任务及可用成片，供「二采放大」选择输入。必须在 {job_id} 路由前注册。"""
    jobs: list[dict[str, Any]] = []
    for state in store.recent(8):
        title = state.get("sourceName") or state["id"]
        kind = state.get("kind") or "singing"
        jobs.append({
            "id": state["id"],
            "kind": kind,
            "status": state["status"],
            "title": title,
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
):
    active = store.active()
    if active:
        raise HTTPException(409, f"已有任务正在运行：{active['id'][:8]}")

    try:
        singing_canvas_params(ratio)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error

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
        "sourceInputName": input_name,
        "sourcePath": str(input_path.resolve()),
        "referenceName": reference_image.filename or reference_input_name,
        "referenceSize": reference_input_path.stat().st_size,
        "referenceInputName": reference_input_name,
        "referencePath": str(reference_input_path.resolve()),
        "actionPrompt": action_prompt,
        "cameraPrompt": camera_prompt,
        "currentNodeId": None,
        "currentNodeTitle": "等待启动 ComfyUI",
        "progress": 0,
        "progressValue": None,
        "progressMax": None,
        "milestones": initial_milestones(),
        "logs": [{"time": created_at, "message": f"已接收人物图片与演唱视频：{reference_image.filename or reference_input_name} / {video.filename or input_name} · 画布 {ratio}"}],
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
        "milestones": migrate_milestones(clean_on, mode, ratio),
        "logs": [{
            "time": created_at,
            "message": (
                f"已接收动作视频{('与人物参考图' if reference_image and reference_image.filename else '（将使用内置默认人物图）')}："
                f"{video.filename or input_name} · 画布 {ratio}"
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
    multiplier: str = Form("4x"),
):
    """独立二采放大：上传视频或引用最近任务成片，按 2×/4× 放大后收 1080p 档。"""
    active = store.active()
    if active:
        raise HTTPException(409, f"已有任务正在运行：{active['id'][:8]}")
    if multiplier not in {"2x", "4x"}:
        raise HTTPException(400, "放大倍数只能是 2x 或 4x")
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
        field = dict((key, field) for key, field, _flag, _label in OUTPUT_MEDIA_FIELDS).get(source_key)
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
    model = UPSCALE_MODEL_X2 if multiplier == "2x" else UPSCALE_MODEL_X4
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
            "message": f"已接收放大源：{source_name_text} · {width}×{height} · 倍数 {multiplier} → 输出 {scale[0]}×{scale[1]}",
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


@app.post("/api/jobs/{job_id}/retry-enhance")
async def retry_job_enhance(job_id: str):
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    if store.active() and store.active()["id"] != job_id:
        raise HTTPException(409, "另一个任务正在运行")
    if not state.get("originalReady"):
        raise HTTPException(400, "没有可用于高清转换的原版成片")
    spawn(retry_enhance(job_id))
    return store.update(job_id, status="queued", stage="starting")


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


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """取消当前任务：清掉排队中的本任务 prompt，中断正在运行的 prompt。

    中断后 pipeline 感知到 cancelled 状态即收尾（里程碑置为跳过），
    不会把任务改写成失败，也不会关闭 ComfyUI。
    """
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    if state.get("status") not in ("queued", "running", "cancelling"):
        raise HTTPException(409, "任务已结束，无法取消")
    active = store.active()
    if active and active["id"] != job_id:
        raise HTTPException(409, "另一个任务正在运行")

    store.update(
        job_id,
        status="cancelling",
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
    store.update(job_id, status="cancelled", finishedAt=now_iso())
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


dist_dir = PROJECT_ROOT / "dist" / "client"
if dist_dir.is_dir():
    @app.get("/douyin", include_in_schema=False)
    async def douyin_frontend():
        return FileResponse(dist_dir / "index.html")

    @app.get("/migrate", include_in_schema=False)
    async def migrate_frontend():
        return FileResponse(dist_dir / "index.html")

    @app.get("/upscale", include_in_schema=False)
    async def upscale_frontend():
        return FileResponse(dist_dir / "index.html")

    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="frontend")
