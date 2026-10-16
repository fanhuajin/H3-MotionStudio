import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as api, batch_worker, batch_store as store_module
from backend.batch_store import BatchStore


class ManualRetryImageTests(unittest.TestCase):
    def test_manual_endpoints_archive_unknown_requests_for_all_retryable_statuses(self):
        for many in (False, True):
            for status in ("failed", "skipped", "completed"):
                with self.subTest(many=many, status=status), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                    root = Path(directory)
                    with patch.object(store_module, "DB_PATH", root / "queue.db"):
                        store = BatchStore()
                        state = batch_worker.new_batch_state(["https://v.douyin.com/test"], [])
                        item = state["items"][0]
                        item["status"] = status
                        store.create(state)
                        work = root / "batches" / state["id"] / item["id"]
                        work.mkdir(parents=True)
                        marker = work / "candidate_r0_attempt1.png.request-pending"
                        marker.write_text("submitted", encoding="utf-8")
                        with patch.object(api, "batch_store", store), patch.object(batch_worker, "batch_store", store), patch.object(batch_worker, "DATA_DIR", root), patch.object(batch_worker, "_adopt_published_image_for_restart", return_value=None), patch.object(batch_worker, "_sync_publish_images"), patch.object(api, "_resume_batch", side_effect=lambda *args: store.get(state["id"])):
                            if status != "failed":
                                with self.assertRaises(ValueError):
                                    batch_worker.restart_item_to_run(state["id"], item["id"])
                                self.assertTrue(marker.exists())
                            if many:
                                request = api.BatchItemIdsRequest(itemIds=[item["id"]])
                                asyncio.run(api.retry_batch_items_endpoint(state["id"], request))
                            else:
                                asyncio.run(api.retry_batch_item(state["id"], item["id"]))
                        row = store.get(state["id"])["items"][0]
                        self.assertEqual(row["status"], "pending")
                        self.assertTrue(row["retryMissingCovers"])
                        self.assertFalse(marker.exists())
                        archived = list(work.glob("*.previous-unknown-*"))
                        self.assertEqual(len(archived), 1)
                        self.assertEqual(archived[0].read_text(encoding="utf-8"), "submitted")
