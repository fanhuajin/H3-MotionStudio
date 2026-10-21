"""本地项目关闭入口；只停止经验证的项目启动进程树。"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
import psutil

from .settings import DATA_DIR, PROJECT_ROOT

router = APIRouter(prefix="/api/system", tags=["system"])
_LOCK = threading.Lock()
_HELPER: subprocess.Popen | None = None
_CLOSING = threading.Event()


def project_is_closing():
    return _CLOSING.is_set() and (_HELPER is None or _HELPER.poll() is None)


def is_project_process(process, root: Path):
    arguments = process.cmdline()
    if "backend.app:app" in arguments and Path(process.cwd()).resolve() == root.resolve():
        return True
    launchers = {str(root / "scripts" / "start-local.ps1").casefold(),
                 str(root / "启动H3影动高清工作台.bat").casefold()}
    return any(arg.replace('"', '').strip().casefold() in launchers for arg in arguments[1:])


def shutdown_target():
    current = psutil.Process(os.getpid())
    chain = [current, *current.parents()]
    backend = next((i for i, process in enumerate(chain)
                    if "backend.app:app" in process.cmdline()
                    and Path(process.cwd()).resolve() == PROJECT_ROOT.resolve()), None)
    if backend is None:
        raise ValueError("未识别到本项目的服务进程，已取消关闭。")
    target = chain[backend]
    for parent in chain[backend + 1:]:
        if not is_project_process(parent, PROJECT_ROOT):
            break
        target = parent
    return target.pid, target.create_time()


def validate_local_request(request: Request):
    try:
        local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        local = False
    origin = request.headers.get("origin")
    trusted = {"localhost", "127.0.0.1", "::1", "terminal.local"}
    if not local or (origin and urlsplit(origin).hostname not in trusted):
        raise HTTPException(403, "只能从本机工作台关闭项目。")


@router.post("/close-project")
def close_project(request: Request):
    global _HELPER
    validate_local_request(request)
    with _LOCK:
        if _HELPER is not None and _HELPER.poll() is None:
            return {"accepted": True}
        try:
            pid, created = shutdown_target()
            _CLOSING.set()
            # 已经安排的本项目自动关机需撤销，关闭项目不会关闭电脑。
            from .batch_worker import cancel_pending_shutdown
            from .batch_store import batch_store
            cancelled = cancel_pending_shutdown(notice="关闭项目，已取消本次自动关机。")
            if cancelled.get("batchId"):
                try:
                    batch_store.update(cancelled["batchId"], shutdownOnComplete=False, shutdownAfterPrep=False)
                except KeyError:
                    pass
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            with (DATA_DIR / "project-close.log").open("ab") as log:
                _HELPER = subprocess.Popen([
                    sys.executable, "-B", str(PROJECT_ROOT / "scripts" / "stop-project.py"),
                    "--pid", str(pid), "--created", str(created), "--root", str(PROJECT_ROOT),
                ], cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=flags, close_fds=True)
        except (ValueError, OSError, psutil.Error) as error:
            _CLOSING.clear()
            raise HTTPException(409, f"未能关闭项目：{error}") from error
    return {"accepted": True}


@router.get("/project-status")
def project_status():
    return {"running": True}
