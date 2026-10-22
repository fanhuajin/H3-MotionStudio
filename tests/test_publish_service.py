"""发布网页接口隔离回归，不启动桌面进程或访问真实作品。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import threading
from types import SimpleNamespace
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

    def test_native_picker_returns_exact_selection_without_adding_unselected_work(self):
        self.work("002_跳舞_not_selected")
        result = SimpleNamespace(returncode=0, stdout=json.dumps({"folders": [str(self.folder)]}), stderr="")
        with patch.object(service.subprocess, "run", return_value=result) as run:
            response = self.client.post("/api/publish/pick", json={"directory": str(self.root)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["folders"], [str(self.folder)])
        self.assertFalse(self.store.path.exists())
        args = run.call_args.args[0]
        self.assertIn(str(service.ENGINE / "folder_picker.py"), args)
        self.assertEqual(args[-2:], ["--directory", str(self.root)])

    def test_cancel_native_picker_does_not_modify_queue(self):
        self.add()
        before = self.store.path.read_bytes()
        result = SimpleNamespace(returncode=0, stdout='{"folders": []}', stderr="")
        with patch.object(service.subprocess, "run", return_value=result):
            response = self.client.post("/api/publish/pick", json={})
        self.assertEqual(response.json()["folders"], [])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_only_one_native_picker_can_be_open(self):
        lock = threading.Lock()
        lock.acquire()
        with patch.object(service, "_PICKER_LOCK", lock), patch.object(service.subprocess, "run") as run:
            response = self.client.post("/api/publish/pick", json={})
        self.assertEqual(response.status_code, 409)
        run.assert_not_called()

    def test_picker_error_releases_guard_for_retry(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr="test picker error")
        with patch.object(service.subprocess, "run", return_value=result):
            response = self.client.post("/api/publish/pick", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("test picker error", response.text)
        self.assertFalse(service._PICKER_LOCK.locked())

    def test_picker_cannot_open_during_publishing(self):
        self.store.process = Mock()
        self.store.process.poll.return_value = None
        with patch.object(service.subprocess, "run") as run:
            response = self.client.post("/api/publish/pick", json={})
        self.assertEqual(response.status_code, 409)
        run.assert_not_called()

    def test_publishing_cannot_start_while_native_picker_is_open(self):
        self.add()
        lock = threading.Lock()
        lock.acquire()
        with patch.object(service, "_PICKER_LOCK", lock), patch.object(service.subprocess, "Popen") as popen:
            response = self.client.post("/api/publish/start", json={})
        self.assertEqual(response.status_code, 409)
        popen.assert_not_called()

    def test_invalid_picker_response_does_not_change_queue(self):
        result = SimpleNamespace(returncode=0, stdout='{"folders": "bad"}', stderr="")
        with patch.object(service.subprocess, "run", return_value=result):
            response = self.client.post("/api/publish/pick", json={})
        self.assertEqual(response.status_code, 400)
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

    def test_platform_choices_survive_reload_and_other_item_edits(self):
        first = self.add()
        second = self.add(self.work("002_跳舞_test"))
        self.assertTrue(all(state == "待发布" for state in first["states"].values()))
        for platform in service.PLATFORMS:
            self.assertEqual(self.edit(first, platform=platform, state="不发布").status_code, 200)
        self.edit(second, hour="09")
        rows = self.client.get("/api/publish").json()["items"]
        self.assertTrue(all(state == "不发布" for state in rows[0]["states"].values()))
        self.assertTrue(all(state == "待发布" for state in rows[1]["states"].values()))
        self.edit(first, platform="bilibili", state="待发布")
        states = service.PublishStore(self.store.directory).read()["items"][0]["states"]
        self.assertEqual(states, {"douyin": "不发布", "xiaohongshu": "不发布", "bilibili": "待发布"})

    def test_time_fields_can_be_saved_independently(self):
        item = self.add()
        self.edit(item, scheduled=False)
        changed = self.edit(item, date="2027-01-02").json()["items"][0]
        self.assertEqual(changed["date"], "2027-01-02")
        self.assertEqual(changed["hour"], item["hour"])
        self.assertFalse(changed["scheduled"])
        changed = self.edit(item, hour="09").json()["items"][0]
        self.assertEqual(changed["date"], "2027-01-02")
        self.assertEqual(changed["hour"], "09")
        self.assertFalse(changed["scheduled"])
        self.assertEqual(changed["states"], item["states"])

    def test_bulk_time_edits_preserve_each_omitted_field(self):
        first = self.add()
        second = self.add(self.work("002_跳舞_test"))
        self.edit(first, date="2027-01-01", hour="03", scheduled=True)
        self.edit(second, date="2027-01-02", hour="08", scheduled=False)
        ids = [first["id"], second["id"]]
        def update(**changes):
            response = self.client.post("/api/publish/edit", json={"ids": ids, **changes})
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()["items"]
        rows = update(date="2027-02-03")
        self.assertEqual([r["hour"] for r in rows], ["03", "08"])
        self.assertEqual([r["scheduled"] for r in rows], [True, False])
        self.edit(second, date="2027-02-04")
        rows = update(hour="12")
        self.assertEqual([r["date"] for r in rows], ["2027-02-03", "2027-02-04"])
        self.assertEqual([r["scheduled"] for r in rows], [True, False])
        rows = update(date="2027-03-05", hour="14")
        self.assertTrue(all(r["date"] == "2027-03-05" and r["hour"] == "14" for r in rows))
        self.assertEqual([r["scheduled"] for r in rows], [True, False])
        rows = update(scheduled=False)
        self.assertTrue(all(r["date"] == "2027-03-05" and r["hour"] == "14" and not r["scheduled"] for r in rows))

    def test_bulk_date_schedules_selected_items_without_reassigning_hours(self):
        first = self.add()
        second = self.add(self.work("002_跳舞_test"))
        untouched = self.add(self.work("003_跳舞_test"))
        self.edit(first, hour="03", scheduled=False)
        self.edit(second, hour="08", scheduled=True)
        self.edit(untouched, scheduled=False)
        before = self.store.read()["items"][2]
        response = self.client.post("/api/publish/edit", json={
            "ids": [first["id"], second["id"]], "date": "2027-04-06", "scheduled": True,
        })
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()["items"]
        self.assertEqual([row["hour"] for row in rows[:2]], ["03", "08"])
        self.assertTrue(all(row["date"] == "2027-04-06" and row["scheduled"] for row in rows[:2]))
        self.assertEqual(self.store.read()["items"][2], before)

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

    def test_stop_fallback_terminates_only_matching_worker_and_preserves_submission(self):
        item = self.add()
        state = self.store.read()
        state.update(phase="running", workerPid=123)
        state["items"][0]["states"]["douyin"] = "发布中"
        state["items"][0]["details"]["douyin"] = {"submitted": True}
        service.write_json(self.store.path, state)
        service.write_json(self.store.control_path, {"command": "stop", "id": "stop-one"})
        process = Mock()
        process.cmdline.return_value = [str(service.ENGINE / "worker.py"), str(self.store.path)]
        with patch.object(service.time, "sleep"), patch.object(self.store, "active", side_effect=[True, False]), patch.object(service.psutil, "Process", return_value=process):
            self.store.finish_stop("stop-one")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        saved = self.store.read()
        self.assertEqual(saved["phase"], "idle")
        self.assertEqual(saved["items"][0]["states"]["douyin"], "待核对")

    def test_stop_fallback_never_terminates_unrelated_process(self):
        self.add()
        state = self.store.read()
        state["workerPid"] = 123
        service.write_json(self.store.path, state)
        service.write_json(self.store.control_path, {"command": "stop", "id": "stop-one"})
        process = Mock()
        process.cmdline.return_value = ["unrelated.py"]
        with patch.object(service.time, "sleep"), patch.object(self.store, "active", return_value=True), patch.object(service.psutil, "Process", return_value=process):
            self.store.finish_stop("stop-one")
        process.terminate.assert_not_called()

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
