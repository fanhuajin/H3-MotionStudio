from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def load_workflow(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def node_by_id(workflow: dict[str, Any], node_id: int) -> dict[str, Any]:
    for node in workflow.get("nodes", []):
        if int(node.get("id")) == node_id:
            return node
    raise KeyError(f"Workflow node {node_id} not found")


def prepare_singing_workflow(
    source_name: str,
    reference_name: str,
    action_prompt: str,
    camera_prompt: str,
    output_prefix: str,
    source_path,
) -> dict[str, Any]:
    workflow = copy.deepcopy(source_path if isinstance(source_path, dict) else load_workflow(source_path))
    image_node = node_by_id(workflow, 307)
    image_node["widgets_values"][0] = reference_name

    audio_node = node_by_id(workflow, 300)
    audio_node["widgets_values"][0] = source_name

    prompt_node = node_by_id(workflow, 480)
    values = prompt_node["widgets_values"]
    if action_prompt.strip():
        values[1] = action_prompt.strip()
    if camera_prompt.strip():
        values[2] = camera_prompt.strip()

    save_node = node_by_id(workflow, 59)
    save_node["widgets_values"][0] = output_prefix
    return workflow


def prepare_upscale_workflow(
    source_name: str,
    output_prefix: str,
    source_path,
    scale: tuple[int, int] | None = None,
    upscale_model: str | None = None,
) -> dict[str, Any]:
    """1080P 高清加强：替换输入视频与输出前缀；可选替换缩回尺寸与超分模型。

    9:16 竖版时把 ImageScale 的目标尺寸从 1440×1080 换成 1080×1920；
    9:16 迁移链路可用 x2plus 代替 x4plus（目标放大仅 ~2.1×，耗时约 1/4）。
    其余（8 帧分批、逐帧处理、锐化、保留原音频）保持作者默认。
    """
    workflow = copy.deepcopy(source_path if isinstance(source_path, dict) else load_workflow(source_path))
    _set_vhs_widget(workflow, 2, "video", source_name)

    save_node = node_by_id(workflow, 8)
    save_values = save_node.get("widgets_values") or {}
    if not isinstance(save_values, dict):
        raise ValueError("VHS_VideoCombine widgets_values is not an object")
    save_values["filename_prefix"] = output_prefix
    save_values.pop("videopreview", None)

    if scale is not None:
        scale_node = node_by_id(workflow, 5)
        widgets = scale_node.get("widgets_values") or []
        if not isinstance(widgets, list) or len(widgets) < 3:
            raise ValueError("ImageScale widgets_values is not a list of width/height")
        widgets[1], widgets[2] = int(scale[0]), int(scale[1])

    if upscale_model:
        loader_node = node_by_id(workflow, 3)
        loader_widgets = loader_node.get("widgets_values") or []
        if not isinstance(loader_widgets, list) or not loader_widgets:
            raise ValueError("UpscaleModelLoader widgets_values is not a non-empty list")
        loader_widgets[0] = upscale_model
    return workflow


def _set_vhs_widget(workflow: dict[str, Any], node_id: int, key: str, value: Any) -> None:
    """Set a named VHS-style widget (dict widgets_values) on a node."""
    node = node_by_id(workflow, node_id)
    values = node.get("widgets_values") or {}
    if not isinstance(values, dict):
        raise ValueError(f"Node {node_id} widgets_values is not an object")
    values[key] = value
    values.pop("videopreview", None)


def prepare_clean_workflow(
    source_name: str,
    output_prefix: str,
    source_path,
    canvas: dict | None = None,
) -> dict[str, Any]:
    """视频-去字幕-ProPainter-固定底部：替换原视频、输出前缀与可选竖版画布。

    #1 VHS_LoadVideo(带字幕原视频) -> #2 CreateShapeMask(固定底部字幕遮罩) ->
    #3 ProPainterInpaint -> #5 VHS_VideoCombine(带原音频、原帧率)。
    默认(4:3)参数保持作者原状；9:16 时按 canvas 组替换读取尺寸、遮罩坐标与
    ProPainter 画布。
    """
    workflow = copy.deepcopy(source_path if isinstance(source_path, dict) else load_workflow(source_path))
    canvas = canvas or {}
    if canvas:
        _set_vhs_widget(workflow, 1, "custom_width", int(canvas["clean_width"]))
        _set_vhs_widget(workflow, 1, "custom_height", int(canvas["clean_height"]))

        mask = node_by_id(workflow, 2)
        widgets = mask.get("widgets_values") or []
        if not isinstance(widgets, list) or len(widgets) < 9:
            raise ValueError("CreateShapeMask widgets_values is not a 9-item list")
        # 布局: [shape, frames, location_x, location_y, grow, frame_width,
        #        frame_height, shape_width, shape_height]
        widgets[2] = int(canvas["mask_center_x"])
        widgets[3] = int(canvas["mask_center_y"])
        widgets[5] = int(canvas["clean_width"])
        widgets[6] = int(canvas["clean_height"])
        widgets[7] = int(canvas["mask_width"])
        widgets[8] = int(canvas["mask_height"])

        paint = node_by_id(workflow, 3)
        paint_widgets = paint.get("widgets_values") or []
        if not isinstance(paint_widgets, list) or len(paint_widgets) < 2:
            raise ValueError("ProPainterInpaint widgets_values is not a list")
        paint_widgets[0] = int(canvas["clean_width"])
        paint_widgets[1] = int(canvas["clean_height"])

    _set_vhs_widget(workflow, 1, "video", source_name)
    _set_vhs_widget(workflow, 5, "filename_prefix", output_prefix)
    return workflow


def prepare_migrate_workflow(
    drive_name: str,
    reference_name: str,
    mode: str,
    output_prefix: str,
    source_path,
    canvas: dict | None = None,
    content_prompt: str | None = None,
    video_prompt: str | None = None,
    image_prompt: str | None = None,
    unet_model: str | None = None,
    lightx2v_lora: str | None = None,
) -> dict[str, Any]:
    """视频-长视频替换-4x3加速版-ProPainter输入：准备一次动作迁移/人物替换运行。

    #563 SelectVideoPath 提供驱动视频（#469/#543 兜底一并替换）；
    #30 LoadImage 人物参考图；#353 Boolean (Replace Mode)：false=动作迁移, true=人物替换；
    #545/#509/#510 三处提示词（None/空串保留工作流默认）；
    #342/#343 画布宽高（9:16 时联动 VHS 读取/参考图裁剪/SCAIL 生成）；
    #456 VHS_VideoCombine 输出前缀。段长/重叠/步数/CFG/seed 等保持作者默认。
    unet_model/lightx2v_lora：博主 wan21_scail-2_loop 复刻组合（int8_convrot + rank64），
    传 None 保持工作流默认（fp8_scaled + rank128）。
    """
    workflow = copy.deepcopy(source_path if isinstance(source_path, dict) else load_workflow(source_path))
    canvas = canvas or {}
    if canvas:
        for node_id in (342, 343):
            int_node = node_by_id(workflow, node_id)
            widgets = int_node.get("widgets_values") or []
            if not isinstance(widgets, list) or not widgets:
                raise ValueError(f"PrimitiveInt node {node_id} widgets_values is empty")
            widgets[0] = (
                int(canvas["migrate_width"]) if node_id == 342 else int(canvas["migrate_height"])
            )

    if unet_model:
        unet_node = node_by_id(workflow, 329)
        unet_widgets = unet_node.get("widgets_values") or []
        if not isinstance(unet_widgets, list) or not unet_widgets:
            raise ValueError("UNETLoader widgets_values is empty")
        unet_widgets[0] = unet_model
    if lightx2v_lora:
        lora_node = node_by_id(workflow, 322)
        lora_widgets = lora_node.get("widgets_values") or []
        if not isinstance(lora_widgets, list) or not lora_widgets:
            raise ValueError("LoraLoaderModelOnly widgets_values is empty")
        lora_widgets[0] = lightx2v_lora

    selector = node_by_id(workflow, 563)
    selector_widgets = selector.get("widgets_values") or []
    if isinstance(selector_widgets, list) and selector_widgets:
        selector_widgets[0] = drive_name
    else:
        raise ValueError("SelectVideoPath widgets_values is not a non-empty list")
    # 未接线的兜底也替换，避免 ComfyUI 里手动运行时仍是旧文件
    _set_vhs_widget(workflow, 469, "video", drive_name)
    _set_vhs_widget(workflow, 543, "video", drive_name)

    image_node = node_by_id(workflow, 30)
    image_node["widgets_values"][0] = reference_name

    mode_node = node_by_id(workflow, 353)
    mode_node["widgets_values"][0] = mode != "animation"  # 动作迁移=false, 人物替换=true

    if content_prompt and content_prompt.strip():
        content_node = node_by_id(workflow, 545)
        content_node["widgets_values"][0] = content_prompt.strip()
    if video_prompt and video_prompt.strip():
        video_node = node_by_id(workflow, 509)
        video_node["widgets_values"][0] = video_prompt.strip()
    if image_prompt and image_prompt.strip():
        image_node_prompt = node_by_id(workflow, 510)
        image_node_prompt["widgets_values"][0] = image_prompt.strip()

    _set_vhs_widget(workflow, 456, "filename_prefix", output_prefix)
    return workflow


def graph_to_api_prompt(workflow: dict[str, Any], object_info: dict[str, Any]) -> dict[str, Any]:
    links = {str(link[0]): [str(link[1]), int(link[2])] for link in workflow.get("links", [])}
    prompt: dict[str, Any] = {}

    for node in workflow.get("nodes", []):
        node_id = str(node.get("id"))
        class_type = node.get("type")
        if not class_type or class_type not in object_info or int(node.get("mode", 0)) == 2:
            continue

        definition = object_info[class_type].get("input") or {}
        accepted = set((definition.get("required") or {}).keys()) | set((definition.get("optional") or {}).keys())
        # ComfyUI v3 Autogrow 输入（如 ComfyMathExpression 的 "values" + 模板名 a..z）：
        # 工作流 JSON 里每个展开输入以 "容器.模板名" 命名（如 "values.a"），而 object_info
        # 只列出容器名 "values"。把展开名也纳入合法集合，否则链接/值会被丢弃，
        # 服务端会报 required_input_missing: values.a。
        for section in ("required", "optional"):
            for container, spec in (definition.get(section) or {}).items():
                if not (isinstance(spec, list) and spec and spec[0] == "COMFY_AUTOGROW_V3"):
                    continue
                template = ((spec[1] or {}).get("template")) or {}
                for item_name in template.get("names") or []:
                    accepted.add(f"{container}.{item_name}")

        inputs: dict[str, Any] = {}
        widgets = node.get("widgets_values")
        widget_index = 0

        for input_spec in node.get("inputs") or []:
            name = input_spec.get("name")
            widget = input_spec.get("widget")
            widget_value: Any = None
            has_widget_value = False

            if widget is not None:
                widget_name = widget.get("name") or name
                if isinstance(widgets, dict):
                    if widget_name in widgets:
                        widget_value = widgets[widget_name]
                        has_widget_value = True
                elif isinstance(widgets, list):
                    if widget_index < len(widgets):
                        widget_value = widgets[widget_index]
                        has_widget_value = True
                    widget_index += 1
                else:
                    widget_value = widgets
                    has_widget_value = widgets is not None

            if name not in accepted:
                continue

            link_id = input_spec.get("link")
            if link_id is not None and str(link_id) in links:
                inputs[name] = links[str(link_id)]
            elif has_widget_value and widget_value is not None:
                inputs[name] = widget_value

        prompt[node_id] = {
            "class_type": class_type,
            "inputs": inputs,
            "_meta": {"title": node.get("title") or class_type},
        }

    return prompt


def patch_wan_chunk_feedforward(
    prompt: dict[str, Any],
    object_info: dict[str, Any],
    *,
    after_node: str = "561",
    target_nodes: tuple[str, ...] = ("330", "332"),
    chunks: int = 2,
) -> bool:
    """运行时注入 WanChunkFeedForward（KJNodes）到 Wan 模型链。

    只对 9:16（512×896）迁移运行启用：把注意力之后的 FFN 激活分块计算，
    降低采样期峰值显存，避免 8GB 显卡在长段结尾 OOM。不改作者工作流文件。
    节点不在 object_info（未安装/改名）时静默跳过。返回是否注入成功。
    """
    definition = object_info.get("WanChunkFeedForward") if object_info else None
    if not definition:
        return False
    accepted = set(((definition.get("input") or {}).get("required") or {}).keys())
    accepted |= set(((definition.get("input") or {}).get("optional") or {}).keys())
    if after_node not in prompt or "model" not in accepted or "chunks" not in accepted:
        return False
    chunk_id = f"{after_node}_chunk_ffn"
    if chunk_id in prompt:
        return False
    inputs: dict[str, Any] = {"model": [after_node, 0], "chunks": int(chunks)}
    if "dim_threshold" in accepted:
        inputs["dim_threshold"] = 4096
    prompt[chunk_id] = {
        "class_type": "WanChunkFeedForward",
        "inputs": inputs,
        "_meta": {"title": "Wan Chunk FeedForward（自动注入·低显存）"},
    }
    for target in target_nodes:
        if target in prompt:
            prompt[target]["inputs"]["model"] = [chunk_id, 0]
    return True


def workflow_titles(workflow: dict[str, Any]) -> dict[str, str]:
    return {
        str(node["id"]): str(node.get("title") or node.get("type") or node["id"])
        for node in workflow.get("nodes", [])
    }


def workflow_types(workflow: dict[str, Any]) -> dict[str, str]:
    return {str(node["id"]): str(node.get("type") or "") for node in workflow.get("nodes", [])}
