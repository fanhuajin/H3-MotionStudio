import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend import app, batch_worker


class BatchZoneTests(unittest.TestCase):
    def test_cross_kind_duplicate_is_removed_from_both_lists(self):
        shared = "https://v.douyin.com/shared"
        state = batch_worker.new_batch_state(
            [shared + "/?from=share", "https://v.douyin.com/song"],
            [shared, "https://v.douyin.com/dance"],
        )
        self.assertEqual(
            [(item["kind"], item["url"]) for item in state["items"]],
            [("singing", "https://v.douyin.com/song"), ("dance", "https://v.douyin.com/dance")],
        )

    def test_append_filters_cross_kind_duplicate_from_both_lists(self):
        state = batch_worker.new_batch_state(["https://v.douyin.com/original"], [])

        class Store:
            def get(self, _batch_id):
                return state

            def mutate(self, _batch_id, change):
                change(state)
                return state

        with patch.object(batch_worker, "batch_store", Store()):
            result = batch_worker.append_batch_items(
                state["id"], ["https://v.douyin.com/shared"], ["https://v.douyin.com/shared/"]
            )
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["duplicates"], 2)
        self.assertEqual(len(state["items"]), 1)

    def test_create_rejects_batch_when_every_link_is_cross_kind_duplicate(self):
        shared = "https://v.douyin.com/shared"
        with patch.object(app.batch_store, "active", return_value=None), patch.object(app.batch_store, "create") as create:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(app.create_batch(app.BatchCreateRequest(singingUrls=[shared], danceUrls=[shared])))
        self.assertEqual(caught.exception.status_code, 400)
        create.assert_not_called()

    def test_new_batch_always_waits_for_manual_schedule(self):
        self.assertFalse(batch_worker.new_batch_state(["https://v.douyin.com/a"], [], auto_render=True)["autoRender"])
        self.assertFalse(batch_worker.new_batch_state(["https://v.douyin.com/a"], [], auto_render=False)["autoRender"])

    def test_prep_blockers_ignore_covers_and_failed_items(self):
        """2026-10-18：封面不再拦出片（出片后才从成片抽帧出封面），失败条目也不挡备料。"""
        row = {"status": "confirmed"}
        with patch.object(batch_worker, "full_auto_enabled", return_value=True), patch.object(
            batch_worker, "_covers_ready", return_value=False
        ):
            self.assertFalse(batch_worker._prep_remaining({"items": [row]}))
        row["status"] = "failed"
        self.assertFalse(batch_worker._prep_remaining({"items": [row]}))
        row["status"] = "awaiting_review"
        self.assertTrue(batch_worker._prep_remaining({"items": [row]}))

    def test_manual_render_refuses_unfinished_prep(self):
        state = {"status": "awaiting_review", "items": [{"status": "pending"}, {"status": "confirmed"}]}
        with patch.object(app, "_batch_or_404", return_value=state):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(app.start_batch_render("batch"))
        self.assertEqual(caught.exception.status_code, 409)

    def test_manual_complete_registers_existing_final_video(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "最终成片.mp4").write_bytes(b"video")
            (folder / "人物图.png").write_bytes(b"person")
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            row = {
                "id": "item", "status": "skipped", "stage": "skipped",
                "milestones": [{"id": "video", "status": "skipped", "progress": 0}],
            }

            class Store:
                def get(self, _batch_id):
                    return {"id": "batch", "items": [row]}

                def mutate_item(self, _batch_id, _item_id, change):
                    change(row)

                def add_item_log(self, *_args, **_kwargs):
                    return None

            with patch.object(batch_worker, "_item", return_value=row), patch.object(
                batch_worker, "_recorded_publish_folder", return_value=folder
            ), patch.object(batch_worker, "batch_store", Store()):
                batch_worker.mark_item_completed("batch", "item")
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["outputs"]["videoFinal"], str(folder / "最终成片.mp4"))
            self.assertIsNone(row["warning"])

    def test_manual_complete_rejects_running_item(self):
        row = {"id": "item", "status": "running"}
        with patch.object(batch_worker, "_item", return_value=row):
            with self.assertRaises(ValueError):
                batch_worker.mark_item_completed("batch", "item")

    def test_output_falls_back_to_publish_folder_after_manual_complete(self):
        """手动标记完成时成片还没落盘 → outputs 没有 videoFinal，之后放进来也要能点开。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "dance" / "026_跳舞_测试"
            folder.mkdir(parents=True)
            video = folder / "最终成片.mp4"
            video.write_bytes(b"video")
            item = {
                "id": "item", "status": "completed", "stage": "completed",
                "kind": "dance", "outputs": {"folder": str(folder)},
            }
            with patch.object(app, "_batch_item_or_404", return_value=item), patch.object(
                app, "BATCH_OUTPUT_ROOT", root
            ), patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                response = asyncio.run(
                    app.batch_item_output("batch", "item", "videoFinal", download=False)
                )
                missing = None
                try:
                    asyncio.run(app.batch_item_output("batch", "item", "copy", download=False))
                except HTTPException as error:
                    missing = error
            self.assertEqual(Path(response.path).resolve(), video.resolve())
            self.assertEqual(missing.status_code, 404)

    def test_publish_output_path_returns_none_without_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "dance" / "027_跳舞_空"
            folder.mkdir(parents=True)
            item = {"outputs": {"folder": str(folder)}}
            with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                self.assertIsNone(batch_worker.publish_output_path(item, "videoFinal"))

    def test_existing_covers_make_no_api_request(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("人物图.png", "发布文案.txt", "B站4x3.png", "抖音3x4.png"):
                (folder / name).write_bytes(b"existing")
            item = {"status": "confirmed"}
            with patch.object(batch_worker, "_item", return_value=item), patch.object(
                batch_worker, "_recorded_publish_folder", return_value=folder
            ), patch.object(batch_worker, "full_auto_enabled", return_value=True), patch.object(
                batch_worker, "_generate_covers_via_api", new_callable=AsyncMock
            ) as generate:
                asyncio.run(batch_worker._finish_prep_covers("batch", "item"))
            generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class WaitingQueueTests(unittest.TestCase):
    def test_runner_only_picks_explicitly_scheduled_items(self):
        waiting = {"id": "waiting", "status": "confirmed", "index": 1}
        queued = {"id": "queued", "status": "confirmed", "index": 2, "renderQueued": True}
        self.assertIsNone(batch_worker._next_confirmed({"items": [waiting]}))
        self.assertEqual(batch_worker._next_confirmed({"items": [waiting, queued]})["id"], "queued")

    def test_schedule_selected_waiting_and_skipped_only(self):
        state = {"status": "completed", "items": [
            {"id": "a", "status": "confirmed"},
            {"id": "b", "status": "skipped"},
            {"id": "c", "status": "confirmed"}]}
        with patch.object(app, "_batch_or_404", return_value=state), \
             patch.object(batch_worker, "_set_item") as mark, \
             patch.object(batch_worker, "restart_item_to_run") as restart, \
             patch.object(app, "deliver_review_materials") as deliver, \
             patch.object(app.batch_store, "update"), \
             patch.object(app, "_resume_batch", return_value=state):
            asyncio.run(app.start_batch_render("batch", app.BatchItemIdsRequest(itemIds=["a", "b"])))
        self.assertEqual([call.args[1] for call in mark.call_args_list], ["a", "b"])
        restart.assert_called_once_with("batch", "b")
        self.assertEqual([call.args[1] for call in deliver.call_args_list], ["a", "b"])

    def test_removed_auto_render_cannot_be_enabled(self):
        with patch.object(app, "_batch_or_404", return_value={}), patch.object(app.batch_store, "update") as update:
            with self.assertRaises(HTTPException):
                asyncio.run(app.set_batch_auto_render("batch", app.BatchAutoRenderRequest(enabled=True)))
        update.assert_not_called()
