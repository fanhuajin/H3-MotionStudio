import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend import app, batch_worker


class BatchZoneTests(unittest.TestCase):
    def test_new_batch_defaults_to_auto_render(self):
        self.assertTrue(batch_worker.new_batch_state(["https://v.douyin.com/a"], [])["autoRender"])
        self.assertFalse(batch_worker.new_batch_state(["https://v.douyin.com/a"], [], auto_render=False)["autoRender"])

    def test_prep_blockers_include_failed_and_unfinished_covers(self):
        row = {"status": "failed"}
        with patch.object(batch_worker, "full_auto_enabled", return_value=True), patch.object(
            batch_worker, "_covers_ready", return_value=False
        ):
            self.assertTrue(batch_worker._prep_remaining({"items": [row]}))
            row["status"] = "confirmed"
            self.assertTrue(batch_worker._prep_remaining({"items": [row]}))
            row["status"] = "skipped"
            self.assertFalse(batch_worker._prep_remaining({"items": [row]}))

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
