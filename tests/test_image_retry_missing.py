import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import image_workbench as workbench


class RetryMissingTests(unittest.IsolatedAsyncioTestCase):
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
