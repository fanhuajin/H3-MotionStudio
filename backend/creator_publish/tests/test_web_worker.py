"""网页执行适配器回归：替换所有平台输入，不操作桌面。"""
import json
import os
import sys
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from worker import WebQueue, atomic_write, run_queue
from multi_publish import PLATFORMS


class WebWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "queue.json"
        self.video = self.root / "最终成片.mp4"
        self.cover = self.root / "cover.png"
        self.video.write_bytes(b"video")
        self.cover.write_bytes(b"cover")
        atomic_write(self.path, {"items": [{"id": "one", "folder": str(self.root),
            "date": "2026-10-22", "hour": "10", "scheduled": False,
            "states": {p: "待发布" for p in PLATFORMS}, "details": {}}], "ownerPid": os.getppid()})
        self.queue = WebQueue(self.path)

    def draft(self, submitted=False, failure=False):
        draft = Mock()
        def run(*args):
            draft.on_stage("upload_ready")
            if submitted:
                draft.on_submit()
            if failure:
                raise RuntimeError("offline failure")
        draft.run.side_effect = run
        draft.actual_publish_at = "2026-10-22 10:17"
        adapter = (Mock(return_value=draft), Mock(return_value=(self.video, "title", "body", ["tag"])),
                   Mock(return_value=self.cover), Mock(return_value="舞蹈"), Mock(return_value=None))
        self.queue.adapters["douyin"] = adapter
        return draft

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_success_updates_only_target_platform_and_records_actual_time(self):
        self.draft(submitted=True)
        self.queue.run_platform("one", self.queue.items["one"], "douyin")
        item = self.saved()["items"][0]
        self.assertEqual(item["states"]["douyin"], "已完成")
        self.assertEqual(item["states"]["bilibili"], "待发布")
        self.assertEqual(item["details"]["douyin"]["publish_at"], "2026-10-22 10:17")

    def test_error_after_submit_requires_review(self):
        self.draft(submitted=True, failure=True)
        with self.assertRaisesRegex(RuntimeError, "offline failure"):
            self.queue.run_platform("one", self.queue.items["one"], "douyin")
        self.assertEqual(self.saved()["items"][0]["states"]["douyin"], "待核对")

    def test_error_before_submit_can_retry(self):
        self.draft(failure=True)
        with self.assertRaises(RuntimeError):
            self.queue.run_platform("one", self.queue.items["one"], "douyin")
        self.assertEqual(self.saved()["items"][0]["states"]["douyin"], "失败")

    def test_previously_submitted_work_is_never_sent_again(self):
        draft = self.draft()
        self.queue.items["one"]["details"]["douyin"] = {"submitted": True}
        with self.assertRaises(RuntimeError):
            self.queue.run_platform("one", self.queue.items["one"], "douyin")
        draft.run.assert_not_called()
        self.assertEqual(self.saved()["items"][0]["states"]["douyin"], "待核对")

    def test_control_stop_is_checked_before_desktop_actions(self):
        atomic_write(self.queue.control_file, {"command": "stop"})
        with self.assertRaisesRegex(RuntimeError, "已停止"):
            self.queue.tick()

    def test_parent_exit_stops_worker(self):
        with patch("worker.psutil.Process") as process:
            process.return_value.parents.return_value = []
            with self.assertRaisesRegex(RuntimeError, "已停止"):
                self.queue.tick()

    def test_windows_venv_launcher_keeps_backend_as_live_ancestor(self):
        with patch("worker.psutil.Process") as process:
            process.return_value.parents.return_value = [Mock(pid=-1), Mock(pid=self.queue.state["ownerPid"])]
            self.queue.tick()

    def test_repeated_desktop_checks_reuse_verified_owner_and_detect_exit(self):
        owner = Mock(pid=self.queue.state["ownerPid"])
        owner.is_running.side_effect = [True, True, False]
        with patch("worker.psutil.Process") as process:
            process.return_value.parents.return_value = [Mock(pid=-1), owner]
            self.queue.tick()
            self.queue.tick()
            with self.assertRaisesRegex(RuntimeError, "已停止"):
                self.queue.tick()
            process.return_value.parents.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32", "Windows venv launcher regression")
    def test_real_python_child_accepts_backend_through_venv_launcher(self):
        state = self.saved()
        state["ownerPid"] = os.getpid()
        atomic_write(self.path, state)
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "from pathlib import Path; import sys; from worker import WebQueue; WebQueue(Path(sys.argv[1])).tick()",
             str(self.path)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_backend_exit_also_stops_paused_worker(self):
        with patch.object(self.queue, "command", return_value="pause"), \
             patch("worker.psutil.Process") as process:
            process.return_value.parents.return_value = []
            with self.assertRaisesRegex(RuntimeError, "已停止"):
                self.queue.pause()

    def test_web_pause_waits_and_returns_focus_on_resume(self):
        draft = Mock(paused_seconds=0.)
        self.queue.active_draft = draft
        with patch.object(self.queue, "command", side_effect=["pause", "pause", "pause", "pause", "resume"]), patch("worker.time.sleep"):
            self.queue.tick()
        self.assertEqual(self.saved()["phase"], "running")
        draft.window.SetFocus.assert_called_once()
        self.assertGreaterEqual(draft.paused_seconds, 0)

    def test_fast_web_resume_is_not_discarded_as_stale_escape_command(self):
        self.queue.web_pause = True
        with patch.object(self.queue, "command", return_value="resume"):
            self.queue.pause()
        self.assertEqual(self.saved()["phase"], "running")

    def test_stopping_paused_work_preserves_stop(self):
        with patch.object(self.queue, "command", return_value="stop"):
            with self.assertRaisesRegex(RuntimeError, "已停止"):
                self.queue.pause()

    def run_harness(self, prepare_error=False, skipped=(), platforms=("douyin",)):
        queue = Mock()
        queue.state = {}
        queue.items = {"one": {"folder": self.root, "scheduled": False,
                                "states": {p: "不发布" if p in skipped else "待发布" for p in PLATFORMS}}}
        queue.adapters = {p: (type("OfflineDraft", (), {}), Mock(), Mock(), Mock(), Mock()) for p in PLATFORMS}
        events = []
        queue.run_platform.side_effect = lambda *args: events.append("publish")
        kernel = Mock()
        kernel.CreateMutexW.return_value = 1
        modules = {"windows_mcp": Mock(), "windows_mcp.uia": Mock(), "keyboard_session": Mock()}
        with patch.dict(sys.modules, modules), patch("worker.WebQueue", return_value=queue), \
             patch("worker.ctypes.WinDLL", return_value=kernel), patch("worker.ctypes.get_last_error", return_value=0), \
             patch("worker.BilibiliDraft"), patch("worker.PlatformPages") as pages:
            def ensure():
                events.append("pages_ready")
                if prepare_error:
                    raise RuntimeError("页面未就绪")
            pages.return_value.ensure.side_effect = ensure
            # 跳过外层真实桌面互斥，所有平台输入均是桩。
            run_queue.__wrapped__(self.path, platforms)
        return events, queue

    def test_pages_are_prepared_before_first_platform_execution(self):
        events, queue = self.run_harness()
        self.assertEqual(events, ["pages_ready", "publish"])
        self.assertEqual(queue.state["phase"], "idle")

    def test_page_preparation_failure_never_enters_platform_execution(self):
        events, queue = self.run_harness(prepare_error=True)
        self.assertEqual(events, ["pages_ready"])
        queue.run_platform.assert_not_called()

    def test_unchecked_platforms_skip_asset_preparation_and_publishing(self):
        for skipped in (("douyin",), ("bilibili", "xiaohongshu"), PLATFORMS):
            with self.subTest(skipped=skipped):
                _, queue = self.run_harness(skipped=skipped, platforms=PLATFORMS)
                self.assertEqual([call.args[2] for call in queue.run_platform.call_args_list],
                                 [p for p in PLATFORMS if p not in skipped])
                for p in skipped:
                    for prepare in queue.adapters[p][1:]:
                        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
