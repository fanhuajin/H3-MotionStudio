import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend import batch_worker as worker


class ReusedAutoConfirmTests(unittest.TestCase):
    def test_reuse_reaches_auto_confirmation_without_new_generation(self):
        with patch.object(worker, "_item", return_value={"sourcePath": "unused.mp4", "kind": "dance"}), patch.object(worker, "_set_item"), patch.object(worker.batch_store, "set_item_milestone"), patch.object(worker.batch_store, "add_item_log"), patch.object(worker, "_video_duration_seconds", return_value=20), patch.object(worker, "_adopt_previous_work", return_value=True), patch.object(worker, "_auto_confirm_prepared_item") as confirm:
            asyncio.run(worker._prepare_review_work("batch", "item"))
            confirm.assert_called_once_with("batch", "item")

    def test_auto_confirmation_requires_existing_image_and_review_state(self):
        with tempfile.TemporaryDirectory() as raw:
            image = Path(raw) / "person.png"
            image.write_bytes(b"existing")
            item = {"status": "awaiting_review", "ai": {"reference_image_path": str(image)}}
            with patch.object(worker, "_cgi_auto_confirm", return_value=True), patch.object(worker, "_item", return_value=item), patch.object(worker, "confirm_batch_items", return_value={"confirmed": ["item"]}) as confirm, patch.object(worker.batch_store, "add_item_log"):
                worker._auto_confirm_prepared_item("batch", "item")
                confirm.assert_called_once()
                confirm.reset_mock()
                item["status"] = "failed"
                worker._auto_confirm_prepared_item("batch", "item")
                confirm.assert_not_called()
                item["status"] = "awaiting_review"
                image.unlink()
                worker._auto_confirm_prepared_item("batch", "item")
                confirm.assert_not_called()

    def test_manual_mode_keeps_review(self):
        with patch.object(worker, "_cgi_auto_confirm", return_value=False), patch.object(worker, "confirm_batch_items") as confirm:
            worker._auto_confirm_prepared_item("batch", "item")
            confirm.assert_not_called()
