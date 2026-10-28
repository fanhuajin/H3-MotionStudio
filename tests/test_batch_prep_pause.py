import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import app, batch_store, batch_worker


class PrepPauseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(batch_store, "DB_PATH", Path(temporary.name) / "batch.db")
        database.start()
        self.addCleanup(database.stop)
        self.store = batch_store.BatchStore()
        self.store.create({
            "id": "pause-test", "status": "running", "runnerActive": False,
            "createdAt": "2026-10-28T00:00:00", "total": 2,
            "updatedAt": "2026-10-28T00:00:00",
            "items": [
                {"id": "first", "index": 1, "status": "pending", "stage": "queued"},
                {"id": "next", "index": 2, "status": "pending", "stage": "queued"},
            ],
        })

    async def test_pause_survives_reopening_store_and_explicit_resume(self):
        with patch.object(app, "batch_store", self.store):
            paused = await app.pause_batch("pause-test")
        self.assertTrue(paused["pauseRequested"])
        reopened = batch_store.BatchStore()
        reopened.interrupt_active()
        state = reopened.get("pause-test")
        self.assertEqual(state["status"], "paused")
        self.assertEqual([row["status"] for row in state["items"]], ["pending", "pending"])
        spawned = []

        def spawn(coro):
            spawned.append(coro)
            coro.close()

        with (
            patch.object(app, "batch_store", reopened),
            patch.object(batch_worker, "batch_store", reopened),
            patch.object(app, "spawn", side_effect=spawn),
            patch.object(app, "batch_run_alive", return_value=False),
            patch.object(app, "take_over_batch_run"),
            patch.object(app, "_with_publish_chain", side_effect=lambda value: value),
        ):
            app._heal_stalled_batch(state)
            self.assertEqual(spawned, [])
            resumed = await app.resume_batch("pause-test")
        self.assertEqual(len(spawned), 1)
        self.assertEqual(resumed["status"], "running")
        self.assertFalse(resumed["pauseRequested"])

    async def test_pause_before_runner_starts_is_not_overwritten(self):
        self.store.update("pause-test", status="paused", pauseRequested=True)
        with (
            patch.object(batch_worker, "batch_store", self.store),
            patch.object(batch_worker, "_run_prep", AsyncMock()) as prepare,
        ):
            await batch_worker.run_batch("pause-test")
        prepare.assert_not_awaited()
        self.assertEqual(self.store.get("pause-test")["status"], "paused")

    async def test_pause_finishes_current_preparation_without_starting_next(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def prepare(batch_id, item_id):
            self.store.mutate_item(batch_id, item_id, lambda row: row.update(status="running", stage="prepare"))
            started.set()
            await release.wait()
            self.store.mutate_item(batch_id, item_id, lambda row: row.update(status="confirmed", stage="confirmed"))

        with (
            patch.object(batch_worker, "batch_store", self.store),
            patch.object(app, "batch_store", self.store),
            patch.object(batch_worker, "_run_prep", AsyncMock(side_effect=prepare)) as prep,
        ):
            task = asyncio.create_task(batch_worker.run_batch("pause-test"))
            try:
                await asyncio.wait_for(started.wait(), timeout=3)
                await app.pause_batch("pause-test")
                self.assertFalse(task.done())
                release.set()
                await asyncio.wait_for(task, timeout=3)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        prep.assert_awaited_once_with("pause-test", "first")
        state = self.store.get("pause-test")
        self.assertEqual(state["status"], "paused")
        self.assertFalse(state["runnerActive"])
        self.assertEqual([row["status"] for row in state["items"]], ["confirmed", "pending"])
