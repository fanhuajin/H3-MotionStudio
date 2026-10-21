"""发布网页接口隔离回归，不启动桌面进程或访问真实作品。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import publish_service as service


class PublishServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = service.PublishStore(self.root / "state")
        self.patcher = patch.object(service, "publisher", self.store)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        app = FastAPI()
        app.include_router(service.router)
        self.client = TestClient(app)
        self.folder = self.work("001_跳舞_test")

    def work(self, name):
        folder = self.root / name
        folder.mkdir()
        (folder / "最终成片.mp4").write_bytes(b"video")
        (folder / "发布文案.txt").write_text("标题：\n测试\n简介：\n简介\n标签：\n#舞蹈", encoding="utf-8")
        return folder

    def add(self, folder=None):
        result = self.client.post("/api/publish/add", json={"folders": [str(folder or self.folder)]})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["items"][-1]

    def edit(self, item, **changes):
        return self.client.post("/api/publish/edit", json={"ids": [item["id"]], **changes})

    def test_scan_only_lists_works_with_copy(self):
        (self.root / "unrelated").mkdir()
        response = self.client.post("/api/publish/scan", json={"directory": str(self.root)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([i["name"] for i in response.json()["items"]], [self.folder.name])
        self.assertFalse(self.store.path.exists())

    def test_add_deduplicates_and_schedules_next_hour(self):
        first = self.add()
        self.add()
        second = self.add(self.work("002_跳舞_test"))
        self.assertEqual(len(self.store.read()["items"]), 2)
        self.assertEqual(int(second["hour"]), int(first["hour"]) + 1)

    def test_invalid_add_is_atomic(self):
        self.add()
        before = self.store.path.read_bytes()
        response = self.client.post("/api/publish/add", json={"folders": [str(self.work("002_跳舞_test")), str(self.root / "missing")]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_platform_skip_is_independent_and_persisted(self):
        item = self.add()
        response = self.edit(item, platform="douyin", state="不发布")
        self.assertEqual(response.status_code, 200)
        state = service.PublishStore(self.store.directory).read()["items"][0]
        self.assertEqual(state["states"]["douyin"], "不发布")
        self.assertEqual(state["states"]["bilibili"], "待发布")

    def test_invalid_time_does_not_save(self):
        item = self.add()
        before = self.store.path.read_bytes()
        self.assertEqual(self.edit(item, hour="25").status_code, 400)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_completed_status_cannot_be_reset(self):
        item = self.add()
        self.assertEqual(self.edit(item, platform="douyin", complete=True).status_code, 200)
        self.assertEqual(self.edit(item, platform="douyin", state="待发布").status_code, 400)

    def test_import_preserves_submitted_and_recovers_interrupted_states(self):
        source = self.root / "old.json"
        source.write_text(json.dumps({"items": [{"folder": str(self.folder), "states": {
            "douyin": "发布中", "xiaohongshu": "发布中", "bilibili": "已完成"},
            "details": {"douyin": {"submitted": True, "stage": "submit_ready"}}}]}), encoding="utf-8")
        before = source.read_bytes()
        response = self.client.post("/api/publish/import", json={"path": str(source)})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertEqual(item["states"], {"douyin": "待核对", "xiaohongshu": "失败", "bilibili": "已完成"})
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.edit(item, platform="douyin", state="待发布").status_code, 400)
        self.assertEqual(self.edit(item, platform="douyin", review=False).status_code, 200)
        latest = self.store.read()["items"][0]
        self.assertFalse(latest["details"]["douyin"]["submitted"])
        self.assertTrue(latest["details"]["douyin"]["previous_attempt"]["submitted"])

    def test_import_does_not_replace_current_record(self):
        item = self.add()
        self.edit(item, platform="bilibili", complete=True)
        source = self.root / "old.json"
        source.write_text(json.dumps({"items": [{"folder": str(self.folder)}]}), encoding="utf-8")
        response = self.client.post("/api/publish/import", json={"path": str(source)})
        self.assertEqual(response.json()["items"][0]["states"]["bilibili"], "已完成")

    def test_manual_complete_preserves_failure_history(self):
        item = self.add()
        state = self.store.read()
        state["items"][0]["details"]["douyin"] = {"error": "original", "publish_at": None}
        service.write_json(self.store.path, state)
        self.edit(item, platform="douyin", complete=True)
        self.assertEqual(self.store.read()["items"][0]["details"]["douyin"]["previous_attempt"]["error"], "original")

    def test_remove_keeps_files(self):
        item = self.add()
        response = self.client.post("/api/publish/remove", json={"ids": [item["id"]]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.folder / "最终成片.mp4").exists())
        self.assertEqual(self.store.read()["items"], [])

    def test_edits_blocked_while_running(self):
        item = self.add()
        self.store.process = Mock()
        self.store.process.poll.return_value = None
        self.assertEqual(self.edit(item, hour="06").status_code, 400)
        response = self.client.post("/api/publish/control/pause", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.store.control_path.read_text())["command"], "pause")

    def test_stop_cannot_be_overwritten_by_pause_or_resume(self):
        self.add()
        self.store.process = Mock()
        self.store.process.poll.return_value = None
        self.assertEqual(self.client.post("/api/publish/control/stop", json={}).status_code, 200)
        self.assertEqual(self.client.post("/api/publish/control/resume", json={}).status_code, 409)
        self.assertEqual(json.loads(self.store.control_path.read_text())["command"], "stop")

    def test_publish_activity_blocks_batch_shutdown(self):
        from backend import batch_worker
        with patch.object(service, "publishing_in_flight", return_value=True):
            self.assertTrue(batch_worker.work_in_flight())

    def test_corrupt_publish_state_blocks_shutdown(self):
        self.store.directory.mkdir()
        self.store.path.write_text("broken", encoding="utf-8")
        self.assertTrue(service.publishing_in_flight())

    def test_start_uses_isolated_process_and_never_auto_runs_on_get(self):
        self.add()
        with patch.object(service.subprocess, "Popen") as popen, patch.object(service, "dependencies_ready", return_value=True):
            self.client.get("/api/publish")
            popen.assert_not_called()
            popen.return_value.poll.return_value = None
            response = self.client.post("/api/publish/start", json={"platform": "bilibili"})
            self.assertEqual(response.status_code, 200, response.text)
            args = popen.call_args.args[0]
            self.assertIn(str(service.ENGINE / "worker.py"), args)
            self.assertEqual(args[-2:], ["--platform", "bilibili"])
            self.assertEqual(self.client.post("/api/publish/start", json={}).status_code, 409)

    def test_unverified_only_blocks_selected_platform(self):
        self.add()
        state = self.store.read()
        state["items"][0]["states"]["douyin"] = "待核对"
        service.write_json(self.store.path, state)
        with patch.object(service, "dependencies_ready", return_value=True), patch.object(service.subprocess, "Popen") as popen:
            self.assertEqual(self.client.post("/api/publish/start", json={}).status_code, 409)
            popen.assert_not_called()
            self.assertEqual(self.client.post("/api/publish/start", json={"platform": "bilibili"}).status_code, 200)

    def test_interrupted_queue_recovers_safely(self):
        self.add()
        state = self.store.read()
        state["phase"] = "running"
        state["items"][0]["states"]["douyin"] = "发布中"
        state["items"][0]["details"]["douyin"] = {"submitted": True}
        service.write_json(self.store.path, state)
        recovered = self.store.snapshot()
        self.assertEqual(recovered["phase"], "idle")
        self.assertEqual(recovered["items"][0]["states"]["douyin"], "待核对")

    def test_idle_file_with_dangling_publishing_item_also_recovers(self):
        self.add()
        state = self.store.read()
        state["phase"] = "idle"
        state["items"][0]["states"]["douyin"] = "发布中"
        service.write_json(self.store.path, state)
        self.assertEqual(self.store.snapshot()["items"][0]["states"]["douyin"], "失败")

    def test_corrupted_queue_is_not_overwritten(self):
        self.store.directory.mkdir()
        self.store.path.write_text("broken", encoding="utf-8")
        self.assertEqual(self.client.get("/api/publish").status_code, 409)
        self.assertEqual(self.client.post("/api/publish/add", json={"folders": [str(self.folder)]}).status_code, 400)
        self.assertEqual(self.store.path.read_text(), "broken")

    def test_unknown_platform_and_review_rejected(self):
        item = self.add()
        self.assertEqual(self.edit(item, platform="bad", state="不发布").status_code, 400)
        self.assertEqual(self.edit(item, platform="douyin", review=True).status_code, 400)

    def test_copy_asset_is_limited_to_registered_work(self):
        item = self.add()
        response = self.client.get(f"/api/publish/items/{item['id']}/asset/copy")
        self.assertEqual(response.status_code, 200)
        self.assertIn("标题", response.text)
        self.assertEqual(self.client.get(f"/api/publish/items/{item['id']}/asset/other").status_code, 404)
        self.assertEqual(self.client.get("/api/publish/items/unknown/asset/copy").status_code, 404)


if __name__ == "__main__":
    unittest.main()
