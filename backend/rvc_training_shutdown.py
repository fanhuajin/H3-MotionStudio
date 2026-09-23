"""Persistent shutdown preference and status for the detached RVC trainer."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psutil


EXPERIMENT = Path(r"E:\AI_Assets\RVC\Logs\mogoo_48k_v2")
SETTINGS = EXPERIMENT / "shutdown_settings.json"
STATE = EXPERIMENT / "shutdown_status.json"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def enabled() -> bool:
    return _read(SETTINGS).get("enabled", True) is True


def set_enabled(value: bool) -> dict:
    _write(SETTINGS, {"enabled": value})
    return status()


def status() -> dict:
    state = _read(STATE)
    watcher_alive = False
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            arguments = process.info["cmdline"] or []
            if (process.info["name"] or "").lower() == "python.exe" and any(
                Path(argument).name.lower() == "rvc_training_shutdown_watch.py" for argument in arguments
            ):
                watcher_alive = True
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {
        "enabled": enabled(),
        "watcherAlive": watcher_alive,
        "state": state.get("state", "waiting"),
        "shutdownAt": state.get("shutdownAt"),
        "error": state.get("error"),
    }


def write_state(value: dict) -> None:
    _write(STATE, value)


def read_state() -> dict:
    return _read(STATE)
