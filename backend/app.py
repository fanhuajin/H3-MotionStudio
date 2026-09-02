from __future__ import annotations

import asyncio
import mimetypes
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .pipeline import PipelineError, comfy_health, media_metadata, retry_voice, run_pipeline
from .settings import (
    COMFY_INPUT,
    FIXED_REFERENCE,
    MAX_DURATION_SECONDS,
    PROJECT_ROOT,
    SINGING_WORKFLOW,
    required_paths,
)
from .store import initial_milestones, now_iso, store
from .workflows import load_workflow, node_by_id


running_tasks: set[asyncio.Task] = set()


def spawn(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    running_tasks.add(task)
    task.add_done_callback(running_tasks.discard)


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
        )
    yield


app = FastAPI(title="H3 MotionStudio", version="0.1.0", lifespan=lifespan)


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


@app.get("/api/jobs/latest")
async def latest_job():
    state = store.latest()
    return state if state else Response(status_code=204)


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
):
    active = store.active()
    if active:
        raise HTTPException(409, f"已有任务正在运行：{active['id'][:8]}")

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
        "logs": [{"time": created_at, "message": f"已接收人物图片与演唱视频：{reference_image.filename or reference_input_name} / {video.filename or input_name}"}],
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


@app.post("/api/jobs/{job_id}/retry-voice")
async def retry_job_voice(job_id: str):
    state = store.get(job_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    if store.active() and store.active()["id"] != job_id:
        raise HTTPException(409, "另一个任务正在运行")
    if not state.get("enhancedReady"):
        raise HTTPException(400, "没有可用于音色转换的高清成片")
    spawn(retry_voice(job_id))
    return store.update(job_id, status="queued", stage="handoff")


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
    key_map = {"original": "originalOutput", "enhanced": "enhancedOutput", "final": "finalOutput"}
    key = key_map.get(kind)
    if not key or not state.get(key):
        raise HTTPException(404, "对应成片尚未生成")
    path = Path(state[key]).resolve()
    if not path.is_file():
        raise HTTPException(404, "成片文件不存在")
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    filename = path.name if download else None
    return FileResponse(path, media_type=media_type, filename=filename)


dist_dir = PROJECT_ROOT / "dist" / "client"
if dist_dir.is_dir():
    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="frontend")
