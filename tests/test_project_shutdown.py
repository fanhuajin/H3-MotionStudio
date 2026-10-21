"""项目关闭的隔离测试；不向真实进程发送挂起或终止信号。"""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import threading
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend import project_shutdown as shutdown


class ProjectShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.app = FastAPI()
        self.app.include_router(shutdown.router)
        self.client = TestClient(self.app, client=("127.0.0.1", 5000))
        for target, value in (("DATA_DIR", self.folder), ("_HELPER", None), ("_CLOSING", threading.Event())):
            patcher = patch.object(shutdown, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def process(self, pid, args, cwd=None):
        result = Mock(pid=pid)
        result.cmdline.return_value = args
        result.cwd.return_value = str(cwd or shutdown.PROJECT_ROOT)
        result.create_time.return_value = 123.5
        return result

    def test_target_stops_at_project_launcher_and_excludes_user_shell(self):
        current = self.process(10, ["python", "-m", "uvicorn", "backend.app:app"])
        wrapper = self.process(9, ["python", "-m", "uvicorn", "backend.app:app"])
        launcher = self.process(8, ["powershell.exe", "-File", str(shutdown.PROJECT_ROOT / "scripts/start-local.ps1")])
        shell = self.process(7, ["powershell.exe"])
        current.parents.return_value = [wrapper, launcher, shell]
        with patch.object(shutdown.psutil, "Process", return_value=current):
            self.assertEqual(shutdown.shutdown_target(), (8, 123.5))

    def test_non_project_server_is_rejected(self):
        current = self.process(10, ["python", "-m", "uvicorn", "backend.app:app"], self.folder)
        current.parents.return_value = []
        with patch.object(shutdown.psutil, "Process", return_value=current):
            with self.assertRaises(ValueError):
                shutdown.shutdown_target()

    def test_generic_python_or_terminal_is_not_a_project_launcher(self):
        for args in (["python.exe", "other.py"], ["powershell.exe"], ["cmd.exe", "/c", "echo H3-MotionStudio"]):
            self.assertFalse(shutdown.is_project_process(self.process(1, args), shutdown.PROJECT_ROOT))

    def test_request_spawns_only_deferred_helper_not_immediate_kill(self):
        with patch.object(shutdown, "shutdown_target", return_value=(10, 123.5)), \
             patch.object(shutdown.subprocess, "Popen") as popen, \
             patch("backend.batch_worker.cancel_pending_shutdown", return_value={"pending": False}) as cancel:
            response = self.client.post("/api/system/close-project", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["accepted"])
        command = popen.call_args.args[0]
        self.assertIn(str(shutdown.PROJECT_ROOT / "scripts/stop-project.py"), command)
        self.assertEqual(command[command.index("--pid") + 1], "10")
        cancel.assert_called_once()

    def test_duplicate_click_does_not_spawn_second_helper(self):
        helper = Mock()
        helper.poll.return_value = None
        with patch.object(shutdown, "_HELPER", helper), patch.object(shutdown.subprocess, "Popen") as popen:
            self.assertEqual(self.client.post("/api/system/close-project", json={}).status_code, 200)
        popen.assert_not_called()

    def test_remote_client_cannot_close_project(self):
        client = TestClient(self.app, client=("192.0.2.10", 5000))
        with patch.object(shutdown.subprocess, "Popen") as popen:
            self.assertEqual(client.post("/api/system/close-project", json={}).status_code, 403)
        popen.assert_not_called()

    def test_cross_site_origin_cannot_close_project(self):
        with patch.object(shutdown.subprocess, "Popen") as popen:
            response = self.client.post("/api/system/close-project", json={}, headers={"Origin": "https://other.example"})
        self.assertEqual(response.status_code, 403)
        popen.assert_not_called()

    def test_unrecognized_service_does_not_start_helper(self):
        with patch.object(shutdown, "shutdown_target", side_effect=ValueError("unrecognized")), \
             patch.object(shutdown.subprocess, "Popen") as popen:
            self.assertEqual(self.client.post("/api/system/close-project", json={}).status_code, 409)
        popen.assert_not_called()

    def test_pending_machine_shutdown_is_cleared(self):
        with patch.object(shutdown, "shutdown_target", return_value=(10, 123.5)), \
             patch.object(shutdown.subprocess, "Popen"), \
             patch("backend.batch_worker.cancel_pending_shutdown", return_value={"batchId": "batch"}), \
             patch("backend.batch_store.batch_store.update") as update:
            self.assertEqual(self.client.post("/api/system/close-project", json={}).status_code, 200)
        update.assert_called_once_with("batch", shutdownOnComplete=False, shutdownAfterPrep=False)

    def test_automatic_machine_shutdown_cannot_be_rearmed_while_closing(self):
        from backend import batch_worker
        shutdown._CLOSING.set()
        with patch.object(batch_worker, "launch_shutdown") as launch:
            self.assertEqual(batch_worker.shutdown_tick(), "waiting")
        launch.assert_not_called()

    def helper(self):
        path = shutdown.PROJECT_ROOT / "scripts/stop-project.py"
        spec = importlib.util.spec_from_file_location("stop_project_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_helper_excludes_itself_and_terminates_children_before_launcher(self):
        helper = self.helper()
        root = self.process(10, ["python", "backend.app:app"])
        child = self.process(11, ["comfy"])
        worker = self.process(12, ["publisher"])
        root.children.return_value = [child, worker, Mock(pid=99)]
        child.children.return_value = []
        worker.children.return_value = []
        order = []
        for process in (root, child, worker):
            process.kill.side_effect = lambda p=process: order.append(p.pid)
        with patch.object(helper.psutil, "Process", return_value=root), patch.object(helper.os, "getpid", return_value=99):
            helper.stop_tree(10, 123.5, shutdown.PROJECT_ROOT)
        self.assertEqual(order, [12, 11, 10])
        for process in (root, child, worker):
            process.suspend.assert_called_once()

    def test_helper_rejects_reused_pid(self):
        helper = self.helper()
        root = self.process(10, ["python", "backend.app:app"])
        root.create_time.return_value = 999
        with patch.object(helper.psutil, "Process", return_value=root):
            with self.assertRaises(RuntimeError):
                helper.stop_tree(10, 123.5, shutdown.PROJECT_ROOT)
        root.kill.assert_not_called()
        root.suspend.assert_not_called()

    def test_helper_resumes_survivors_if_termination_fails(self):
        helper = self.helper()
        root = self.process(10, ["python", "backend.app:app"])
        root.children.return_value = []
        root.kill.side_effect = shutdown.psutil.AccessDenied(10)
        with patch.object(helper.psutil, "Process", return_value=root):
            with self.assertRaises(shutdown.psutil.AccessDenied):
                helper.stop_tree(10, 123.5, shutdown.PROJECT_ROOT)
        root.resume.assert_called_once()


if __name__ == "__main__":
    unittest.main()
