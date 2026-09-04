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
    CLEAN_WORKFLOW,
    COMFY_HOME,
    COMFY_INPUT,
    COMFY_LOG,
    COMFY_MAIN,
    COMFY_OUTPUT,
    COMFY_PYTHON,
    COMFY_URL,
    COMFY_WS,
    DEFAULT_CANVAS,
    MIGRATE_WORKFLOW,
    MIGRATE_REFERENCE,
    RVC_INDEX,
    RVC_MODEL,
    RVC_PYTHON,
    RVC_ROOT,
    RVC_SCRIPT,
    SINGING_WORKFLOW,
    UPSCALE_WORKFLOW,
    canvas_params,
)
from .store import now_iso, store
from .workflows import (
    graph_to_api_prompt,
    prepare_clean_workflow,
    prepare_migrate_workflow,
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


# 动作迁移链路两个工作流的里程碑顺序（与 store.migrate_milestones 的 id 对应）。
CLEAN_PLAN = ("read", "mask", "paint", "clean_save")
MIGRATE_PLAN = ("prep", "sam", "migrate", "save")
_PLAN_BY_KIND = {"clean": CLEAN_PLAN, "migrate": MIGRATE_PLAN}

_CLEAN_STAGE_OF = {"1": "read", "2": "mask", "3": "paint", "5": "clean_save"}

# 迁移工作流按节点 id 划分显示阶段（只在阶段推进时切换里程碑，回退/重复都忽略）：
# - prep：模型/参考加载与常量、循环准备（一次）；
# - sam：SAM3 人物追踪与遮罩（每次进循环的读取与追踪都算此阶段）；
# - migrate：每段采样/解码/收集等主体生成；
# - save：最终 VHS_VideoCombine 合成；
# - 未列入的轻量节点（分段边界计算、缓存门等）保持当前阶段不变。
_MIGRATE_PREP_NODES = frozenset({
    30, 322, 323, 327, 328, 329, 330, 331, 332, 335, 342, 343, 344, 345,
    348, 349, 353, 358, 359, 360, 362, 363, 457, 464, 469, 470, 474, 476,
    477, 479, 493, 495, 509, 510, 514, 515, 521, 533, 540, 541, 545, 561, 563,
})
_MIGRATE_SAM_NODES = frozenset({350, 351, 352, 446, 512, 513, 543})
_MIGRATE_GEN_NODES = frozenset({333, 356, 361, 450, 452, 453, 462, 465, 466})
_MIGRATE_SAVE_NODES = frozenset({456})


def clean_stage_of(node_id: str) -> str | None:
    return _CLEAN_STAGE_OF.get(node_id)


def migrate_stage_of(node_id: str) -> str | None:
    try:
        key = int(node_id)
    except (TypeError, ValueError):
        return None
    if key in _MIGRATE_PREP_NODES:
        return "prep"
    if key in _MIGRATE_SAM_NODES:
        return "sam"
    if key in _MIGRATE_GEN_NODES:
        return "migrate"
    if key in _MIGRATE_SAVE_NODES:
        return "save"
    return None


def stage_of(kind: str, node_id: str) -> str | None:
    if kind == "clean":
        return clean_stage_of(node_id)
    if kind == "migrate":
        return migrate_stage_of(node_id)
    return None


def completed_milestones_for(kind: str) -> tuple[str, ...]:
    if kind == "clean":
        return CLEAN_PLAN
    if kind == "migrate":
        return MIGRATE_PLAN
    return ("input", "h3", "stitch") if kind == "singing" else ("upscale", "hd")


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


async def _completed_history_media(prompt_id: str, preferred_node: str) -> Path | None:
    """Return a finished prompt's media, or None for an unfinished VHS meta-batch."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{COMFY_URL}/history/{prompt_id}")
        response.raise_for_status()
        history = response.json()
    record = history.get(prompt_id)
    if not record:
        return None
    status = record.get("status") or {}
    if status.get("status_str") == "error":
        raise PipelineError("ComfyUI 工作流执行失败", json.dumps(status, ensure_ascii=False, indent=2))
    try:
        return resolve_history_media(record, preferred_node)
    except PipelineError as error:
        if error.summary == "没有找到工作流输出视频":
            return None
        raise


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
    resolved_output: Path | None = None
    seen_prompt_ids: list[str] = []
    active_prompt_id = ""

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
            seen_prompt_ids.append(prompt_id)
            active_prompt_id = prompt_id

        store.add_log(job_id, f"工作流已进入 ComfyUI 队列：{prompt_id}")
        store.update(job_id, promptIds={**((store.get(job_id) or {}).get("promptIds") or {}), kind: prompt_id})
        active_milestone: str | None = None
        plan = _PLAN_BY_KIND.get(kind)
        plan_index = -1

        while True:
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=30)
            except asyncio.TimeoutError:
                if kind == "upscale":
                    for candidate_prompt_id in reversed(seen_prompt_ids):
                        resolved_output = await _completed_history_media(candidate_prompt_id, preferred_output_node)
                        if resolved_output is not None:
                            break
                    if resolved_output is not None:
                        break
                    continue
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
            message_prompt_id = str(data.get("prompt_id") or "")
            if kind != "upscale" and message_prompt_id not in ("", prompt_id):
                continue
            if kind == "upscale" and message_prompt_id:
                active_prompt_id = message_prompt_id
                if message_prompt_id not in seen_prompt_ids:
                    seen_prompt_ids.append(message_prompt_id)

            if event == "executing":
                node_id = data.get("node")
                if node_id is None:
                    if kind == "upscale":
                        resolved_output = await _completed_history_media(
                            active_prompt_id or prompt_id,
                            preferred_output_node,
                        )
                        if resolved_output is not None:
                            break
                        continue
                    break
                node_id = str(node_id)
                title = titles.get(node_id, node_id)
                if plan is not None:
                    # 里程碑只前进不倒退：长视频 Mie 循环会重复执行读取/SAM/采样节点，
                    # 重复与未列入节点只刷新当前标题，避免进度面板来回跳阶段。
                    target = stage_of(kind, node_id)
                    if target:
                        index = plan.index(target)
                        if index > plan_index:
                            plan_index = index
                            active_milestone = target
                            _set_running_milestone(job_id, target, node_id, title)
                        elif index == plan_index and active_milestone:
                            store.set_milestone(
                                job_id,
                                active_milestone,
                                currentNode=title,
                                currentNodeId=node_id,
                            )
                            store.update(job_id, currentNodeId=node_id, currentNodeTitle=title)
                else:
                    # A VHS meta-batch cycles through every upscale node many times.
                    # Keep the whole second pass running until the final combined
                    # video exists; node 8 finishing one chunk is not the final HD file.
                    milestone_id = singing_milestone(types.get(node_id, ""), title) if kind == "singing" else "upscale"
                    active_milestone = milestone_id
                    _set_running_milestone(job_id, milestone_id, node_id, title)
            elif event == "progress":
                value = int(data.get("value") or 0)
                maximum = max(1, int(data.get("max") or 1))
                percent = min(100.0, value / maximum * 100.0)
                node_id = str(data.get("node") or (store.get(job_id) or {}).get("currentNodeId") or "")
                title = titles.get(node_id, (store.get(job_id) or {}).get("currentNodeTitle") or "正在处理")
                if active_milestone:
                    milestone_id = active_milestone
                elif kind == "singing":
                    milestone_id = singing_milestone(types.get(node_id, ""), title)
                elif kind == "upscale":
                    milestone_id = upscale_milestone(node_id)
                else:
                    milestone_id = stage_of(kind, node_id) or (plan[0] if plan else "save")
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
                milestone_id = active_milestone or stage_of(kind, node_id)
                if not milestone_id:
                    milestone_id = "h3" if kind == "singing" else ("upscale" if kind == "upscale" else "migrate")
                store.set_milestone(job_id, milestone_id, status="error", currentNode=title)
                raise PipelineError(f"{title}：{summary}", detail)
            elif event in {"execution_success", "execution_complete"}:
                if kind == "upscale":
                    resolved_output = await _completed_history_media(
                        active_prompt_id or prompt_id,
                        preferred_output_node,
                    )
                    if resolved_output is not None:
                        break
                    continue
                break

    if resolved_output is None:
        resolved_output = await _completed_history_media(prompt_id, preferred_output_node)
    if resolved_output is None:
        raise PipelineError(
            "没有找到工作流输出视频",
            f"已检查的 Prompt ID：{', '.join(seen_prompt_ids)}",
        )

    for milestone_id in completed_milestones_for(kind):
        store.set_milestone(job_id, milestone_id, status="completed", progress=100, currentNode=None)
    return resolved_output


def link_into_input_as(source: Path, job_id: str, tag: str) -> Path:
    destination = COMFY_INPUT / f"motionstudio_{job_id}_{tag}{source.suffix.lower()}"
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def link_into_input(source: Path, job_id: str) -> Path:
    return link_into_input_as(source, job_id, "original")


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
    for path in (RVC_PYTHON, RVC_SCRIPT, RVC_MODEL):
        if not path.is_file():
            raise PipelineError("音色转换环境不完整", f"缺少：{path}")

    store.update(job_id, stage="voice", currentNodeTitle="启动便携音色转换器", progress=0)
    store.set_milestone(job_id, "stems", status="running", currentNode="提取成片音频", progress=0)
    store.add_log(job_id, "ComfyUI 已关闭，正在启动便携音色转换器。")

    command = [
        str(RVC_PYTHON),
        str(RVC_SCRIPT),
        str(enhanced_path),
        "--model",
        RVC_MODEL.name,
    ]
    # index 为可选：存在则显式传入，缺失时由转换脚本自动探测/以无索引模式运行
    if RVC_INDEX.is_file():
        command += ["--index", str(RVC_INDEX)]

    process = await asyncio.create_subprocess_exec(
        *command,
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
            store.set_milestone(job_id, "voice", status="running", currentNode=f"加载 {RVC_MODEL.stem} 音色模型", progress=15)
            store.update(job_id, currentNodeTitle=f"加载 {RVC_MODEL.stem} 音色模型", progress=15)
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


async def _run_enhance_and_voice(job_id: str, original: Path) -> None:
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
    store.update(
        job_id,
        status="completed",
        stage="completed",
        finalOutput=str(final),
        finalReady=True,
        currentNodeId=None,
        currentNodeTitle=None,
        progress=100,
        output=await media_metadata(final),
        finishedAt=now_iso(),
    )


async def run_pipeline(job_id: str) -> None:
    async with pipeline_lock:
        state = store.get(job_id)
        if not state:
            return
        store.update(
            job_id,
            status="running",
            stage="starting",
            errorSummary=None,
            errorDetail=None,
            startedAt=now_iso(),
            finishedAt=None,
        )
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

            await _run_enhance_and_voice(job_id, original)
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
            store.update(job_id, status="failed", stage="failed", errorSummary=summary, errorDetail=detail, finishedAt=now_iso())
            try:
                if await comfy_health():
                    await resources.stop_comfy(job_id)
            except Exception as stop_error:
                store.add_log(job_id, f"清理 ComfyUI 时发生错误：{stop_error}")


async def run_migrate_pipeline(job_id: str) -> None:
    """动作迁移任务：可选去字幕 → SCAIL 长视频动作迁移/人物替换 → 可选 1080P 高清。

    不涉及 RVC：输出保留原视频音频与帧率。与其它任务共用 pipeline_lock（单任务互斥）。
    """
    async with pipeline_lock:
        state = store.get(job_id)
        if not state:
            return
        store.update(
            job_id,
            status="running",
            stage="starting",
            errorSummary=None,
            errorDetail=None,
            startedAt=now_iso(),
            finishedAt=None,
        )
        try:
            await resources.ensure_comfy(job_id)
            state = store.get(job_id) or state
            params = canvas_params(state.get("canvas") or DEFAULT_CANVAS)
            mode = state.get("migrateMode") or "animation"
            hd1080 = bool(state.get("hd1080"))
            drive_name = state["sourceInputName"]

            if state.get("removeSubtitles"):
                store.update(job_id, stage="cleaning")
                store.add_log(job_id, "去字幕开关已开启：先运行 ProPainter 去字幕工作流。")
                clean = prepare_clean_workflow(
                    drive_name,
                    f"video/H3_MotionStudio/{job_id}_clean",
                    CLEAN_WORKFLOW,
                    canvas=params,
                )
                cleaned = await run_comfy_workflow(job_id, "clean", clean, "5")
                store.update(job_id, cleanOutput=str(cleaned), cleanReady=True)
                store.add_log(job_id, f"无字幕视频已保存：{cleaned.name}")
                drive_input = await asyncio.to_thread(link_into_input_as, cleaned, job_id, "clean")
                drive_name = drive_input.name
            else:
                store.add_log(job_id, "去字幕开关未开启：直接使用上传的原视频作为驱动视频。")

            reference_name = state.get("referenceInputName") or MIGRATE_REFERENCE.name
            if not (COMFY_INPUT / reference_name).is_file():
                raise PipelineError(
                    "人物参考图不存在",
                    f"预期文件：{COMFY_INPUT / reference_name}（未上传人物图时请检查默认人物图，或重新上传后提交）",
                )

            store.update(job_id, stage="migrating")
            migrate = prepare_migrate_workflow(
                drive_name,
                reference_name,
                mode,
                f"video/H3_MotionStudio/{job_id}_migrate",
                MIGRATE_WORKFLOW,
                canvas=params,
                content_prompt=state.get("contentPrompt"),
                video_prompt=state.get("videoPrompt"),
                image_prompt=state.get("imagePrompt"),
            )
            draft = await run_comfy_workflow(job_id, "migrate", migrate, "456")
            store.update(job_id, draftOutput=str(draft), draftReady=True)
            store.add_log(job_id, f"迁移成片已保存：{draft.name}")

            if hd1080:
                store.update(job_id, stage="enhancing")
                draft_input = await asyncio.to_thread(link_into_input_as, draft, job_id, "draft")
                upscale = prepare_upscale_workflow(
                    draft_input.name,
                    f"video/H3_MotionStudio/{job_id}_1080P",
                    UPSCALE_WORKFLOW,
                    scale=(params["hd_width"], params["hd_height"]),
                )
                final = await run_comfy_workflow(job_id, "upscale", upscale, "8")
                store.update(job_id, enhancedOutput=str(final), enhancedReady=True)
                store.add_log(job_id, f"1080P 高清成片已保存：{final.name}")
            else:
                final = draft

            store.update(
                job_id,
                status="completed",
                stage="completed",
                finalOutput=str(final),
                finalReady=True,
                currentNodeId=None,
                currentNodeTitle=None,
                progress=100,
                progressValue=None,
                progressMax=None,
                output=await media_metadata(final),
                finishedAt=now_iso(),
            )
            store.add_log(job_id, "动作迁移任务已完成。")
        except Exception as error:
            if isinstance(error, PipelineError):
                summary, detail = error.summary, error.detail
            else:
                summary, detail = "动作迁移执行失败", repr(error)
            store.add_log(job_id, f"错误：{summary}")
            failed_state = store.get(job_id) or {}
            running = next(
                (item["id"] for item in failed_state.get("milestones", []) if item.get("status") == "running"),
                None,
            )
            if running:
                store.set_milestone(job_id, running, status="error")
            store.update(job_id, status="failed", stage="failed", errorSummary=summary, errorDetail=detail, finishedAt=now_iso())
            try:
                if await comfy_health():
                    await resources.stop_comfy(job_id)
            except Exception as stop_error:
                store.add_log(job_id, f"清理 ComfyUI 时发生错误：{stop_error}")


async def retry_enhance(job_id: str) -> None:
    """Resume a failed job from its preserved original video."""
    async with pipeline_lock:
        state = store.get(job_id)
        if not state or not state.get("originalOutput"):
            raise PipelineError("没有可用于高清转换的原版成片")
        original = Path(state["originalOutput"])
        if not original.is_file():
            raise PipelineError("原版成片文件不存在", str(original))

        store.update(
            job_id,
            status="running",
            stage="starting",
            errorSummary=None,
            errorDetail=None,
            enhancedReady=False,
            finalReady=False,
            enhancedOutput=None,
            finalOutput=None,
            output=None,
            startedAt=now_iso(),
            finishedAt=None,
        )
        for milestone in ("upscale", "hd", "handoff", "stems", "voice", "mux"):
            store.set_milestone(job_id, milestone, status="pending", progress=None, currentNode=None)
        store.add_log(job_id, "从已保留的原版成片重新开始 1080P 高清转换。")

        try:
            await resources.ensure_comfy(job_id)
            await _run_enhance_and_voice(job_id, original)
        except Exception as error:
            if isinstance(error, PipelineError):
                summary, detail = error.summary, error.detail
            else:
                summary, detail = "高清转换重试失败", repr(error)
            store.add_log(job_id, f"错误：{summary}")
            failed_state = store.get(job_id) or {}
            running = next(
                (item["id"] for item in failed_state.get("milestones", []) if item.get("status") == "running"),
                None,
            )
            if running:
                store.set_milestone(job_id, running, status="error")
            store.update(job_id, status="failed", stage="failed", errorSummary=summary, errorDetail=detail, finishedAt=now_iso())
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
        store.update(job_id, status="running", stage="handoff", errorSummary=None, errorDetail=None, finalReady=False, startedAt=now_iso(), finishedAt=None)
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
                finishedAt=now_iso(),
            )
        except Exception as error:
            summary = error.summary if isinstance(error, PipelineError) else "音色转换失败"
            detail = error.detail if isinstance(error, PipelineError) else repr(error)
            store.update(job_id, status="failed", stage="failed", errorSummary=summary, errorDetail=detail, finishedAt=now_iso())
