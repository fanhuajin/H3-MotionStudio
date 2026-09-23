"""Independent watcher: shut down only after a successful 500-epoch RVC run."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import rvc_training_shutdown as shutdown_state  # noqa: E402


EXPERIMENT = shutdown_state.EXPERIMENT
COMPLETE_EPOCH = re.compile(r"====> 轮次：500\b")
WINDOWS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def completed() -> bool:
    try:
        job = json.loads((EXPERIMENT / "job_status.json").read_text(encoding="utf-8"))
        if job.get("stage") != "training_complete":
            return False
        with (EXPERIMENT / "train.log").open("rb") as log:
            log.seek(max(0, log.seek(0, 2) - 100_000))
            tail = log.read().decode("utf-8", errors="replace")
        return bool(COMPLETE_EPOCH.search(tail))
    except (OSError, ValueError):
        return False


def command(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["shutdown", *arguments], capture_output=True, text=True, creationflags=WINDOWS_FLAGS)


def main() -> None:
    if not shutdown_state.read_state():
        shutdown_state.write_state({"state": "waiting", "shutdownAt": None})
    while True:
        try:
            state = shutdown_state.read_state()
            enabled = shutdown_state.enabled()
            finished = completed()
            if state.get("state") == "scheduled" and not enabled:
                result = command("/a")
                shutdown_state.write_state({"state": "cancelled", "shutdownAt": None, "error": result.stderr.strip() if result.returncode else None})
            elif finished and enabled and state.get("state") != "scheduled":
                result = command("/s", "/t", "300", "/c", "RVC 48k training completed 500 epochs")
                if result.returncode:
                    shutdown_state.write_state({"state": "error", "shutdownAt": None, "error": result.stderr.strip() or f"exit {result.returncode}"})
                else:
                    shutdown_state.write_state({"state": "scheduled", "shutdownAt": (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")})
            elif not finished and state.get("state") not in {"waiting", "scheduled"}:
                shutdown_state.write_state({"state": "waiting", "shutdownAt": None})
        except Exception as exc:
            shutdown_state.write_state({"state": "error", "shutdownAt": None, "error": repr(exc)})
        time.sleep(15)


if __name__ == "__main__":
    main()
