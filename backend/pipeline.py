from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets

from .settings import (
    COMFY_HOME,
    COMFY_INPUT,
    COMFY_LOG,
    COMFY_MAIN,
    COMFY_OUTPUT,
    COMFY_PYTHON,
    COMFY_URL,
    COMFY_WS,
    RVC_INDEX,
    RVC_MODEL,
    RVC_PYTHON,
    RVC_ROOT,
    RVC_SCRIPT,
    SINGING_WORKFLOW,
    UPSCALE_WORKFLOW,
)
from .store import now_iso, store
from .workflows import (
    graph_to_api_prompt,
    prepare_singing_workflow,
    prepare_upscale_workflow,
    workflow_titles,
    workflow_types,
)


class PipelineError(RuntimeError):
    def __init__(self, summary: str, detail: str | None = None) -> None:
        super().__init__(summary)
        self.summary = summary
        self.detail = detail or summary


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


async def comfy_health() -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.get(f"{COMFY_URL}/system_stats")
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        return None


class ResourceManager:
    def __init__(self) -> None:
        self.comfy_process: subprocess.Popen | None = None
        self.comfy_log_handle = None

    async def ensure_comfy(self, job_id: str) -> None:
        if await comfy_health():
            store.add_log(job_id, "ComfyUI 已在运行，继续使用当前服务。")
            return

        if not COMFY_PYTHON.is_file() or not COMFY_MAIN.is_file():
            raise PipelineError("无法启动 ComfyUI", f"缺少运行文件：{COMFY_PYTHON} 或 {COMFY_MAIN}")

        store.add_log(job_id, "正在启动 ComfyUI，并等待模型服务就绪……")
        COMFY_LOG.parent.mkdir(parents=True, exist_ok=True)
        self.comfy_log_handle = COMFY_LOG.open("a", encoding="utf-8")
        self.comfy_process = subprocess.Popen(
            [
                str(COMFY_PYTHON),
                str(COMFY_MAIN),
                "--lowvram",
                "--listen",
                "127.0.0.1",
                "--port",
                "8188",
            ],
            cwd=str(COMFY_HOME),
            stdout=self.comfy_log_handle,
            stderr=subprocess.STDOUT,
            creationflags=_creation_flags(),
        )

        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if self.comfy_process.poll() is not None:
                raise PipelineError("ComfyUI 启动失败", f"进程退出码：{self.comfy_process.returncode}\n日志：{COMFY_LOG}")
            if await comfy_health():
                store.add_log(job_id, "ComfyUI 已启动。")
                return
            await asyncio.sleep(1.5)
        raise PipelineError("ComfyUI 启动超时", f"240 秒内没有响应。日志：{COMFY_LOG}")

    def _listener_process(self) -> dict[str, Any] | None:
        if sys.platform != "win32":
            return None
        command = (
            "$c=Get-NetTCPConnection -LocalPort 8188 -State Listen -ErrorAction SilentlyContinue | "
            "Select-Object -First 1; if($c){Get-CimInstance Win32_Process -Filter "
            "(\"ProcessId=\"+$c.OwningProcess) | Select-Object ProcessId,ExecutablePath,CommandLine | "
            "ConvertTo-Json -Compress}"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            creationflags=_creation_flags(),
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    async def stop_comfy(self, job_id: str) -> None:
        store.set_milestone(job_id, "handoff", status="running", currentNode="正在卸载模型并关闭 ComfyUI")
        store.update(job_id, stage="handoff", currentNodeTitle="关闭 ComfyUI", progress=None)
        store.add_log(job_id, "正在卸载 ComfyUI 模型并释放显存……")
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{COMFY_URL}/free", json={"unload_models": True, "free_memory": True})
        except httpx.HTTPError:
            pass

        stopped = False
        if self.comfy_process and self.comfy_process.poll() is None:
            self.comfy_process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.comfy_process.wait), timeout=15)
            except asyncio.TimeoutError:
                self.comfy_process.kill()
                await asyncio.to_thread(self.comfy_process.wait)
            stopped = True
        elif await comfy_health():
            process = await asyncio.to_thread(self._listener_process)
            if not process:
                raise PipelineError("无法安全关闭 ComfyUI", "8188 端口正在使用，但无法确认对应进程。")
            executable = str(process.get("ExecutablePath") or "")
            command_line = str(process.get("CommandLine") or "")
            expected_python = str(COMFY_PYTHON).lower()
            is_expected = executable.lower() == expected_python or (
                "comfyui\\main.py" in command_line.lower() and "d:\\comfyui" in command_line.lower()
            )
            if not is_expected:
                raise PipelineError(
                    "拒绝关闭未知进程",
                    f"8188 端口进程不是预期的 ComfyUI。PID={process.get('ProcessId')}\n{command_line}",
                )
            result = await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(process["ProcessId"]), "/T", "/F"],
                capture_output=True,
                text=True,
                creationflags=_creation_flags(),
            )
            if result.returncode != 0:
                raise PipelineError("关闭 ComfyUI 失败", result.stderr or result.stdout)
            stopped = True

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and await comfy_health():
            await asyncio.sleep(1)
        if await comfy_health():
            raise PipelineError("ComfyUI 没有完全关闭", "服务仍在占用 8188 端口，已阻止 RVC 启动。")

        if self.comfy_log_handle:
            self.comfy_log_handle.close()
            self.comfy_log_handle = None
        self.comfy_process = None
        store.set_milestone(job_id, "handoff", status="completed", currentNode=None, progress=100)
        store.add_log(job_id, "ComfyUI 已完全关闭，资源已切换到 RVC。" if stopped else "ComfyUI 已处于关闭状态。")


resources = ResourceManager()
_object_info_cache: dict[str, Any] | None = None
pipeline_lock = asyncio.Lock()


async def object_info() -> dict[str, Any]:
    global _object_info_cache
    if _object_info_cache is None:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(f"{COMFY_URL}/object_info")
            response.raise_for_status()
            _object_info_cache = response.json()
    return _object_info_cache


def singing_milestone(node_type: str, title: str) -> str:
    lowered = f"{node_type} {title}".lower()
    if any(token in lowered for token in ("loadimage", "loadaudio", "audiowindow", "audiolatent", "读取输入")):
        return "input"
    if any(token in lowered for token in ("join", "stitch", "trim", "lazy", "durationplan", "savevideo", "保存", "拼接", "裁")):
        return "stitch"
    return "h3"


def upscale_milestone(node_id: str) -> str:
    return "hd" if node_id == "8" else "upscale"


def _set_running_milestone(job_id: str, milestone_id: str, node_id: str, title: str) -> None:
    state = store.get(job_id) or {}
    for milestone in state.get("milestones", []):
        if milestone["status"] == "running" and milestone["id"] != milestone_id:
            store.set_milestone(job_id, milestone["id"], status="completed", progress=100, currentNode=None)
    store.set_milestone(
        job_id,
        milestone_id,
        status="running",
        currentNode=title,
        currentNodeId=node_id,
        progress=None,
        progressValue=None,
        progressMax=None,
    )
    store.update(job_id, currentNodeId=node_id, currentNodeTitle=title, progress=None, progressValue=None, progressMax=None)
    store.add_log(job_id, f"ComfyUI 节点开始：{title}（节点 {node_id}）")


def _candidate_media(value: Any, candidates: list[dict[str, str]]) -> None:
    if isinstance(value, dict):
        filename = value.get("filename") or value.get("name")
        fullpath = value.get("fullpath")
        if isinstance(fullpath, str) and Path(fullpath).suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
            candidates.append({"fullpath": fullpath, "type": str(value.get("type") or "output")})
        elif isinstance(filename, str) and Path(filename).suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
            candidates.append({
                "filename": filename,
                "subfolder": str(value.get("subfolder") or ""),
                "type": str(value.get("type") or "output"),
            })
        for child in value.values():
            _candidate_media(child, candidates)
    elif isinstance(value, list):
        for child in value:
            _candidate_media(child, candidates)
    elif isinstance(value, str) and Path(value).suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
        candidates.append({"fullpath": value, "type": "output"})


def resolve_history_media(record: dict[str, Any], preferred_node: str) -> Path:
    outputs = record.get("outputs") or {}
    search_values = []
    if preferred_node in outputs:
        search_values.append(outputs[preferred_node])
    search_values.extend(value for key, value in outputs.items() if key != preferred_node)
    candidates: list[dict[str, str]] = []
    for value in search_values:
        _candidate_media(value, candidates)
    for candidate in candidates:
        if candidate.get("fullpath"):
            path = Path(candidate["fullpath"])
        else:
            base = COMFY_OUTPUT if candidate.get("type") == "output" else COMFY_INPUT
            path = base / candidate.get("subfolder", "") / candidate["filename"]
        if path.is_file():
            return path.resolve()
    raise PipelineError("没有找到工作流输出视频", f"历史记录中的候选输出：{json.dumps(candidates, ensure_ascii=False)}")


async def run_comfy_workflow(
    job_id: str,
    kind: str,
    workflow: dict[str, Any],
    preferred_output_node: str,
) -> Path:
    info = await object_info()
    prompt = graph_to_api_prompt(workflow, info)
    titles = workflow_titles(workflow)
    types = workflow_types(workflow)
    client_id = f"motionstudio-{job_id}-{kind}-{uuid.uuid4().hex[:8]}"
    websocket_url = f"{COMFY_WS}/ws?clientId={client_id}"

    async with websockets.connect(websocket_url, max_size=64 * 1024 * 1024) as websocket:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{COMFY_URL}/prompt", json={"prompt": prompt, "client_id": client_id})
            if response.status_code >= 400:
                try:
                    body = response.json()
                    detail = json.dumps(body, ensure_ascii=False, indent=2)
                except ValueError:
                    detail = response.text
                raise PipelineError("ComfyUI 拒绝了工作流", detail)
            prompt_id = response.json()["prompt_id"]

        store.add_log(job_id, f"工作流已进入 ComfyUI 队列：{prompt_id}")
        store.update(job_id, promptIds={**((store.get(job_id) or {}).get("promptIds") or {}), kind: prompt_id})
        active_milestone: str | None = None

        while True:
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=30)
            except asyncio.TimeoutError:
                async with httpx.AsyncClient(timeout=15) as client:
                    history_response = await client.get(f"{COMFY_URL}/history/{prompt_id}")
                    history = history_response.json()
                    if prompt_id in history:
                        status = (history[prompt_id].get("status") or {}).get("status_str")
                        if status == "error":
                            raise PipelineError("ComfyUI 工作流执行失败", json.dumps(history[prompt_id].get("status"), ensure_ascii=False, indent=2))
                        break
                continue

            if isinstance(raw, bytes):
                continue
            message = json.loads(raw)
            event = message.get("type")
            data = message.get("data") or {}
            if data.get("prompt_id") not in (None, prompt_id):
                continue

            if event == "executing":
                node_id = data.get("node")
                if node_id is None:
                    break
                node_id = str(node_id)
                title = titles.get(node_id, node_id)
                milestone_id = singing_milestone(types.get(node_id, ""), title) if kind == "singing" else upscale_milestone(node_id)
                active_milestone = milestone_id
                _set_running_milestone(job_id, milestone_id, node_id, title)
            elif event == "progress":
                value = int(data.get("value") or 0)
                maximum = max(1, int(data.get("max") or 1))
                percent = min(100.0, value / maximum * 100.0)
                node_id = str(data.get("node") or (store.get(job_id) or {}).get("currentNodeId") or "")
                title = titles.get(node_id, (store.get(job_id) or {}).get("currentNodeTitle") or "正在处理")
                milestone_id = active_milestone or (singing_milestone(types.get(node_id, ""), title) if kind == "singing" else upscale_milestone(node_id))
                store.set_milestone(
                    job_id,
                    milestone_id,
                    status="running",
                    currentNode=title,
                    progress=percent,
                    progressValue=value,
                    progressMax=maximum,
                )
                store.update(job_id, progress=percent, progressValue=value, progressMax=maximum)
            elif event == "execution_error":
                node_id = str(data.get("node_id") or data.get("node") or "")
                title = titles.get(node_id, node_id or "未知节点")
                summary = str(data.get("exception_message") or "ComfyUI 节点执行失败")
                trace = data.get("traceback") or []
                detail = f"节点：{title}（{node_id}）\n{summary}\n" + "\n".join(trace)
                milestone_id = active_milestone or ("h3" if kind == "singing" else "upscale")
                store.set_milestone(job_id, milestone_id, status="error", currentNode=title)
                raise PipelineError(f"{title}：{summary}", detail)
            elif event in {"execution_success", "execution_complete"}:
                break

    if kind == "singing":
        for milestone_id in ("input", "h3", "stitch"):
            store.set_milestone(job_id, milestone_id, status="completed", progress=100, currentNode=None)
    else:
        for milestone_id in ("upscale", "hd"):
            store.set_milestone(job_id, milestone_id, status="completed", progress=100, currentNode=None)

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{COMFY_URL}/history/{prompt_id}")
        response.raise_for_status()
        history = response.json()
    if prompt_id not in history:
        raise PipelineError("ComfyUI 没有返回历史记录", f"Prompt ID: {prompt_id}")
    return resolve_history_media(history[prompt_id], preferred_output_node)


def link_into_input(source: Path, job_id: str) -> Path:
    destination = COMFY_INPUT / f"motionstudio_{job_id}_original{source.suffix.lower()}"
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


async def media_metadata(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration,size", "-show_entries", "stream=width,height",
        "-select_streams", "v:0", "-of", "json", str(path),
    ]
    result = await asyncio.to_thread(
        subprocess.run,
        command,
        capture_output=True,
        text=True,
        creationflags=_creation_flags(),
        timeout=30,
    )
    if result.returncode != 0:
        return {"width": 1440, "height": 1080, "duration": None, "size": path.stat().st_size, "completedAt": now_iso()}
    payload = json.loads(result.stdout)
    stream = (payload.get("streams") or [{}])[0]
    format_data = payload.get("format") or {}
    return {
        "width": int(stream.get("width") or 1440),
        "height": int(stream.get("height") or 1080),
        "duration": float(format_data["duration"]) if format_data.get("duration") else None,
        "size": int(format_data.get("size") or path.stat().st_size),
        "completedAt": now_iso(),
    }


async def run_rvc(job_id: str, enhanced_path: Path) -> Path:
    if await comfy_health():
        raise PipelineError("为了保护显存，RVC 没有启动", "检测到 ComfyUI 仍在运行。必须先完全关闭 ComfyUI。")
    for path in (RVC_PYTHON, RVC_SCRIPT, RVC_MODEL, RVC_INDEX):
        if not path.is_file():
            raise PipelineError("音色转换环境不完整", f"缺少：{path}")

    store.update(job_id, stage="voice", currentNodeTitle="启动便携音色转换器", progress=0)
    store.set_milestone(job_id, "stems", status="running", currentNode="提取成片音频", progress=0)
    store.add_log(job_id, "ComfyUI 已关闭，正在启动便携音色转换器。")

    process = await asyncio.create_subprocess_exec(
        str(RVC_PYTHON),
        str(RVC_SCRIPT),
        str(enhanced_path),
        "--model",
        RVC_MODEL.name,
        "--index",
        str(RVC_INDEX),
        cwd=str(RVC_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=_creation_flags(),
    )

    assert process.stdout is not None
    lines: list[str] = []
    while True:
        raw = await process.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        lines.append(line)
        store.add_log(job_id, f"RVC：{line}")
        if line.startswith("[1/5]"):
            store.set_milestone(job_id, "stems", status="running", currentNode="提取音频", progress=10)
        elif line.startswith("[2/5]"):
            store.set_milestone(job_id, "stems", status="running", currentNode="Demucs 分离人声与伴奏", progress=35)
        elif line.lstrip().startswith("vocal"):
            store.set_milestone(job_id, "stems", status="completed", currentNode=None, progress=100)
        elif line.startswith("[3/5]"):
            store.set_milestone(job_id, "stems", status="completed", currentNode=None, progress=100)
            store.set_milestone(job_id, "voice", status="running", currentNode="加载我的音色模型", progress=15)
            store.update(job_id, currentNodeTitle="加载我的音色模型", progress=15)
        elif line.startswith("[4/5]"):
            store.set_milestone(job_id, "voice", status="running", currentNode="保存转换后的人声", progress=90)
        elif line.startswith("[5/5]"):
            store.set_milestone(job_id, "voice", status="completed", currentNode=None, progress=100)
            store.set_milestone(job_id, "mux", status="running", currentNode="替换最终成片音频", progress=60)
            store.update(job_id, currentNodeTitle="替换最终成片音频", progress=60)

    return_code = await process.wait()
    if return_code != 0:
        detail = "\n".join(lines[-120:])
        state = store.get(job_id) or {}
        active = next((item["id"] for item in state.get("milestones", []) if item.get("status") == "running"), "voice")
        store.set_milestone(job_id, active, status="error")
        raise PipelineError("音色转换失败", detail)

    expected = enhanced_path.with_name(f"{enhanced_path.stem}_{RVC_MODEL.stem}.mp4")
    if not expected.is_file():
        raise PipelineError("音色转换完成但没有找到最终 MP4", f"预期文件：{expected}\n" + "\n".join(lines[-80:]))
    store.set_milestone(job_id, "voice", status="completed", progress=100, currentNode=None)
    store.set_milestone(job_id, "mux", status="completed", progress=100, currentNode=None)
    store.add_log(job_id, f"最终成片已生成：{expected.name}")
    return expected.resolve()


async def run_pipeline(job_id: str) -> None:
    async with pipeline_lock:
        state = store.get(job_id)
        if not state:
            return
        store.update(job_id, status="running", stage="starting", errorSummary=None, errorDetail=None)
        try:
            await resources.ensure_comfy(job_id)
            state = store.get(job_id) or state
            singing = prepare_singing_workflow(
                state["sourceInputName"],
                state["referenceInputName"],
                state.get("actionPrompt") or "",
                state.get("cameraPrompt") or "",
                f"video/H3_MotionStudio/{job_id}_原版",
                SINGING_WORKFLOW,
            )
            store.update(job_id, stage="singing")
            original = await run_comfy_workflow(job_id, "singing", singing, "59")
            store.update(job_id, originalOutput=str(original), originalReady=True)
            store.add_log(job_id, f"原版成片已保存：{original.name}")

            linked = await asyncio.to_thread(link_into_input, original, job_id)
            upscale = prepare_upscale_workflow(
                linked.name,
                f"video/H3_MotionStudio/{job_id}_1080P",
                UPSCALE_WORKFLOW,
            )
            store.update(job_id, stage="enhancing")
            enhanced = await run_comfy_workflow(job_id, "upscale", upscale, "8")
            store.update(job_id, enhancedOutput=str(enhanced), enhancedReady=True)
            store.add_log(job_id, f"高清加强成片已保存：{enhanced.name}")

            await resources.stop_comfy(job_id)
            final = await run_rvc(job_id, enhanced)
            metadata = await media_metadata(final)
            store.update(
                job_id,
                status="completed",
                stage="completed",
                finalOutput=str(final),
                finalReady=True,
                currentNodeId=None,
                currentNodeTitle=None,
                progress=100,
                output=metadata,
            )
        except Exception as error:
            if isinstance(error, PipelineError):
                summary, detail = error.summary, error.detail
            else:
                summary, detail = "任务执行失败", repr(error)
            store.add_log(job_id, f"错误：{summary}")
            failed_state = store.get(job_id) or {}
            running = next((item["id"] for item in failed_state.get("milestones", []) if item.get("status") == "running"), None)
            if running:
                store.set_milestone(job_id, running, status="error")
            store.update(job_id, status="failed", stage="failed", errorSummary=summary, errorDetail=detail)
            try:
                if await comfy_health():
                    await resources.stop_comfy(job_id)
            except Exception as stop_error:
                store.add_log(job_id, f"清理 ComfyUI 时发生错误：{stop_error}")


async def retry_voice(job_id: str) -> None:
    async with pipeline_lock:
        state = store.get(job_id)
        if not state or not state.get("enhancedOutput"):
            raise PipelineError("没有可用于音色转换的高清成片")
        enhanced = Path(state["enhancedOutput"])
        if not enhanced.is_file():
            raise PipelineError("高清成片文件不存在", str(enhanced))
        store.update(job_id, status="running", stage="handoff", errorSummary=None, errorDetail=None, finalReady=False)
        for milestone in ("handoff", "stems", "voice", "mux"):
            store.set_milestone(job_id, milestone, status="pending", progress=None, currentNode=None)
        if await comfy_health():
            await resources.stop_comfy(job_id)
        else:
            store.set_milestone(job_id, "handoff", status="completed", progress=100)
        try:
            final = await run_rvc(job_id, enhanced)
            store.update(
                job_id,
                status="completed",
                stage="completed",
                finalOutput=str(final),
                finalReady=True,
                output=await media_metadata(final),
                progress=100,
            )
        except Exception as error:
            summary = error.summary if isinstance(error, PipelineError) else "音色转换失败"
            detail = error.detail if isinstance(error, PipelineError) else repr(error)
            store.update(job_id, status="failed", stage="failed", errorSummary=summary, errorDetail=detail)
