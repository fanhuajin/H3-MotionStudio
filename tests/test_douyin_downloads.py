import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import douyin_downloads
from backend.app import _resolve_job, douyin_jobs


class DouyinDownloadsTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_success_never_submits_to_service(self):
        job = {"job_id": "direct-test", "url": "https://www.douyin.com/video/123"}
        with (
            patch("backend.douyin_downloads.upsert_jobs"),
            patch("backend.douyin_downloads.env_value", return_value=""),
            patch("backend.batch_worker._fetch_source_direct", AsyncMock()) as direct,
            patch("backend.douyin_downloads.douyin_service.submit", AsyncMock()) as service,
        ):
            await douyin_downloads._run(job)
        direct.assert_awaited_once()
        service.assert_not_awaited()
        self.assertEqual(job["status"], "success")

    async def test_direct_failure_falls_back_to_service(self):
        job = {"job_id": "direct-test", "url": "https://www.douyin.com/video/123"}
        with (
            patch("backend.douyin_downloads.upsert_jobs"),
            patch("backend.douyin_downloads.env_value", return_value=""),
            patch("backend.batch_worker._fetch_source_direct", AsyncMock(side_effect=RuntimeError("direct failed"))),
            patch("backend.douyin_downloads.douyin_service.submit", AsyncMock(return_value={"status": "success"})) as service,
            patch("backend.douyin_downloads.douyin_service.result_for", return_value={"path": "test.mp4", "awemeId": "123"}),
            patch("backend.batch_worker._park_truncated_source", return_value=None),
            patch("backend.douyin_downloads.ensure_download_playable", AsyncMock(return_value=Path("test.mp4"))),
        ):
            await douyin_downloads._run(job)
        service.assert_awaited_once()
        self.assertEqual(job["status"], "success")

    async def test_both_routes_fail_preserves_reasons(self):
        job = {"job_id": "direct-test", "url": "https://www.douyin.com/video/123"}
        with (
            patch("backend.douyin_downloads.upsert_jobs"),
            patch("backend.douyin_downloads.env_value", return_value=""),
            patch("backend.batch_worker._fetch_source_direct", AsyncMock(side_effect=RuntimeError("direct failed"))),
            patch("backend.douyin_downloads.douyin_service.submit", AsyncMock(return_value={"status": "failed", "error": "service failed"})),
        ):
            await douyin_downloads._run(job)
        self.assertEqual(job["status"], "failed")
        self.assertIn("direct failed", job["error"])
        self.assertIn("service failed", job["error"])

    async def test_live_direct_task_survives_single_and_list_queries(self):
        job = {"job_id": "direct-test", "status": "running"}
        with (
            patch.dict(douyin_downloads.tasks, {"direct-test": object()}),
            patch("backend.app.mirror_get_job", return_value=job),
            patch("backend.app.mirror_jobs", return_value=[job]),
            patch("backend.app.douyin_service.jobs", AsyncMock(return_value={"jobs": []})),
            patch("backend.app.mirror_upsert"),
            patch("backend.app.douyin_service.result_for", return_value=None),
        ):
            self.assertEqual((await _resolve_job("direct-test"))["status"], "running")
            self.assertEqual((await douyin_jobs())["jobs"][0]["status"], "running")
