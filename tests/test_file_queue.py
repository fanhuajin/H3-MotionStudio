import asyncio
import gc
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient

from backend import app
from backend import store as store_module
from backend.file_queue import run_file_queue


class FileQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = patch.object(store_module, "DB_PATH", Path(self.temp.name) / "jobs.db")
        self.db.start()
        self.store = store_module.JobStore()
        self.app_store = patch.object(app, "store", self.store)
        self.app_store.start()
        self.spawned = []

    def tearDown(self):
        for coroutine in self.spawned:
            coroutine.close()
        self.app_store.stop()
        self.db.stop()
        gc.collect()
        self.temp.cleanup()

    def state(self, job_id, kind="upscale", **extra):
        return self.store.create({
            "id": job_id, "kind": kind, "status": "queued",
            "createdAt": f"2026-10-23T10:00:{int(job_id):02d}+00:00",
            "updatedAt": "2026-10-23T10:00:00+00:00", **extra,
        })

    async def test_file_queue_is_serial_skips_cancelled_and_continues_after_failure(self):
        self.state("1")
        self.state("2", status="cancelled")
        self.state("3")
        calls = []

        async def runner(job_id):
            calls.append((job_id, "start"))
            await asyncio.sleep(0)
            if job_id == "1":
                raise RuntimeError("转换失败")
            calls.append((job_id, "end"))

        await run_file_queue(["1", "2", "3"], self.store, runner)
        self.assertEqual(calls, [("1", "start"), ("3", "start"), ("3", "end")])
        self.assertEqual(self.store.get("1")["status"], "failed")
        self.assertEqual(self.store.get("2")["status"], "cancelled")

    async def test_cancellation_stops_remaining_files(self):
        self.state("1")
        self.state("2")
        runner = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await run_file_queue(["1", "2"], self.store, runner)
        runner.assert_awaited_once_with("1")

    async def test_all_files_prepared_before_any_runner_starts(self):
        for kind in ("upscale", "rvc", "subtitle"):
            with self.subTest(kind=kind):
                videos = [UploadFile(io.BytesIO(b"video"), filename=f"{i}.mp4") for i in (1, 2)]

                async def prepare(*args, **kwargs):
                    self.assertFalse(kwargs["enqueue"])
                    self.assertEqual(len(self.spawned), 0)
                    return self.state(str(len(self.store.recent()) + 1), kind)

                with patch.object(app, f"_prepare_{kind}_job", side_effect=prepare), \
                        patch.object(app, "_active_subtitle_job", return_value=None), \
                        patch.object(self.store, "active", return_value=None), \
                        patch.object(app, "spawn", side_effect=self.spawned.append):
                    result = await app.create_file_queue(kind, videos, "歌曲")
                self.assertEqual(len(result["jobs"]), 2)
                self.assertEqual(len({row["fileQueueId"] for row in result["jobs"]}), 1)
                self.assertEqual([row["fileQueueIndex"] for row in result["jobs"]], [0, 1])
                self.assertEqual(len(self.spawned), 1)
                self.spawned.pop().close()
                self.assertTrue(all(video.file.closed for video in videos))

    async def test_upload_failure_does_not_leave_queued_or_start_processing(self):
        videos = [UploadFile(io.BytesIO(b"video"), filename=f"{i}.mp4") for i in (1, 2)]
        first = self.state("1")
        with patch.object(app, "_prepare_upscale_job", side_effect=[first, RuntimeError("上传失败")]), \
                patch.object(self.store, "active", return_value=None), \
                patch.object(app, "spawn") as spawn:
            with self.assertRaisesRegex(RuntimeError, "上传失败"):
                await app.create_file_queue("upscale", videos, "")
        spawn.assert_not_called()
        self.assertEqual(self.store.get("1")["status"], "interrupted")
        self.assertTrue(all(video.file.closed for video in videos))

    async def test_invalid_suffix_rejected_before_creating_any_job(self):
        video = UploadFile(io.BytesIO(b"bad"), filename="not-a-video.txt")
        with patch.object(app, "_prepare_upscale_job") as prepare:
            with self.assertRaises(HTTPException) as error:
                await app.create_file_queue("upscale", [video], "")
        self.assertEqual(error.exception.status_code, 400)
        prepare.assert_not_called()
        await video.close()

    async def test_existing_task_rejects_new_queue(self):
        self.state("1")
        video = UploadFile(io.BytesIO(b"video"), filename="one.mp4")
        with self.assertRaises(HTTPException) as error:
            await app.create_file_queue("rvc", [video], "")
        self.assertEqual(error.exception.status_code, 409)
        await video.close()

    async def test_restores_latest_group_in_file_order(self):
        self.state("1", fileQueueId="old", fileQueueIndex=0)
        self.state("2", fileQueueId="new", fileQueueIndex=1)
        self.state("3", fileQueueId="new", fileQueueIndex=0)
        self.state("4", kind="rvc", fileQueueId="other", fileQueueIndex=0)
        self.assertEqual([row["id"] for row in self.store.latest_file_queue("upscale")], ["3", "2"])
        self.assertEqual((await app.get_file_queue("subtitle"))["jobs"], [])

    async def test_running_file_is_the_active_job_not_the_last_queued_file(self):
        self.state("1", status="running", fileQueueId="queue")
        self.state("2", fileQueueId="queue")
        self.assertEqual(self.store.active()["id"], "1")

    async def test_cancel_queued_file_does_not_interrupt_running_file(self):
        self.state("1", status="running", fileQueueId="queue")
        self.state("2", fileQueueId="queue")
        with patch.object(app, "kill_app") as kill:
            result = await app.cancel_job("2")
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["cancelRequested"])
        self.assertEqual(self.store.get("1")["status"], "running")
        kill.assert_not_called()

    async def test_retry_cannot_start_another_subtitle_while_queue_is_running(self):
        self.state("1", kind="subtitle", status="completed", fileQueueId="queue")
        self.state("2", kind="subtitle", status="running", fileQueueId="queue")
        with patch.object(app, "kill_app") as kill, patch.object(app, "spawn") as spawn:
            with self.assertRaises(HTTPException) as error:
                await app.retry_subtitle_job("1")
        self.assertEqual(error.exception.status_code, 409)
        kill.assert_not_called()
        spawn.assert_not_called()

    async def test_multipart_files_use_existing_preparation_without_starting_real_tools(self):
        api = FastAPI()
        api.add_api_route("/queue/{kind}", app.create_file_queue, methods=["POST"])
        for kind in ("upscale", "rvc", "subtitle"):
            with self.subTest(kind=kind), \
                    patch.object(app, "COMFY_INPUT", Path(self.temp.name) / "input"), \
                    patch.object(app, "DATA_DIR", Path(self.temp.name) / "data"), \
                    patch.object(app, "media_metadata", AsyncMock(return_value={"width": 1440, "height": 1080})), \
                    patch.object(app, "gpu_holder", return_value=None), \
                    patch.object(app, "spawn", side_effect=self.spawned.append):
                response = TestClient(api).post(f"/queue/{kind}", files=[
                    ("videos", ("中文视频.mp4", b"first", "video/mp4")),
                    ("videos", ("second.mov", b"second", "video/quicktime")),
                ])
                self.assertEqual(response.status_code, 200, response.text)
                jobs = response.json()["jobs"]
                self.assertEqual([row["sourceName"] for row in jobs], ["中文视频.mp4", "second.mov"])
                for row, expected in zip(jobs, (b"first", b"second")):
                    source = row.get("sourcePath") or row["subtitleSource"]
                    self.assertEqual(Path(source).read_bytes(), expected)
                    self.assertEqual(row["status"], "queued")
                    self.store.update(row["id"], status="completed")
                self.assertEqual(len(self.spawned), 1)
                self.spawned.pop().close()
