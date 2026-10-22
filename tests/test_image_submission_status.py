import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend import batch_image as relay


class ImageSubmissionStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_read_timeout_is_unknown_and_never_reposted(self):
        calls = []
        messages = []
        timeouts = []
        client = httpx.AsyncClient

        def handler(request):
            calls.append(request.method)
            raise httpx.ReadTimeout("", request=request)

        def factory(**kwargs):
            timeouts.append(kwargs["timeout"])
            return client(transport=httpx.MockTransport(handler))

        with tempfile.TemporaryDirectory() as raw, \
             patch.object(relay, "async_tasks", return_value=True), \
             patch.object(relay, "api_key", return_value="test-only"), \
             patch.object(httpx, "AsyncClient", side_effect=factory), \
             relay.report_request_status(messages.append):
            output = Path(raw) / "cover.png"
            with self.assertRaisesRegex(RuntimeError, "提交阶段.*未收到任务 ID"):
                await relay._submit_or_resume({"prompt": "test"}, [], output)
            self.assertFalse(relay._task_checkpoint(output).exists())
        self.assertEqual(calls, ["POST"])
        self.assertEqual(timeouts[0].read, relay.SUBMIT_TIMEOUT)
        self.assertFalse(any("已受理" in message for message in messages))

    async def test_acceptance_is_reported_only_after_task_is_saved(self):
        client = httpx.AsyncClient
        calls = []
        messages = []
        saved_ids = []
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "cover.png"

            def report(message):
                messages.append(message)
                if "已受理" in message:
                    record = json.loads(relay._task_checkpoint(output).read_text(encoding="utf-8"))
                    saved_ids.append(record["taskId"])

            def handler(request):
                calls.append(request.method)
                if request.method == "POST":
                    return httpx.Response(202, json={"task_id": "accepted-123"})
                return httpx.Response(200, json={"status": "completed", "data": [{"url": "https://example.test/image.png"}]})

            def factory(**kwargs):
                return client(transport=httpx.MockTransport(handler))

            with patch.object(relay, "api_key", return_value="test-only"), \
                 patch.object(httpx, "AsyncClient", side_effect=factory), \
                 relay.report_request_status(report):
                await relay._submit_or_resume({"prompt": "test"}, [], output)
        self.assertEqual(calls, ["POST", "GET"])
        self.assertEqual(saved_ids, ["accepted-123"])
        self.assertIn("尚未确认受理", messages[0])
        self.assertIn("任务 ID：accepted-123", messages[1])

    async def test_poll_timeout_preserves_accepted_task(self):
        with tempfile.TemporaryDirectory() as raw, \
             patch.object(relay, "REQUEST_TIMEOUT", 0), \
             patch.object(relay, "_post_with_route_fallback", new=AsyncMock()) as post:
            post.return_value = httpx.Response(202, json={"task_id": "paid-task"})
            output = Path(raw) / "cover.png"
            for attempt in range(2):
                with self.assertRaisesRegex(RuntimeError, "任务等待超时.*已保存任务 ID"):
                    await relay._submit_or_resume({"prompt": "test"}, [], output)
                self.assertEqual(json.loads(relay._task_checkpoint(output).read_text(encoding="utf-8"))["taskId"], "paid-task")
            post.assert_awaited_once()

    async def test_status_reporters_are_isolated_between_tasks_and_reset(self):
        first, second = [], []

        async def emit(callback, label):
            with relay.report_request_status(callback):
                await asyncio.sleep(0)
                relay._report_request_status(label)

        await asyncio.gather(emit(first.append, "first"), emit(second.append, "second"))
        relay._report_request_status("outside")
        self.assertEqual(first, ["first"])
        self.assertEqual(second, ["second"])

    async def test_synchronous_provider_keeps_generation_timeout(self):
        client = httpx.AsyncClient
        timeouts = []

        def factory(**kwargs):
            timeouts.append(kwargs["timeout"])
            return client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))

        with patch.object(relay, "async_tasks", return_value=False), \
             patch.object(relay, "api_key", return_value="test-only"), \
             patch.object(httpx, "AsyncClient", side_effect=factory):
            await relay._post({}, [])
        self.assertEqual(timeouts, [relay.REQUEST_TIMEOUT])
