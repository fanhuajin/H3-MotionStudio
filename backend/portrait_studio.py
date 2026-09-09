from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


IDENTITY_PATH = Path(os.getenv("H3_PORTRAIT_IDENTITY", r"E:\AI_Assets\PortraitIdentity\本人固定参考.png"))
GUIDE_PATHS = {
    "4x3": Path(r"E:\AI_Assets\PortraitIdentity\4x3唱歌构图参考.png"),
    "9x16": Path(r"E:\AI_Assets\PortraitIdentity\9x16跳舞构图参考.png"),
}
OUTPUT_ROOT = Path(os.getenv("H3_PORTRAIT_OUTPUT", r"E:\AI_Exports\PortraitStudio"))
ANALYSIS_MODEL = os.getenv("H3_OPENAI_ANALYSIS_MODEL", "gpt-5.5")
IMAGE_MODEL = os.getenv("H3_OPENAI_IMAGE_MODEL", "gpt-image-2.5-sunburst")
OPENAI_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def _headers() -> dict[str, str]:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("尚未配置 OPENAI_API_KEY，无法调用图片分析与生成。")
    return {"Authorization": f"Bearer {key}"}


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _output_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    parts: list[str] = []
    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts)


def _json_object(text: str) -> dict[str, Any]:
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = clean.find("{"), clean.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("分析接口没有返回可用的结构化结果。")
    return json.loads(clean[start : end + 1])


def analysis_prompt(mode: str, has_style: bool, notes: str) -> str:
    purpose = "4:3 横版唱歌胸像近景" if mode == "4:3" else "9:16 竖版跳舞人物参考图"
    source = (
        "输入图片顺序：图一是造型与场景参考图，只负责发型、妆容、服装、配饰、姿态、背景、灯光和风格；图二是唯一人物身份与面部参考图。不得混入图一人物的脸。"
        if has_style
        else "只有一张输入图，它是唯一人物身份与面部参考图。没有造型参考图，请根据用途和用户要求提出完整造型方案。"
    )
    return f"""你是写实人物定妆与后续视频稳定性顾问。{source}
目标用途：{purpose}。
用户补充要求：{notes.strip() or '未指定，请主动提出适合的方案。'}

现在只分析和提案，绝对不要生成图片。重点判断背景是否干净、真实、有纵深且适合后续 ComfyUI；服装是否过透、过露、层次复杂、容易与身体融合或造成肩臂错误。也分析发型、发色、妆容、耳环项链发饰、动作与构图。
4:3 唱歌要求胸像近景、正对镜头、头顶仅 0%～2% 留白、嘴唇自然微张，脸和嘴部稳定清楚。
9:16 跳舞要求紧凑上半身近景、人物占画面 88%～93%、脸占高度 28%～32%、头顶 5%～8%，手部和身体轮廓稳定。
只返回 JSON，不要 Markdown，格式：
{{"summary":"一句总评","background":{{"decision":"建议换|建议保留","reason":"原因","proposal":"确认后采用的具体方案"}},"clothing":{{"decision":"建议换|建议保留","reason":"原因","proposal":"具体款式、颜色、材质、领口和风格"}},"hair":"建议","makeup":"建议","accessories":"建议","pose":"建议","composition":"构图调整","risks":["风险"],"confirmedPrompt":"可编辑的最终方案摘要"}}"""


async def analyze(mode: str, style_path: Path | None, identity_path: Path, notes: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": analysis_prompt(mode, style_path is not None, notes)}]
    if style_path:
        content.append({"type": "input_image", "image_url": _data_url(style_path), "detail": "high"})
    content.append({"type": "input_image", "image_url": _data_url(identity_path), "detail": "high"})
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{OPENAI_URL}/responses",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"model": ANALYSIS_MODEL, "store": False, "input": [{"role": "user", "content": content}]},
        )
    if response.is_error:
        raise RuntimeError(_api_error(response))
    return _json_object(_output_text(response.json()))


def generation_prompt(mode: str, has_style: bool, plan: str, notes: str) -> str:
    identity = (
        "图一是造型与场景参考，图二是唯一人物身份与面部参考。图一绝不负责人物身份；最终人物必须明确是图二同一个人。"
        if has_style
        else "唯一输入图是人物身份与面部参考，也是唯一身份锚点。"
    )
    common = """准确保留身份图人物的脸型、五官比例、眼睛与眼距、眉毛、鼻梁鼻尖、嘴唇、面颊、下颌线、肤色、年龄感和气质。禁止网红脸、模板脸、欧美脸、过度幼态、过度瘦脸、放大眼睛、重塑鼻嘴或下颌。真实写实摄影，脸部高清自然、五官边缘明确、皮肤保留适度纹理；自然柔光，主体亮于背景。不要文字、字幕、水印、Logo、平台标志、多余人物、多余肢体、畸形手指、塑料皮肤或明显 AI 痕迹。"""
    if mode == "4:3":
        layout = """生成 4:3 横版高清写实唱歌胸像近景（1536×1024）。人物居中正对镜头，从头顶到胸口下方，双肩完整；头发最高点贴近上边缘，顶部仅 0%～2% 空间。脸约占画面高度 40%～45%，人物占 85%～92%，正常人像镜头透视，无自拍广角畸变。人物正在轻柔唱歌，嘴唇自然微张，上排前牙可自然露出、下排仅少量；牙齿真实，口腔有自然深度，禁止闭嘴、大笑、夸张口型、舌头、重复或粘连牙齿。眼睛看镜头，脸、嘴、下巴、头发和肩部轮廓稳定清晰。"""
    else:
        layout = """生成 9:16 竖版高清写实跳舞人物参考图（1024×1536）。紧凑上半身近景，从头顶到腰部附近，人物靠近镜头且占画面 88%～93%，脸占高度 28%～32%，头顶留白 5%～8%，双肩尽量完整。正脸或接近正脸，五官完整；保留确认后的舞蹈造型和动作气质，但避免高速、大幅侧转和手遮脸。手部入镜时必须自然完整、手指数正确。背景简洁真实、有纵深、服务主体，适合后续动作迁移。"""
    return f"{identity}\n{common}\n{layout}\n用户已经确认的方案：{plan}\n本次补充要求：{notes or '无'}"


def _api_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        err = body.get("error") or body
        return f"OpenAI 接口错误 {response.status_code}：{err.get('message') or json.dumps(err, ensure_ascii=False)}"
    except Exception:
        return f"OpenAI 接口错误 {response.status_code}：{response.text[:500]}"


async def generate(mode: str, style_path: Path | None, identity_path: Path, plan: str, notes: str) -> Path:
    size = "1536x1024" if mode == "4:3" else "1024x1536"
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    if style_path:
        files.append(("image[]", (style_path.name, style_path.read_bytes(), mimetypes.guess_type(style_path.name)[0] or "image/png")))
    files.append(("image[]", (identity_path.name, identity_path.read_bytes(), mimetypes.guess_type(identity_path.name)[0] or "image/png")))
    data = {"model": IMAGE_MODEL, "prompt": generation_prompt(mode, style_path is not None, plan, notes), "size": size, "quality": "high", "output_format": "png"}
    async with httpx.AsyncClient(timeout=600) as client:
        response = await client.post(f"{OPENAI_URL}/images/edits", headers=_headers(), data=data, files=files)
    if response.is_error:
        raise RuntimeError(_api_error(response))
    payload = response.json()
    encoded = (payload.get("data") or [{}])[0].get("b64_json")
    if not encoded:
        raise RuntimeError("图片接口未返回图像数据。")
    folder = OUTPUT_ROOT / ("4x3" if mode == "4:3" else "9x16")
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}_{'唱歌' if mode == '4:3' else '跳舞'}.png"
    target = folder / name
    target.write_bytes(base64.b64decode(encoded))
    return target
