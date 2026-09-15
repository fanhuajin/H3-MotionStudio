"""automation.settings — 集中读 config.json，所有路径/开关不硬编码。

用法：
    from automation import settings
    cfg = settings.load()          # 每次调用重新读，方便改完立刻生效
    cfg["backend_url"]
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# 本项目仓库根目录（automation/ 的上级）
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "automation" / "config.json"


def load(config_path: str | os.PathLike | None = None) -> dict:
    """读取配置。环境变量可覆盖关键项（命名约定 H3AUT_<KEY>）。"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    # 环境变量覆盖：H3AUT_BACKEND_URL 等（大写、- 转 _）
    env_overrides = {
        "backend_url": "H3AUT_BACKEND_URL",
        "publish_root": "H3AUT_PUBLISH_ROOT",
        "chatgpt_profile": "H3AUT_CHATGPT_PROFILE",
        "cdp_port": "H3AUT_CDP_PORT",
        "identity_image": "H3AUT_IDENTITY_IMAGE",
    }
    for key, env in env_overrides.items():
        val = os.getenv(env)
        if val:
            if key == "cdp_port":
                cfg[key] = int(val)
            else:
                cfg[key] = val
    return cfg


def work_dir(cfg: dict) -> Path:
    """自动化工作目录（放日志/中间文件）。"""
    wd = ROOT / str(cfg.get("work_dir", "data/automation"))
    wd.mkdir(parents=True, exist_ok=True)
    return wd
