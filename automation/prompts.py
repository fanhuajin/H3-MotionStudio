"""automation.prompts — 读取桌面提示词 txt。

唱歌条目（4:3）用 4比3图片.txt，跳舞条目（9:16）用 9比16图片.txt。
路径全部配置化（config.json 的 prompt_43_path / prompt_916_path）。
"""
from __future__ import annotations

from pathlib import Path


def read_prompt(path: str | Path) -> str:
    """读取提示词文本；文件不存在则抛 FileNotFoundError（带清晰信息）。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"提示词文件不存在：{p}")
    return p.read_text(encoding="utf-8")


def prompt_for_kind(cfg: dict, kind: str) -> str:
    """按条目类型取提示词：singing→4:3，dance→9:16。"""
    if kind == "dance":
        return read_prompt(cfg["prompt_916_path"])
    return read_prompt(cfg["prompt_43_path"])


def identity_image_path(cfg: dict) -> Path:
    """图二：唯一人物身份参考图。"""
    return Path(cfg["identity_image"])
