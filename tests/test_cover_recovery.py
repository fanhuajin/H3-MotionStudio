import asyncio
import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from backend import batch_image, batch_worker as worker, image_workbench as workbench


def png():
    result = io.BytesIO()
    Image.new("RGB", (320, 240), (40, 80, 60)).save(result, "PNG")
    return result.getvalue()


class CoverRequestRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        judge = patch.object(workbench.batch_ai, "judge_cover_hands", new_callable=AsyncMock,
                             return_value={"status": "reasonable", "reason": "结构自然", "fingersCutByFrame": "no"})
        judge.start()
        self.addCleanup(judge.stop)

    async def test_terminal_failure_retries_once_with_fallback_for_each_ratio(self):
        for stage, ratio in (("bilibili", "4:3 横版"), ("douyin", "3:4 竖版")):
            for retry_succeeds in (True, False):
                with self.subTest(stage=stage, success=retry_succeeds), tempfile.TemporaryDirectory() as raw:
                    folder = Path(raw)
                    (folder / "最终成片.mp4").write_bytes(b"video")
                    (folder / workbench.PERSON_NAME).write_bytes(png())
                    requests = []
                    client = httpx.AsyncClient

                    def handler(request):
                        if request.method == "POST":
                            requests.append(request.content.decode("utf-8", errors="replace"))
                            return httpx.Response(202, json={"task_id": f"task-{len(requests)}"})
                        if len(requests) == 1 or not retry_succeeds:
                            return httpx.Response(200, json={"status": "failed"})
                        return httpx.Response(200, json={"status": "completed", "data": [{
                            "b64_json": base64.b64encode(png()).decode(),
                        }]})

                    def factory(**kwargs):
                        return client(transport=httpx.MockTransport(handler), timeout=2)

                    with patch.object(batch_image, "_prepare_route", new_callable=AsyncMock), \
                         patch.object(batch_image, "api_key", return_value="test-only"), \
                         patch.object(httpx, "AsyncClient", side_effect=factory), \
                         patch.object(worker, "mark_publish_copy_locked"), \
                         patch.object(worker, "resume_failed_item_with_complete_images", return_value=None):
                        if retry_succeeds:
                            await workbench._generate_cover(folder, stage, "dance")
                            self.assertEqual((folder / workbench.COVER_NAMES[stage]).read_bytes(), png())
                        else:
                            with self.assertRaises(RuntimeError):
                                await workbench._generate_cover(folder, stage, "dance")
                    self.assertEqual(len(requests), 2)
                    self.assertIn("不重新摆姿势", requests[0])
                    self.assertIn("将双臂调整为自然下垂", requests[1])
                    self.assertIn(ratio, requests[1])

    async def test_downloaded_task_record_survives_publish_save_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "最终成片.mp4").write_bytes(b"video")
            (folder / workbench.PERSON_NAME).write_bytes(png())

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
            (folder / "最终成片.mp4").write_bytes(b"video")
            reference = folder / workbench.PERSON_NAME
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
                (folder / "最终成片.mp4").write_bytes(b"video")
                (folder / workbench.PERSON_NAME).write_bytes(png())
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
            (folder / "最终成片.mp4").write_bytes(b"video")
            (folder / ".h3-bilibili-request-pending").write_text("unknown")
            with patch.object(batch_image, "generate_candidate_image", new_callable=AsyncMock) as generate:
                with self.assertRaisesRegex(RuntimeError, "避免重复扣费"):
                    await workbench._generate_cover(folder, "bilibili", "dance")
                generate.assert_not_awaited()
