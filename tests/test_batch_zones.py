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

    def test_new_batch_defaults_to_auto_render(self):
        self.assertTrue(batch_worker.new_batch_state(["https://v.douyin.com/a"], [])["autoRender"])
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
