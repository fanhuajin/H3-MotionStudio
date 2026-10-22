import asyncio
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from backend import app, batch_store as store_module, batch_worker as worker


def state(batch_id, created="2026-10-19T00:00:00"):
    return {"id": batch_id, "createdAt": created, "updatedAt": created, "status": "paused", "total": 2,
            "items": [{"id": "keep", "status": "confirmed", "index": 1, "kindIndex": 40, "ai": {"title": "保留任务"}},
                      {"id": "gone", "status": "deleted", "index": 2, "logs": [{"message": "old" * 10000}]}]}


class BatchRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(store_module, "DB_PATH", Path(self.tmp.name) / "state.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = store_module.BatchStore()

    def test_only_latest_and_undeleted_items_remain_on_disk(self):
        self.store.create(state("old"))
        latest = state("new", "2026-10-19T01:00:00")
        self.store.create(latest)
        before = self.store.get("new")["items"][0]
        self.assertEqual(self.store.prune_history("new"), {"batches": 1, "items": 1})
        self.assertIsNone(self.store.get("old"))
        remaining = self.store.get("new")
        self.assertEqual(remaining["items"], [before])
        self.assertEqual(remaining["total"], 1)
        self.assertEqual(remaining["deletedCount"], 0)
        self.assertEqual(self.store.prune_history("new"), {"batches": 0, "items": 0})

    def test_last_deleted_item_removes_batch_and_emits_empty_state(self):
        self.store.create(state("new"))
        queue = self.store.subscribe("new")
        self.store.mutate_item("new", "keep", lambda row: row.update(status="deleted"))
        self.store.prune_history("new")
        self.assertIsNone(self.store.latest())
        self.assertIsNone(self.store.get("new"))
        self.assertEqual(queue.get_nowait()["items"], [])

    def test_protected_pending_delivery_is_not_removed_until_released(self):
        self.store.create(state("new"))
        self.assertEqual(self.store.prune_history("new", {"gone"})["items"], 0)
        self.assertEqual(len(self.store.get("new")["items"]), 2)
        self.assertEqual(self.store.prune_history("new")["items"], 1)

    def test_stale_cleanup_cannot_remove_newer_batch(self):
        self.store.create(state("old"))
        self.store.create(state("new", "2026-10-19T01:00:00"))
        self.store.prune_history("old")
        self.assertIsNotNone(self.store.get("old"))
        self.assertEqual(self.store.latest()["id"], "new")

    def test_subscriber_only_gets_latest_visible_detached_snapshot(self):
        self.store.create(state("new"))
        queue = self.store.subscribe("new")
        for count in range(4):
            returned = self.store.update("new", notice=str(count))
        self.assertEqual(queue.qsize(), 1)
        result = queue.get_nowait()
        self.assertEqual(result["notice"], "3")
        self.assertEqual([row["id"] for row in result["items"]], ["keep"])
        returned["items"][0]["status"] = "failed"
        self.assertEqual(result["items"][0]["status"], "confirmed")
        self.assertEqual(self.store.get("new")["items"][0]["status"], "confirmed")

    def test_mutations_do_not_lose_parallel_updates(self):
        self.store.create(state("new"))
        def increment():
            for _ in range(10):
                self.store.mutate("new", lambda value: value.update(counter=value.get("counter", 0) + 1))
        threads = [threading.Thread(target=increment) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(self.store.get("new")["counter"], 20)

    def test_cleanup_waits_for_real_child_cancellation(self):
        saved = state("new")
        saved["items"][1]["videoJobId"] = "live-job"
        self.store.create(saved)
        with patch.object(worker.store, "get", return_value={"status": "cancelling"}):
            worker.cleanup_batch_history(self.store)
        self.assertEqual(len(self.store.get("new")["items"]), 2)
        with patch.object(worker.store, "get", return_value={"status": "cancelled"}):
            worker.cleanup_batch_history(self.store)
        self.assertEqual(len(self.store.get("new")["items"]), 1)

    def test_live_callback_defers_record_cleanup(self):
        value = state("new")
        value["currentItemId"] = "gone"
        self.store.create(value)
        async def scenario():
            pending = asyncio.create_task(asyncio.sleep(10))
            worker._SALVAGE_TASKS.add(pending)
            try:
                worker.cleanup_batch_history(self.store)
                self.assertEqual(len(self.store.get("new")["items"]), 2)
            finally:
                pending.cancel()
                worker._SALVAGE_TASKS.discard(pending)
                try: await pending
                except asyncio.CancelledError: pass
        asyncio.run(scenario())

    def test_other_video_does_not_retain_deleted_idle_item(self):
        value = state("new")
        value["currentItemId"] = "keep"
        self.store.create(value)
        with patch.object(worker, "_RUNNING_BATCHES", {"new"}):
            worker.cleanup_batch_history(self.store)
        self.assertEqual([row["id"] for row in self.store.get("new")["items"]], ["keep"])

    def test_api_projection_never_contains_deleted_history(self):
        saved = state("new")
        with patch.object(worker, "_prep_assets_ready", return_value=True), patch.object(worker, "publish_chain", return_value={}):
            outgoing = app._with_publish_chain(saved)
        self.assertEqual([row["id"] for row in outgoing["items"]], ["keep"])
        self.assertEqual(len(saved["items"]), 2)
