"""网页执行适配器回归：替换所有平台输入，不操作桌面。"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from worker import WebQueue, atomic_write
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
        with patch("worker.os.getppid", return_value=-1):
            with self.assertRaisesRegex(RuntimeError, "已停止"):
                self.queue.tick()

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


if __name__ == "__main__":
    unittest.main()
