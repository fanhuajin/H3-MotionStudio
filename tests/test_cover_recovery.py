import asyncio
import base64
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from backend import batch_image, batch_worker as worker, image_workbench as workbench, pipeline


def png():
    result = io.BytesIO()
    Image.new("RGB", (320, 240), (40, 80, 60)).save(result, "PNG")
    return result.getvalue()


class CoverFrameResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_global_pipeline_and_rechecks_shared_frame(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "最终成片.mp4").write_bytes(b"video")
            lock = asyncio.Lock()
            calls = []

            def extract(video, target):
                self.assertTrue(lock.locked())
                calls.append(video)
                target.write_bytes(png())
                return target

            with patch.object(pipeline, "pipeline_lock", lock), patch.object(worker, "extract_cover_person", side_effect=extract):
                await lock.acquire()
                first = asyncio.create_task(worker.ensure_cover_person(folder))
                second = asyncio.create_task(worker.ensure_cover_person(folder))
                await asyncio.sleep(0)
                self.assertEqual(calls, [])
                lock.release()
                results = await asyncio.wait_for(asyncio.gather(first, second), 2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(results, [folder / worker.COVER_PERSON_NAME] * 2)
            self.assertFalse(lock.locked())

    async def test_cancellation_keeps_gpu_locked_until_thread_exits(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "最终成片.mp4").write_bytes(b"video")
            started, release = threading.Event(), threading.Event()
            lock = asyncio.Lock()

            def extract(video, target):
                started.set()
                release.wait(3)
                target.write_bytes(png())

            with patch.object(pipeline, "pipeline_lock", lock), patch.object(worker, "extract_cover_person", side_effect=extract):
                task = asyncio.create_task(worker.ensure_cover_person(folder))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertTrue(lock.locked())
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 2)
                self.assertFalse(lock.locked())
                self.assertFalse((folder / worker.COVER_PERSON_NAME).exists())

    async def test_image_page_refreshes_stale_frame_then_reuses_it(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            video, frame = folder / "最终成片.mp4", folder / worker.COVER_PERSON_NAME
            frame.write_bytes(b"old")
            video.write_bytes(b"new video")
            os.utime(frame, ns=(1_000_000_000, 1_000_000_000))
            os.utime(video, ns=(2_000_000_000, 2_000_000_000))
            self.assertIsNone(worker.cover_person_image(folder))

            def extract(_video, target):
                target.write_bytes(png())

            with patch.object(pipeline, "pipeline_lock", asyncio.Lock()), \
                 patch.object(worker, "extract_cover_person", side_effect=extract) as extract_mock, \
                 patch.object(workbench, "resolve_folder", return_value=folder), \
                 patch.object(workbench, "infer_kind", return_value="dance"), \
                 patch.object(workbench, "_stop_requested", return_value=False), \
                 patch.object(workbench, "_set_auto"), \
                 patch.object(workbench, "_generate_cover", new_callable=AsyncMock) as generate:
                await workbench.run_automatic("i", force=True, stage="bilibili")
                await workbench.run_automatic("i", force=True, stage="douyin")
                extract_mock.assert_called_once()
                self.assertEqual(generate.await_count, 2)
            self.assertEqual(frame.read_bytes(), png())

    async def test_failed_extraction_does_not_publish_partial_or_stale_frame(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            video, frame = folder / "最终成片.mp4", folder / worker.COVER_PERSON_NAME
            frame.write_bytes(b"old")
            video.write_bytes(b"new video")
            os.utime(frame, ns=(1_000_000_000, 1_000_000_000))
            os.utime(video, ns=(2_000_000_000, 2_000_000_000))

            def extract(_video, target):
                target.write_bytes(b"partial")
                raise RuntimeError("failed")

            with patch.object(pipeline, "pipeline_lock", asyncio.Lock()), \
                 patch.object(worker, "extract_cover_person", side_effect=extract), \
                 patch.object(worker.batch_store, "add_item_log"):
                self.assertIsNone(await worker._ensure_video_cover_person("b", "i", folder))
            self.assertEqual(frame.read_bytes(), b"old")
            self.assertIsNone(worker.cover_person_image(folder))


class CoverRequestRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_downloaded_task_record_survives_publish_save_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / worker.COVER_PERSON_NAME).write_bytes(png())

            async def submitted(data, files, candidate):
                batch_image._task_checkpoint(candidate).write_text('{"taskId":"already-paid"}')
                return httpx.Response(200, json={"data": [{
                    "b64_json": base64.b64encode(png()).decode(),
                }]})

            with patch.object(batch_image, "_prepare_route", new_callable=AsyncMock), \
                 patch.object(batch_image, "api_key", return_value="test-only"), \
                 patch.object(batch_image, "_submit_or_resume", side_effect=submitted), \
                 patch.object(batch_image, "_upscale_after_relay", new_callable=AsyncMock) as upscale, \
                 patch.object(workbench, "save_upload", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    await workbench._generate_cover(folder, "bilibili", "dance")
            candidate = folder / ".h3-bilibili-work" / "bilibili.png"
            self.assertEqual(candidate.read_bytes(), png())
            self.assertTrue(batch_image._task_checkpoint(candidate).is_file())
            upscale.assert_not_awaited()

    async def test_failed_poll_resumes_same_task_with_frozen_inputs(self):
        calls = []
        failed = True
        payload = png()

        def handler(request):
            nonlocal failed
            calls.append(request.method)
            if request.method == "POST":
                return httpx.Response(202, json={"task_id": "paid-task"})
            if failed:
                failed = False
                return httpx.Response(400, json={"error": "temporary lookup error"})
            return httpx.Response(200, json={"status": "completed", "data": [{
                "b64_json": base64.b64encode(payload).decode(),
            }]})

        client = httpx.AsyncClient
        def factory(**kwargs):
            return client(transport=httpx.MockTransport(handler), timeout=2)

        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            reference = folder / worker.COVER_PERSON_NAME
            reference.write_bytes(payload)
            with patch.object(batch_image, "_prepare_route", new_callable=AsyncMock), \
                 patch.object(batch_image, "api_key", return_value="test-only"), \
                 patch.object(httpx, "AsyncClient", side_effect=factory), \
                 patch.object(workbench, "prompt", return_value="original prompt") as prompt, \
                 patch.object(worker, "mark_publish_copy_locked"), \
                 patch.object(worker, "resume_failed_item_with_complete_images", return_value=None):
                with self.assertRaises(RuntimeError):
                    await workbench._generate_cover(folder, "bilibili", "dance")
                work = folder / ".h3-bilibili-work"
                self.assertTrue(batch_image._task_checkpoint(work / "bilibili.png").is_file())
                reference.write_bytes(b"changed reference")
                prompt.return_value = "changed prompt"
                await workbench._generate_cover(folder, "bilibili", "dance", allow_uncertain_retry=True)
            self.assertEqual(calls, ["POST", "GET", "GET"])
            self.assertEqual((folder / "B站4x3.png").read_bytes(), payload)
            self.assertFalse(work.exists())
            self.assertFalse((folder / ".h3-bilibili-request-pending").exists())

    async def test_save_failure_and_cancellation_preserve_recoverable_files(self):
        for mode in ("save", "cancel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                folder = Path(raw)
                (folder / worker.COVER_PERSON_NAME).write_bytes(png())
                started = asyncio.Event()

                async def generate(**kwargs):
                    candidate = kwargs["output_path"]
                    batch_image._task_checkpoint(candidate).write_text('{"taskId":"paid"}')
                    candidate.write_bytes(png())
                    if mode == "cancel":
                        started.set()
                        await asyncio.Event().wait()

                with patch.object(batch_image, "generate_candidate_image", side_effect=generate), \
                     patch.object(worker, "mark_publish_copy_locked"), \
                     patch.object(worker, "resume_failed_item_with_complete_images", return_value=None):
                    if mode == "save":
                        with patch.object(workbench, "save_upload", side_effect=OSError("disk error")):
                            with self.assertRaises(OSError):
                                await workbench._generate_cover(folder, "douyin", "dance")
                    else:
                        task = asyncio.create_task(workbench._generate_cover(folder, "douyin", "dance"))
                        await asyncio.wait_for(started.wait(), 2)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    work = folder / ".h3-douyin-work"
                    self.assertTrue(batch_image._task_checkpoint(work / "douyin.png").is_file())
                    with patch.object(batch_image, "generate_candidate_image", new_callable=AsyncMock) as retry:
                        await workbench._generate_cover(folder, "douyin", "dance")
                        retry.assert_not_awaited()
                self.assertEqual((folder / "抖音3x4.png").read_bytes(), png())
                self.assertFalse(work.exists())

    async def test_legacy_unknown_request_never_submits_automatically(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / ".h3-bilibili-request-pending").write_text("unknown")
            with patch.object(batch_image, "generate_candidate_image", new_callable=AsyncMock) as generate:
                with self.assertRaisesRegex(RuntimeError, "避免重复扣费"):
                    await workbench._generate_cover(folder, "bilibili", "dance")
                generate.assert_not_awaited()
