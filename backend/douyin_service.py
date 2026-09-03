from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .settings import DATA_DIR

logger = logging.getLogger("uvicorn.error")


DOUYIN_ROOT = Path(
    os.getenv("H3_DOUYIN_DOWNLOADER_ROOT", r"D:\project\douyin-downloader")
).resolve()
DOUYIN_URL = os.getenv("H3_DOUYIN_DOWNLOADER_URL", "http://127.0.0.1:9000").rstrip("/")
# 默认保存到 D:\EV；可用 H3_DOUYIN_OUTPUT 覆盖。下载服务子进程通过
# DOUYIN_PATH 环境变量使用同一目录，保证服务落盘与这里查找结果一致。
DOUYIN_OUTPUT = Path(os.getenv("H3_DOUYIN_OUTPUT", r"D:\EV")).resolve()
DOUYIN_PYTHON = DOUYIN_ROOT / ".venv" / "Scripts" / "python.exe"
DOUYIN_RUN = DOUYIN_ROOT / "run.py"
DOUYIN_LOG = DATA_DIR / "douyin-downloader.log"


class DouyinServiceError(RuntimeError):
    pass


class DouyinServiceOffline(DouyinServiceError):
    """The downloader service is intentionally not running.

    It is started on demand (download submission / login actions only) and
    stopped again once idle to free memory for ComfyUI; read endpoints fall
    back to the on-disk job mirror instead of starting it.
    """

    def __init__(self) -> None:
        super().__init__("下载服务未运行，提交任务或打开登录窗口时自动启动")


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _cookie_ready() -> bool:
    ready, _ = _cookie_stats()
    return ready


def _cookie_stats() -> tuple[bool, int]:
    """Local cookie-file inspection: (ready, non-empty value count).

    Used while the downloader service is offline so login status still
    reflects cookies persisted on disk without starting the service.
    """
    count = 0
    for path in (
        DOUYIN_ROOT / ".cookies.json",
        DOUYIN_ROOT / "config" / "cookies.json",
    ):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            values = [value for value in payload.values() if str(value).strip()]
            count += len(values)
        elif isinstance(payload, list) and payload:
            count += len(payload)
        if count:
            return True, count
    return False, count


def _extract_aweme_id(url: str) -> str | None:
    match = re.search(r"/(?:video|note|gallery|slides)/(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]modal_id=(\d+)", url)
    return match.group(1) if match else None


def is_douyin_url(value: str) -> bool:
    return bool(
        re.search(
            r"https?://(?:[\w-]+\.)*(?:douyin\.com|iesdouyin\.com)(?=[/:?#]|$)",
            value,
            re.IGNORECASE,
        )
    )


class DouyinServiceManager:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.log_handle = None
        self.lock = asyncio.Lock()
        # Monotonic timestamp of the last real download/login activity.
        # The housekeeping loop stops the service after this goes idle so
        # its memory is freed for ComfyUI.
        self.last_activity: float = 0.0

    def mark_activity(self) -> None:
        self.last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity if self.last_activity else float("inf")

    async def healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                response = await client.get(f"{DOUYIN_URL}/api/v1/health")
                return response.status_code == 200 and response.json().get("status") == "ok"
        except (httpx.HTTPError, ValueError):
            return False

    async def ensure_running(self) -> None:
        self.mark_activity()
        if await self.healthy():
            return
        async with self.lock:
            if await self.healthy():
                return
            if not DOUYIN_PYTHON.is_file() or not DOUYIN_RUN.is_file():
                raise DouyinServiceError(
                    f"未找到抖音下载服务，请确认项目位于 {DOUYIN_ROOT}"
                )
            DOUYIN_LOG.parent.mkdir(parents=True, exist_ok=True)
            DOUYIN_OUTPUT.mkdir(parents=True, exist_ok=True)
            self.log_handle = DOUYIN_LOG.open("a", encoding="utf-8")
            parsed_url = urlparse(DOUYIN_URL)
            service_host = parsed_url.hostname or "127.0.0.1"
            service_port = str(parsed_url.port or 9000)
            child_env = os.environ.copy()
            child_env["DOUYIN_PATH"] = str(DOUYIN_OUTPUT)
            self.process = subprocess.Popen(
                [
                    str(DOUYIN_PYTHON),
                    str(DOUYIN_RUN),
                    "--serve",
                    "--serve-host",
                    service_host,
                    "--serve-port",
                    service_port,
                ],
                cwd=str(DOUYIN_ROOT),
                env=child_env,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                creationflags=_creation_flags(),
            )
            for _ in range(40):
                if self.process.poll() is not None:
                    raise DouyinServiceError(
                        f"抖音下载服务启动失败，退出码 {self.process.returncode}"
                    )
                if await self.healthy():
                    return
                await asyncio.sleep(0.5)
            raise DouyinServiceError("抖音下载服务启动超时")

    async def stop(self) -> None:
        """Terminate the downloader service to free its memory."""
        async with self.lock:
            if self.process and self.process.poll() is None:
                logger.info("停止抖音下载服务以释放内存")
                self.process.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(self.process.wait), timeout=8)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await asyncio.to_thread(self.process.wait)
            if self.log_handle:
                self.log_handle.close()
            self.process = None
            self.log_handle = None
            self.last_activity = 0.0

    async def status(self) -> dict[str, Any]:
        available = DOUYIN_PYTHON.is_file() and DOUYIN_RUN.is_file()
        connected = await self.healthy()
        cookie_ready = _cookie_ready()
        if connected:
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    response = await client.get(f"{DOUYIN_URL}/api/v1/auth/status")
                if response.status_code == 200:
                    cookie_ready = bool(response.json().get("cookieReady"))
            except (httpx.HTTPError, ValueError):
                pass
        return {
            "available": available,
            "connected": connected,
            "cookieReady": cookie_ready,
            "serviceUrl": DOUYIN_URL,
            "outputDirectory": str(DOUYIN_OUTPUT),
            "message": (
                "下载服务已连接"
                if connected
                else "下载服务将在首次任务时自动启动"
                if available
                else "未找到抖音下载服务"
            ),
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float = 20,
        start_if_needed: bool = False,
    ) -> Any:
        """Call the downloader service.

        Only user-initiated actions (download submission, login) pass
        ``start_if_needed=True``; everything else reports the service as
        offline rather than starting it, so plain page loads never consume
        RAM that ComfyUI may need.
        """
        if not await self.healthy():
            if not start_if_needed:
                raise DouyinServiceOffline()
            await self.ensure_running()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(method, f"{DOUYIN_URL}{path}", json=json)
        except httpx.HTTPError as exc:
            raise DouyinServiceError(f"无法连接抖音下载服务：{exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text
            raise DouyinServiceError(detail or f"下载服务返回 HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise DouyinServiceError("下载服务返回了无法识别的数据") from exc

    async def submit(self, url: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/v1/download", json={"url": url}, start_if_needed=True
        )

    async def job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/jobs/{job_id}")

    async def jobs(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/jobs")

    async def auth_status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/auth/status", timeout=5)

    async def start_login(self) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/v1/auth/login/start", timeout=45, start_if_needed=True
        )

    async def finish_login(self) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/v1/auth/login/finish", timeout=10, start_if_needed=True
        )

    async def cancel_login(self) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/v1/auth/login/cancel", timeout=10, start_if_needed=True
        )

    def result_for(self, job: dict[str, Any]) -> dict[str, Any] | None:
        aweme_id = _extract_aweme_id(str(job.get("url") or ""))
        if not aweme_id or not DOUYIN_OUTPUT.is_dir():
            return None
        candidates = [
            path
            for path in DOUYIN_OUTPUT.rglob(f"*{aweme_id}*")
            if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}
        ]
        if not candidates:
            return None
        path = max(candidates, key=lambda item: item.stat().st_mtime).resolve()
        try:
            path.relative_to(DOUYIN_OUTPUT)
        except ValueError:
            return None
        return {
            "awemeId": aweme_id,
            "filename": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "mediaType": mimetypes.guess_type(path.name)[0] or "video/mp4",
        }


douyin_service = DouyinServiceManager()
