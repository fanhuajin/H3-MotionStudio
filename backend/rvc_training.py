"""Read-only progress for the detached mogoo RVC training job."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import psutil


EXPERIMENT = Path(r"E:\AI_Assets\RVC\Logs\mogoo_48k_v2")
TARGET_EPOCHS = 500
EPOCH_RE = re.compile(r"====> 轮次：(\d+)")
CURRENT_RE = re.compile(r"训练轮次：(\d+) \[(\d+)%\]")


def _tail(path: Path, limit: int = 300_000) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as file:
        file.seek(max(0, path.stat().st_size - limit))
        return file.read().decode("utf-8", errors="replace")


def _runner_alive() -> bool:
    for process in psutil.process_iter(["cmdline"]):
        try:
            command = " ".join(process.info["cmdline"] or []).lower()
            if "run_mogoo_48k_500.py" in command and "python" in command:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def progress() -> dict:
    status_path = EXPERIMENT / "job_status.json"
    try:
        job = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job = {}
    history = _tail(EXPERIMENT / "job.log", 30_000)
    first = next((line for line in history.splitlines() if '"stage": "started"' in line), "")
    try:
        started_at = datetime.fromisoformat(json.loads(first)["time"])
    except (ValueError, KeyError, TypeError):
        started_at = None
    log = _tail(EXPERIMENT / "train.log")
    completed = max((int(value) for value in EPOCH_RE.findall(log)), default=0)
    current_matches = CURRENT_RE.findall(log)
    current, current_percent = (int(value) for value in current_matches[-1]) if current_matches else (completed, 0)
    if current <= completed:
        current_percent = 100 if completed else 0
    alive = _runner_alive()
    stage = str(job.get("stage") or "not_started")
    now = datetime.now()
    elapsed = max(0, int((now - started_at).total_seconds())) if started_at else 0
    if stage == "training_complete" or completed >= TARGET_EPOCHS:
        state = "completed"
    elif stage == "failed":
        state = "failed"
    elif alive:
        state = "running"
    elif job:
        state = "stopped"
    else:
        state = "not_started"
    # Estimate only after several full epochs; early setup time distorts the rate.
    epoch_lines = [line for line in log.splitlines() if EPOCH_RE.search(line)]
    eta = None
    if state == "running" and len(epoch_lines) >= 5:
        try:
            recent = epoch_lines[-5:]
            first_time = datetime.fromisoformat(recent[0][:19].replace(" ", "T"))
            last_time = datetime.fromisoformat(recent[-1][:19].replace(" ", "T"))
            first_epoch = int(EPOCH_RE.search(recent[0]).group(1))
            last_epoch = int(EPOCH_RE.search(recent[-1]).group(1))
            rate = (last_time - first_time).total_seconds() / (last_epoch - first_epoch)
            eta = max(0, int((TARGET_EPOCHS - completed) * rate))
        except (ValueError, ZeroDivisionError, AttributeError):
            pass
    return {
        "name": "mogoo_48k_v2",
        "state": state,
        "stage": stage,
        "completedEpochs": completed,
        "currentEpoch": current,
        "targetEpochs": TARGET_EPOCHS,
        "progress": round(100 * (completed + (current_percent / 100 if current > completed else 0)) / TARGET_EPOCHS, 2),
        "currentEpochPercent": current_percent,
        "startedAt": started_at.isoformat() if started_at else None,
        "elapsedSeconds": elapsed,
        "etaSeconds": eta,
        "lastUpdate": log.splitlines()[-1][:19].replace(" ", "T") if log else job.get("time"),
        "refreshedAt": now.isoformat(timespec="seconds"),
        "recentLog": log.splitlines()[-5:],
    }
