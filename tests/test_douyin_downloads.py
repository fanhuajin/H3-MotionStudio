import unittest
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import douyin_downloads
from backend.app import _douyin_housekeeping, _resolve_job, douyin_jobs, douyin_job_payload, douyin_job_media


class DouyinDownloadsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "video.mp4"
        self.source.write_bytes(b"video")
        for name in ("_jobs", "_finished", "tasks"):
            patcher = patch.dict(getattr(douyin_downloads, name), {}, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_direct_success_never_submits_to_service(self):
        job = {"job_id": "direct-test", "url": "https://www.douyin.com/video/123"}
        with (
            patch("backend.douyin_downloads.env_value", return_value=""),
            patch("backend.batch_worker._fetch_source_direct", AsyncMock(return_value=(self.source, "123", {}))) as direct,
            patch("backend.douyin_downloads.douyin_service.submit", AsyncMock()) as service,
        ):
            await douyin_downloads._run(job)
        direct.assert_awaited_once()
        service.assert_not_awaited()
        self.assertEqual(job["status"], "success")

    async def test_direct_failure_falls_back_to_service(self):
        job = {"job_id": "direct-test", "url": "https://www.douyin.com/video/123"}
        with (
            patch("backend.douyin_downloads.env_value", return_value=""),
            patch("backend.batch_worker._fetch_source_direct", AsyncMock(side_effect=RuntimeError("direct failed"))),
            patch("backend.douyin_downloads.douyin_service.submit", AsyncMock(return_value={"status": "success"})) as service,
            patch("backend.douyin_downloads.douyin_service.result_for", return_value={"path": "test.mp4", "awemeId": "123"}),
            patch("backend.batch_worker._park_truncated_source", return_value=None),
            patch("backend.douyin_downloads.ensure_download_playable", AsyncMock(return_value=self.source)),
        ):
            await douyin_downloads._run(job)
        service.assert_awaited_once()
        self.assertEqual(job["status"], "success")

    async def test_both_routes_fail_preserves_reasons(self):
        job = {"job_id": "direct-test", "url": "https://www.douyin.com/video/123"}
        with (
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

    async def test_new_download_stays_in_memory_without_writing_history(self):
        with (
            patch("backend.douyin_downloads.env_value", return_value=""),
            patch("backend.batch_worker._fetch_source_direct", AsyncMock(return_value=(self.source, "123", {}))),
            patch("backend.douyin_mirror.upsert_jobs") as persist,
        ):
            created = douyin_downloads.submit("https://v.douyin.com/short/")
            await douyin_downloads.tasks[created["job_id"]]
        persist.assert_not_called()
        result = douyin_downloads.get_job(created["job_id"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["downloadResult"]["path"], str(self.source.resolve()))

    async def test_polling_and_preview_use_exact_file_without_history_or_codec_probe(self):
        job = {
            "job_id": "direct-current", "url": "https://v.douyin.com/short/", "status": "success",
            "downloadResult": douyin_downloads._file_result(self.source, "123"),
        }
        douyin_downloads._jobs[job["job_id"]] = job
        with (
            patch("backend.app.mirror_get_job") as history,
            patch("backend.app.douyin_service.result_for") as scan,
            patch("backend.app.playable_download_path") as probe,
            patch("backend.app.ensure_download_playable", AsyncMock()) as convert,
        ):
            current = await _resolve_job(job["job_id"])
            payload = douyin_job_payload(current)
            media = await douyin_job_media(job["job_id"], download=False)
        for helper in (history, scan, probe, convert):
            helper.assert_not_called()
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["result"]["playableReady"])
        self.assertEqual(Path(media.path), self.source.resolve())

    async def test_running_task_does_not_scan_old_files(self):
        with patch("backend.app.douyin_service.result_for") as scan:
            payload = douyin_job_payload({"job_id": "direct-current", "status": "running"})
        scan.assert_not_called()
        self.assertIsNone(payload["result"])

    async def test_expired_task_state_is_removed_but_video_is_kept(self):
        douyin_downloads._jobs["old"] = {"job_id": "old", "status": "success"}
        douyin_downloads._finished["old"] = 0
        with patch("backend.douyin_downloads.time.monotonic", return_value=7201):
            self.assertIsNone(douyin_downloads.get_job("old"))
        self.assertTrue(self.source.is_file())

    async def test_idle_watch_does_not_scan_or_convert_old_successful_downloads(self):
        with (
            patch("backend.app.douyin_service.healthy", AsyncMock(return_value=True)),
            patch("backend.app.douyin_service.jobs", AsyncMock(return_value={"jobs": [{"status": "success"}]})),
            patch("backend.app.douyin_service.process", None),
            patch("backend.app.douyin_service.result_for") as scan,
            patch("backend.app.mirror_upsert") as persist,
            patch("backend.app.schedule_download_playable") as convert,
            patch("backend.app.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _douyin_housekeeping()
        for helper in (scan, persist, convert):
            helper.assert_not_called()
