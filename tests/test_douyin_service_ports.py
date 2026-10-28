import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend import douyin_service as service


class DouyinServicePortTests(unittest.IsolatedAsyncioTestCase):
    def test_reserved_port_uses_system_assigned_port(self):
        preferred = MagicMock()
        preferred.__enter__.return_value = preferred
        preferred.bind.side_effect = PermissionError(13, "reserved port")
        fallback = MagicMock()
        fallback.__enter__.return_value = fallback
        fallback.getsockname.return_value = ("127.0.0.1", 52123)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(service.socket, "socket", side_effect=[preferred, fallback]),
        ):
            self.assertEqual(
                service._available_service_url("http://127.0.0.1:9100"),
                "http://127.0.0.1:52123",
            )
        preferred.bind.assert_called_once_with(("127.0.0.1", 9100))
        fallback.bind.assert_called_once_with(("127.0.0.1", 0))

    def test_available_preferred_port_is_kept(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(service.socket, "socket") as socket_factory,
        ):
            self.assertEqual(
                service._available_service_url("http://127.0.0.1:9100"),
                "http://127.0.0.1:9100",
            )
        socket_factory.assert_called_once()

    def test_explicit_endpoint_is_not_replaced(self):
        url = "http://127.0.0.1:9100"
        with (
            patch.dict(os.environ, {"H3_DOUYIN_DOWNLOADER_URL": url}),
            patch.object(service.socket, "socket") as socket_factory,
        ):
            self.assertEqual(service._available_service_url(url), url)
        socket_factory.assert_not_called()

    async def test_selected_port_reaches_process_requests_and_status(self):
        manager = service.DouyinServiceManager()
        selected = "http://127.0.0.1:52123"
        process = MagicMock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "python.exe"
            executable.touch()
            run = root / "run.py"
            run.touch()
            with (
                patch.object(service, "DOUYIN_PYTHON", executable),
                patch.object(service, "DOUYIN_RUN", run),
                patch.object(service, "DOUYIN_OUTPUT", root / "output"),
                patch.object(service, "DOUYIN_LOG", root / "service.log"),
                patch.object(service, "_available_service_url", return_value=selected),
                patch.object(service.subprocess, "Popen", return_value=process) as launch,
                patch.object(manager, "healthy", AsyncMock(side_effect=[False, False, True])),
            ):
                try:
                    await manager.ensure_running()
                    command = launch.call_args.args[0]
                    self.assertEqual(command[command.index("--serve-port") + 1], "52123")
                    self.assertEqual(manager.url, selected)
                finally:
                    if manager.log_handle:
                        manager.log_handle.close()
            response = MagicMock(status_code=200)
            response.json.return_value = {"status": "ok"}
            client = AsyncMock()
            client.request.return_value = response
            client.get.return_value = response
            with patch.object(service.httpx, "AsyncClient") as client_factory:
                client_factory.return_value.__aenter__.return_value = client
                self.assertTrue(await manager.healthy())
                client.get.assert_awaited_with(f"{selected}/api/v1/health")
                await manager._request("GET", "/api/v1/jobs")
                client.request.assert_awaited_with("GET", f"{selected}/api/v1/jobs", json=None)
                self.assertEqual((await manager.status())["serviceUrl"], selected)

    async def test_unhealthy_live_process_is_not_replaced(self):
        manager = service.DouyinServiceManager()
        manager.process = MagicMock()
        manager.process.poll.return_value = None
        with (
            patch.object(manager, "healthy", AsyncMock(return_value=False)),
            patch.object(Path, "is_file", return_value=True),
            patch.object(service, "_available_service_url") as select_port,
            patch.object(service.subprocess, "Popen") as launch,
        ):
            with self.assertRaisesRegex(service.DouyinServiceError, "仍在运行"):
                await manager.ensure_running()
        select_port.assert_not_called()
        launch.assert_not_called()
