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


def prepare_upscale_workflow(source_name: str, output_prefix: str, source_path) -> dict[str, Any]:
    workflow = copy.deepcopy(source_path if isinstance(source_path, dict) else load_workflow(source_path))
    load_node = node_by_id(workflow, 2)
    values = load_node.get("widgets_values") or {}
    if not isinstance(values, dict):
        raise ValueError("VHS_LoadVideo widgets_values is not an object")
    values["video"] = source_name
    values.pop("videopreview", None)

    save_node = node_by_id(workflow, 8)
    save_values = save_node.get("widgets_values") or {}
    if not isinstance(save_values, dict):
        raise ValueError("VHS_VideoCombine widgets_values is not an object")
    save_values["filename_prefix"] = output_prefix
    save_values.pop("videopreview", None)
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


def workflow_titles(workflow: dict[str, Any]) -> dict[str, str]:
    return {
        str(node["id"]): str(node.get("title") or node.get("type") or node["id"])
        for node in workflow.get("nodes", [])
    }


def workflow_types(workflow: dict[str, Any]) -> dict[str, str]:
    return {str(node["id"]): str(node.get("type") or "") for node in workflow.get("nodes", [])}
