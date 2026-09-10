"""批量预审的候选人物图：本地 ComfyUI Krea2「双图片编辑」。

工作流的输入约定（节点备注原文「图像-1上传场景，图像-2上传人物」）与桌面
`4比3图片.txt` / `9比16图片.txt` 的「图一造型场景 + 图二唯一身份」完全一致：
图像-1 = 源视频取帧（造型/服装/场景/灯光），图像-2 = 固定身份图（只负责脸）。

这里不做 jobs 表登记，因为批量条目不是标准任务；进度通过 `on_progress` 回调
把 KSampler 的真实步数交给调用方写入条目里程碑，不使用假进度。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import websockets

from .pipeline import object_info, pipeline_lock, resources
from .settings import (
    COMFY_INPUT,
    COMFY_URL,
    COMFY_WS,
    PORTRAIT_RATIO_PARAMS,
    PORTRAIT_WORKFLOW,
)
from .workflows import graph_to_api_prompt, load_workflow

SCENE_TITLE = "加载图像-1"
IDENTITY_TITLE = "加载图像-2"
PROMPT_TITLE = "提示词"
GENERATION_TIMEOUT = 1800.0

ProgressCallback = Callable[[float, str], Awaitable[None]]


def _node_by_title(workflow: dict[str, Any], title: str) -> dict[str, Any] | None:
    for node in workflow.get("nodes") or []:
        if (node.get("title") or "").strip() == title:
            return node
    return None


def _node_by_type(workflow: dict[str, Any], class_type: str) -> dict[str, Any] | None:
    for node in workflow.get("nodes") or []:
        if node.get("type") == class_type:
            return node
    return None


def prepare_portrait_workflow(
    workflow: dict[str, Any],
    *,
    scene_image: str,
    identity_image: str,
    prompt: str,
    ratio: str,
    prefix: str,
) -> dict[str, Any]:
    """把 ComfyUI input 内的图片名、提示词、画布档位和输出前缀写进 Krea2 图。"""
    if ratio not in PORTRAIT_RATIO_PARAMS:
        raise RuntimeError(f"候选人物图不支持的比例：{ratio}")
    params = PORTRAIT_RATIO_PARAMS[ratio]

    for title, value in (
        (SCENE_TITLE, scene_image),
        (IDENTITY_TITLE, identity_image),
        (PROMPT_TITLE, prompt),
    ):
        node = _node_by_title(workflow, title)
        if node is None or not isinstance(node.get("widgets_values"), list):
            raise RuntimeError(f"Krea2 双图编辑工作流缺少可用节点「{title}」")
        node["widgets_values"][0] = value

    selector = _node_by_type(workflow, "ResolutionSelector")
    if selector is None or not isinstance(selector.get("widgets_values"), list):
        raise RuntimeError("Krea2 双图编辑工作流缺少 ResolutionSelector 节点")
    selector["widgets_values"][0] = params["aspect_ratio"]
    selector["widgets_values"][1] = params["megapixels"]

    save = _node_by_type(workflow, "SaveImage")
    if save is None or not isinstance(save.get("widgets_values"), list):
        raise RuntimeError("Krea2 双图编辑工作流缺少 SaveImage 节点")
    save["widgets_values"][0] = prefix
    return workflow


def stage_input(source: Path, tag: str) -> str:
    """把图片按其内容指纹复制进 ComfyUI input，重复使用时不再重写。"""
    source = Path(source)
    if not source.is_file():
        raise RuntimeError(f"候选人物图缺少输入图片：{source}")
    digest = hashlib.sha1(source.read_bytes()).hexdigest()[:10]
    name = f"batch_{tag}_{digest}{source.suffix.lower() or '.png'}"
    target = COMFY_INPUT / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or target.stat().st_size != source.stat().st_size:
        shutil.copy2(source, target)
    return name


def _submit_error(response: httpx.Response, payload: dict[str, Any] | None = None) -> str:
    body = payload
    if body is None:
        try:
            body = response.json()
        except ValueError:
            return f"ComfyUI 拒绝了候选人物图（HTTP {response.status_code}）：{response.text[:400]}"
    errors = body.get("node_errors") or {}
    detail = "; ".join(
        f"节点 {node_id}：" + "、".join(str(e.get("details") or e.get("message")) for e in rows)
        for node_id, rows in errors.items()
    )
    return detail or str(body.get("error") or body)[:400] or "ComfyUI 拒绝了候选人物图"


def _first_image(record: dict[str, Any]) -> Path:
    from .settings import COMFY_OUTPUT

    for output in (record.get("outputs") or {}).values():
        for image in output.get("images") or []:
            path = COMFY_OUTPUT / str(image.get("subfolder") or "") / str(image.get("filename") or "")
            if path.is_file():
                return path
    raise RuntimeError("候选人物图工作流已结束，但没有产出图片")


async def _watch_progress(
    prompt_id: str,
    client_id: str,
    state: dict[str, Any],
    on_progress: ProgressCallback | None,
) -> None:
    """监听 ComfyUI 广播，把采样步数换成真实百分比。"""
    try:
        url = f"{COMFY_WS}/ws?clientId={client_id}"
        async with websockets.connect(url, max_size=64 * 1024 * 1024, open_timeout=20) as socket:
            while not state.get("done"):
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                event = json.loads(raw)
                kind = event.get("type")
                data = event.get("data") or {}
                if kind == "progress" and data.get("max"):
                    fraction = min(0.98, float(data.get("value") or 0) / float(data["max"]))
                    state["fraction"] = fraction
                    if on_progress:
                        await on_progress(fraction, "正在生成候选人物图")
                elif kind in {"execution_error", "execution_interrupted"}:
                    state["error"] = json.dumps(data, ensure_ascii=False)[:600]
                    state["done"] = True
    except asyncio.CancelledError:
        raise
    except Exception:  # 监听失败不影响主流程，退回轮询判定完成
        return


async def generate_portrait(
    *,
    scene_image: Path,
    identity_image: Path,
    prompt: str,
    ratio: str,
    output_path: Path,
    prefix: str = "krea2_batch",
    on_progress: ProgressCallback | None = None,
) -> Path:
    """用本地 Krea2 双图编辑生成候选人物图，并把结果写到 output_path。"""
    if not PORTRAIT_WORKFLOW.is_file():
        raise RuntimeError(f"找不到候选人物图工作流：{PORTRAIT_WORKFLOW}")
    scene_name, identity_name = await asyncio.gather(
        asyncio.to_thread(stage_input, Path(scene_image), "scene"),
        asyncio.to_thread(stage_input, Path(identity_image), "identity"),
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)

    async with pipeline_lock:
        await resources.ensure_comfy()
        workflow = prepare_portrait_workflow(
            load_workflow(PORTRAIT_WORKFLOW),
            scene_image=scene_name,
            identity_image=identity_name,
            prompt=prompt,
            ratio=ratio,
            prefix=prefix,
        )
        api_prompt = graph_to_api_prompt(workflow, await object_info())
        client_id = uuid.uuid4().hex
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{COMFY_URL}/prompt", json={"prompt": api_prompt, "client_id": client_id}
            )
            if response.status_code != 200:
                raise RuntimeError(_submit_error(response))
            payload = response.json()
            if payload.get("node_errors"):
                raise RuntimeError(_submit_error(response, payload))
            prompt_id = str(payload["prompt_id"])
            produced = await _await_image(client, prompt_id, client_id, on_progress)

    await asyncio.to_thread(shutil.copy2, produced, output_path)
    return output_path


async def _await_image(
    client: httpx.AsyncClient,
    prompt_id: str,
    client_id: str,
    on_progress: ProgressCallback | None,
) -> Path:
    state: dict[str, Any] = {"fraction": 0.0, "done": False, "error": None}
    watcher = asyncio.create_task(_watch_progress(prompt_id, client_id, state, on_progress))
    started = time.monotonic()
    try:
        while True:
            if state.get("error"):
                raise RuntimeError(f"候选人物图生成失败：{state['error']}")
            if time.monotonic() - started > GENERATION_TIMEOUT:
                raise RuntimeError("候选人物图生成超时（30 分钟），可点击重试")
            history = (await client.get(f"{COMFY_URL}/history/{prompt_id}")).json()
            if prompt_id in history:
                record = history[prompt_id]
                status = (record.get("status") or {}).get("status_str")
                if status != "success":
                    messages = (record.get("status") or {}).get("messages") or []
                    detail = next(
                        (
                            json.dumps(row[1], ensure_ascii=False)[:400]
                            for row in messages
                            if row and row[0] in {"execution_error", "execution_interrupted"}
                        ),
                        status or "未知错误",
                    )
                    raise RuntimeError(f"候选人物图生成失败：{detail}")
                if on_progress:
                    await on_progress(1.0, "候选人物图已生成")
                return _first_image(record)
            await asyncio.sleep(2)
    finally:
        state["done"] = True
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
