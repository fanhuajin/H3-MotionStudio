import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend.app import douyin_job
from backend.douyin_service import DouyinServiceError, DouyinServiceOffline


class DouyinJobStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_active_jobs_are_not_marked_stopped(self):
        for status in ("pending", "running", "success", "failed"):
            job = {"job_id": "test", "status": status}
            with (
                patch("backend.app.douyin_service.job", AsyncMock(return_value=job)),
                patch("backend.app.mirror_upsert"),
                patch("backend.app.douyin_service.result_for", return_value=None),
            ):
                result = await douyin_job("test")
            self.assertEqual(result["status"], "completed" if status == "success" else status)
            self.assertNotIn("error", result)

    async def test_missing_or_offline_live_job_settles_cached_active_job(self):
        for failure in (None, DouyinServiceOffline()):
            with (
                patch("backend.app.douyin_service.job", AsyncMock(return_value=None, side_effect=failure)),
                patch("backend.app.mirror_get_job", return_value={"job_id": "test", "status": "running"}),
                patch("backend.app.douyin_service.result_for", return_value=None),
            ):
                result = await douyin_job("test")
            self.assertEqual(result["status"], "failed")
            self.assertIn("下载进程已停止", result["error"])

    async def test_service_query_error_does_not_claim_process_stopped(self):
        with patch("backend.app.douyin_service.job", AsyncMock(side_effect=DouyinServiceError("query failed"))):
            with self.assertRaises(HTTPException) as raised:
                await douyin_job("test")
        self.assertEqual(raised.exception.status_code, 502)
