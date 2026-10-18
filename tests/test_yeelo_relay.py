import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from backend import batch_image as relay


class YeeloRelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.object(relay, "env_value", side_effect=lambda key, default="": {
            "H3_BATCH_IMAGE_BASE_URL": "https://img.yeelo.fun/v1",
            "H3_BATCH_IMAGE_API_KEY": "test-only",
            "H3_BATCH_IMAGE_MODEL": "gpt-image-2",
        }.get(key, default))
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_form_and_single_billed_post_then_resume_saved_result(self):
        calls = []
        def handler(request):
            calls.append(request)
            if request.method == "POST":
                body = request.content
                self.assertIn(b'name="async"', body)
                self.assertIn(b'name="response_format"', body)
                self.assertNotIn(b'input_fidelity', body)
                self.assertEqual(body.count(b'name="image"'), 2)
                return httpx.Response(202, json={"task_id": "task-1", "status": "queued"})
            return httpx.Response(200, json={"status": "completed", "result": {"data": [{"url": "https://example.test/result.png"}]}})
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), timeout=60)
        with tempfile.TemporaryDirectory() as folder, patch.object(httpx, "AsyncClient", side_effect=client):
            path = Path(folder) / "person.png"
            data = {"model": "gpt-image-2", "prompt": "test", "input_fidelity": "high"}
            files = [("image", ("scene.png", b"scene", "image/png")), ("image", ("id.png", b"identity", "image/png"))]
            first = await relay._submit_or_resume(data, files, path)
            second = await relay._submit_or_resume(data, files, path)
            self.assertEqual(first.json(), second.json())
            # POST 只发一次；续跑先 GET 一次任务换新地址（不计费），再取结果。
            self.assertEqual([r.method for r in calls], ["POST", "GET", "GET"])
            with self.assertRaisesRegex(RuntimeError, "输入不同"):
                await relay._submit_or_resume({**data, "prompt": "changed"}, files, path)
            record = json.loads(relay._task_checkpoint(path).read_text())
            self.assertNotIn("test-only", json.dumps(record))

    async def test_poll_failure_never_looks_like_unsubmitted_post(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cover.png"
            client = AsyncMock()
            client.get.return_value = httpx.Response(200, json={"status": "failed"})
            with patch.object(httpx, "AsyncClient") as factory:
                factory.return_value.__aenter__.return_value = client
                with self.assertRaisesRegex(RuntimeError, "任务失败"):
                    await relay._resolve_task(httpx.Response(202, json={"data": {"id": "task-2"}}), path)
            self.assertTrue(relay._task_checkpoint(path).is_file())

    async def test_missing_id_and_unknown_response_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "任务 ID"):
                await relay._resolve_task(httpx.Response(200, json={"unexpected": True}), Path(folder)/"x.png")
        self.assertFalse(relay._is_policy_rejection(httpx.Response(400, json={"error": {"message": "content policy"}})))

    async def test_poll_transport_retry_only_uses_get(self):
        with tempfile.TemporaryDirectory() as folder:
            client = AsyncMock()
            client.get.side_effect = [httpx.ConnectError("offline"), httpx.Response(200, json={"status": "done", "output": {"images": ["https://example.test/a"]}})]
            with patch.object(httpx, "AsyncClient") as factory, patch.object(asyncio, "sleep", new_callable=AsyncMock):
                factory.return_value.__aenter__.return_value = client
                result = await relay._resolve_task(httpx.Response(202, json={"task_id": "task-3"}), Path(folder)/"cover.png")
            self.assertEqual(client.get.await_count, 2)
            client.post.assert_not_called()
            self.assertEqual(result.json()["data"][0]["url"], "https://example.test/a")

    async def test_single_cover_uses_same_task_adapter(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"person.png"
            source.write_bytes(b"reference")
            output = Path(folder)/"cover.png"
            with patch.object(relay, "_submit_or_resume", new_callable=AsyncMock) as submit, patch.object(relay, "_store", return_value=output):
                submit.return_value = httpx.Response(200, json={"data": [{"b64_json": "eA=="}]})
                await relay.generate_single_edit(source_image=source, prompt="cover", size="1024x768", output_path=output)
                data, files, path = submit.call_args.args
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0][0], "image")
                self.assertEqual(path, output)

    def test_result_wrappers(self):
        for body in ({"data": [{"url": "https://example.test/a"}]},
                     {"response": {"data": [{"b64_json": "eA=="}]}},
                     {"output": {"images": ["https://example.test/a"]}}):
            self.assertIsNotNone(relay._result_payload(body))

    async def test_any_host_that_returns_a_task_id_is_polled(self):
        """换中转站只改配置：异步任务识别不能绑死在 yeelo 主机名上。"""
        self.env.stop()
        other = patch.object(relay, "env_value", side_effect=lambda key, default="": {
            "H3_BATCH_IMAGE_BASE_URL": "https://relay.example/v1",
            "H3_BATCH_IMAGE_API_KEY": "test-only",
            "H3_BATCH_IMAGE_MODEL": "gpt-image-2",
        }.get(key, default))
        other.start()
        self.addCleanup(other.stop)
        posted = []
        def handler(request):
            posted.append((request.method, str(request.url)))
            if request.method == "POST":
                return httpx.Response(202, json={"data": {"task_id": "t-1", "status": "queued"}})
            return httpx.Response(200, json={"data": {"id": "t-1", "status": "succeeded", "images": ["https://example.test/x.png"]}})
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), timeout=60)
        with tempfile.TemporaryDirectory() as folder, patch.object(httpx, "AsyncClient", side_effect=client):
            path = Path(folder) / "person.png"
            files = [("image", ("scene.png", b"scene", "image/png"))]
            result = await relay._submit_or_resume({"model": "gpt-image-2", "prompt": "x"}, files, path)
        self.assertEqual(result.json()["data"][0]["url"], "https://example.test/x.png")
        self.assertEqual([m for m, _ in posted], ["POST", "GET"])

    async def test_finished_file_drops_the_task_record(self):
        """图已落盘就删记录：否则下一次明确重做会拿到旧结果或直接报「输入不同」。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cover.png"
            checkpoint = relay._task_checkpoint(path)
            relay._save_task_record(checkpoint, {"taskId": "t-2", "baseUrl": relay.base_url(), "model": relay.model()})
            self.assertTrue(checkpoint.is_file())
            path.write_bytes(b"done")
            relay._clear_task_checkpoint(path)
            self.assertFalse(checkpoint.exists())
            relay._clear_task_checkpoint(path)  # 再删一次也不报错

    async def test_person_resumes_a_saved_task_instead_of_raising(self):
        """有任务记录时 `.request-pending` 不再把这条钉死：续跑只会 GET 同一条任务。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "person.png"
            candidate = path.with_name("person_attempt1.png")
            candidate.with_name(candidate.name + ".request-pending").write_text("{}", encoding="utf-8")
            relay._save_task_record(
                relay._task_checkpoint(candidate),
                {"taskId": "t-3", "baseUrl": relay.base_url(), "model": relay.model(), "fingerprint": "f"},
            )

            async def fake_generate(**kwargs):
                Path(kwargs["output_path"]).write_bytes(b"generated")
                return Path(kwargs["output_path"])

            with patch.object(relay, "generate_candidate_image", side_effect=fake_generate), \
                    patch.object(relay, "_upscale_after_relay", new_callable=AsyncMock):
                result, score = await relay.generate_person_image(
                    scene_image=path, identity_image=None, prompt="p", ratio="9:16", output_path=path,
                )
            self.assertEqual(result, path)
            self.assertEqual(score, -1)
            self.assertEqual(path.read_bytes(), b"generated")
            self.assertFalse(candidate.with_name(candidate.name + ".request-pending").exists())


if __name__ == "__main__":
    unittest.main()
