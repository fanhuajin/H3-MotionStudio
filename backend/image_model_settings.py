"""用户保存的图片模型选择；凭据仍只读取环境变量。"""
import json
import re
import threading

from .settings import DATA_DIR

SETTINGS_PATH = DATA_DIR / "image-model.json"
_LOCK = threading.Lock()


def selected_model() -> str:
    with _LOCK:
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))["model"]
        except FileNotFoundError:
            return ""


def save_model(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value):
        raise ValueError("模型名须为 1–128 位英文字母、数字或 . _ : / -")
    with _LOCK:
        temporary = SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps({"model": value}), encoding="utf-8")
        temporary.replace(SETTINGS_PATH)
    return value
