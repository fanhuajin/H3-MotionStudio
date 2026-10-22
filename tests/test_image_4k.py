import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from backend import batch_image as images


class Native4KTests(unittest.IsolatedAsyncioTestCase):
    def test_native_sizes_skip_local_upscale(self):
        self.assertEqual(images.image_sizes(), {"4:3": "3264x2448", "3:4": "2448x3264", "9:16": "2160x3840"})
        for ratio, size in images.image_sizes().items():
            self.assertFalse(images.needs_local_upscale(ratio))
            w, h = map(int, size.split("x"))
            self.assertLessEqual(w*h, 8294400)
            self.assertEqual(w % 16, 0)
            self.assertEqual(h % 16, 0)

    async def test_old_paid_requests_resume_without_post(self):
        for sizes in (images.IMAGE_SIZES_1K, images.IMAGE_SIZES_2K):
            with tempfile.TemporaryDirectory() as raw:
                output = Path(raw) / "image.png"
                data = {"model": images.model(), "size": sizes["4:3"], "prompt": "frozen"}
                fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
                images._save_task_record(images._task_checkpoint(output), {
                    "baseUrl": images.base_url(), "model": images.model(),
                    "taskId": "old-paid", "fingerprint": fingerprint,
                })
                data["size"] = images.image_sizes()["4:3"]
                with patch.object(images, "_resolve_task", new_callable=AsyncMock, return_value=httpx.Response(200)) as resolve, patch.object(images, "_post_with_route_fallback", new_callable=AsyncMock) as post:
                    await images._submit_or_resume(data, [], output)
                    post.assert_not_awaited()
                    self.assertEqual(resolve.call_args.args[2], fingerprint)
                    data["prompt"] = "changed"
                    with self.assertRaises(RuntimeError):
                        await images._submit_or_resume(data, [], output)
