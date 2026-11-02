import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import image_workbench as workbench
from backend import batch_worker


class RetryMissingTests(unittest.IsolatedAsyncioTestCase):
    def test_failure_requires_missing_cover_and_recorded_cover_error(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            folder_id = workbench._folder_id(folder)
            rows = [{"outputs": {"folder": str(folder)}, "bilibiliCoverError": "submit timeout"}]
            with patch.dict(workbench.AUTOMATIONS, {}, clear=True):
                self.assertFalse(workbench._has_failed_covers(folder, []))
                self.assertTrue(workbench._has_failed_covers(folder, rows))
                self.assertFalse(workbench._has_failed_covers(folder, [{
                    "outputs": {"folder": str(folder / "another")}, "bilibiliCoverError": "failed",
                }]))
                workbench.AUTOMATIONS[folder_id] = {"status": "running", "stage": "bilibili"}
                self.assertFalse(workbench._has_failed_covers(folder, rows))
                workbench.AUTOMATIONS[folder_id] = {"status": "failed", "stage": "person"}
                self.assertFalse(workbench._has_failed_covers(folder, []))
                workbench.AUTOMATIONS[folder_id] = {"status": "failed", "stage": "bilibili"}
                self.assertTrue(workbench._has_failed_covers(folder, []))
                (folder / "B站4x3.png").write_bytes(b"completed")
                self.assertFalse(workbench._has_failed_covers(folder, rows))

    async def test_cover_retry_attempts_both_missing_covers_without_generating_person(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(workbench, "resolve_folder", return_value=folder), patch.object(
                workbench, "infer_kind", return_value="dance"
            ), patch.object(workbench, "_set_auto") as status, patch.object(
                workbench, "_stop_requested", return_value=False
            ), patch.object(workbench, "_generate_person", new_callable=AsyncMock) as person, patch.object(
                workbench, "_generate_cover", new_callable=AsyncMock,
                side_effect=[RuntimeError("first failed"), None],
            ) as cover:
                await workbench.run_automatic("retry-test", stage="covers", retry_missing=True)
        person.assert_not_awaited()
        self.assertEqual([call.args[1] for call in cover.await_args_list], ["bilibili", "douyin"])
        self.assertTrue(all(call.kwargs["allow_uncertain_retry"] for call in cover.await_args_list))
        self.assertEqual(status.call_args.kwargs["status"], "failed")
        self.assertIn("first failed", status.call_args.kwargs["message"])

    async def test_bulk_retry_only_queues_missing_ready_covers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = []
            for name in ("missing", "done", "waiting", "running"):
                folder = root / name
                folder.mkdir()
                items.append({"id": name, "kind": "dance", "canCover": name != "waiting",
                              "assets": {"person": {"name": "person"}, "bilibili": {"name": "cover"},
                                         "douyin": {"name": "cover"} if name == "done" else None}})
            with patch.object(workbench, "list_items", return_value=items), patch.object(
                workbench, "resolve_folder", side_effect=lambda folder_id: root / folder_id
            ), patch.dict(workbench.AUTOMATIONS, {"running": {"status": "running"}}, clear=True), patch.object(
                workbench, "start_automatic"
            ) as start:
                result = workbench.retry_incomplete_covers()
            self.assertEqual(result, {"queued": 1, "waiting": 1, "running": 1})
            start.assert_called_once_with("missing", stage="covers", retry_missing=True)

    async def test_cover_retry_keeps_completed_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "B站4x3.png").write_bytes(b"keep cover")
            with patch.object(workbench, "resolve_folder", return_value=folder), patch.object(
                workbench, "infer_kind", return_value="dance"
            ), patch.object(workbench, "_set_auto"), patch.object(
                workbench, "_stop_requested", return_value=False
            ), patch.object(workbench, "_generate_cover", new_callable=AsyncMock) as cover:
                await workbench.run_automatic("retry-test", stage="covers", retry_missing=True)
            cover.assert_awaited_once_with(folder, "douyin", "dance", allow_uncertain_retry=True)
            self.assertEqual((folder / "B站4x3.png").read_bytes(), b"keep cover")

    async def test_cover_retry_is_tracked_until_finished(self):
        with patch.dict(workbench.AUTOMATIONS, {}, clear=True), patch.object(
            workbench, "run_automatic", new_callable=AsyncMock
        ):
            workbench.start_automatic("retry-test", stage="covers", retry_missing=True)
            self.assertTrue(batch_worker.covers_in_flight())
            await asyncio.gather(*batch_worker._BG_COVER_TASKS)
            await asyncio.sleep(0)
            self.assertFalse(batch_worker.covers_in_flight())

    async def test_missing_requests_are_retried_only_when_explicit(self):
        for retry in (False, True):
            for has_person in (False, True):
                with self.subTest(retry=retry, has_person=has_person):
                    with tempfile.TemporaryDirectory() as directory:
                        folder = Path(directory)
                        if has_person:
                            (folder / workbench.PERSON_NAME).write_bytes(b"existing person")
                        existing = folder / workbench.COVER_NAMES["bilibili"]
                        existing.write_bytes(b"existing cover")
                        with patch.object(workbench, "resolve_folder", return_value=folder), patch.object(
                            workbench, "infer_kind", return_value="dance"
                        ), patch.object(workbench, "_set_auto"), patch.object(
                            workbench, "_stop_requested", return_value=False
                        ), patch.object(workbench, "_generate_person", new_callable=AsyncMock, return_value=-1) as person, patch.object(
                            workbench, "_generate_cover", new_callable=AsyncMock
                        ) as cover:
                            await workbench.run_automatic("retry-test", retry_missing=retry)
                        if has_person:
                            person.assert_not_awaited()
                        else:
                            person.assert_awaited_once_with(folder, "dance", allow_uncertain_retry=retry)
                        cover.assert_awaited_once_with(folder, "douyin", "dance", allow_uncertain_retry=retry)
                        self.assertEqual(existing.read_bytes(), b"existing cover")

    async def test_start_passes_retry_intent_and_deduplicates_active_work(self):
        with patch.dict(workbench.AUTOMATIONS, {}, clear=True), patch.object(
            workbench, "run_automatic", new_callable=AsyncMock
        ) as run:
            workbench.start_automatic("retry-test", retry_missing=True)
            workbench.start_automatic("retry-test", retry_missing=True)
            await asyncio.sleep(0)
            run.assert_awaited_once_with("retry-test", force=False, stage=None, retry_missing=True)
