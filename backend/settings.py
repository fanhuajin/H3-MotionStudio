from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

COMFY_HOME = Path(os.getenv("H3_COMFY_HOME", r"D:\Comfyui")).resolve()
COMFY_ROOT = COMFY_HOME / "ComfyUI"
COMFY_INPUT = COMFY_ROOT / "input"
COMFY_OUTPUT = COMFY_ROOT / "output"
COMFY_PYTHON = COMFY_HOME / "python_embeded" / "python.exe"
COMFY_MAIN = COMFY_ROOT / "main.py"
COMFY_URL = os.getenv("H3_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
COMFY_WS = COMFY_URL.replace("http://", "ws://").replace("https://", "wss://")

WORKFLOW_DIR = COMFY_ROOT / "user" / "default" / "workflows" / "video"
SINGING_WORKFLOW = WORKFLOW_DIR / "视频-单图唱歌-自动拼接40秒内-4x3-运镜版.json"
UPSCALE_WORKFLOW = WORKFLOW_DIR / "视频-成片输入-独立二采-RealESRGAN4x转1080P-8GB高清加强版.json"
FIXED_REFERENCE = COMFY_INPUT / "25181125-唱歌优化-指定背景-1440x1080-v3.png"

RVC_ROOT = COMFY_HOME / "RVC"
RVC_PYTHON = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
RVC_SCRIPT = RVC_ROOT / "convert_video_to_my_voice.py"
RVC_MODEL = RVC_ROOT / "assets" / "weights" / "我的音色_测试_v1.pth"
RVC_INDEX = RVC_ROOT / "assets" / "indices" / "my_voice_test_v1_added_IVF96_Flat_nprobe_1_my_voice_test_v1_v2.index"

DB_PATH = DATA_DIR / "motionstudio.db"
COMFY_LOG = DATA_DIR / "comfyui.log"
MAX_DURATION_SECONDS = 40.0


def required_paths() -> dict[str, Path]:
    return {
        "ComfyUI Python": COMFY_PYTHON,
        "ComfyUI main.py": COMFY_MAIN,
        "唱歌工作流": SINGING_WORKFLOW,
        "高清工作流": UPSCALE_WORKFLOW,
        "固定人物图片": FIXED_REFERENCE,
        "RVC Python": RVC_PYTHON,
        "RVC 转换脚本": RVC_SCRIPT,
        "我的音色模型": RVC_MODEL,
        "我的音色索引": RVC_INDEX,
    }

