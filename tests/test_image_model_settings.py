import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend import batch_image, image_model_settings as settings


class ImageModelSettingsTests(unittest.TestCase):
    def test_persistence_validation_and_environment_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(settings, "SETTINGS_PATH", Path(directory) / "model.json"), patch.object(batch_image, "env_value", return_value="environment-model"):
            self.assertEqual(batch_image.model(), "environment-model")
            self.assertEqual(settings.save_model(" gpt-image-2.5 "), "gpt-image-2.5")
            self.assertEqual(batch_image.model(), "gpt-image-2.5")
            self.assertEqual(json.loads(settings.SETTINGS_PATH.read_text(encoding="utf-8")), {"model": "gpt-image-2.5"})
            for invalid in ("", " ", "bad model", "bad\nmodel", "x" * 129):
                with self.assertRaises(ValueError):
                    settings.save_model(invalid)
            self.assertEqual(settings.selected_model(), "gpt-image-2.5")

    def test_api_reads_and_saves_model(self):
        from backend.app import ImageModelRequest, get_image_settings, put_image_settings

        with tempfile.TemporaryDirectory() as directory, patch.object(settings, "SETTINGS_PATH", Path(directory) / "model.json"):
            self.assertEqual(put_image_settings(ImageModelRequest(model="custom-model")), {"model": "custom-model"})
            self.assertEqual(get_image_settings(), {"model": "custom-model"})


class ImageModelRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_keeps_original_model_and_never_posts(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(settings, "SETTINGS_PATH", Path(directory) / "model.json"), patch.object(batch_image, "base_url", return_value="https://relay.test/v1"):
            settings.save_model("new-model")
            path = Path(directory) / "image.png"
            old_data = {"model": "old-model", "prompt": "same"}
            record = {"model": "old-model", "baseUrl": batch_image.base_url(), "taskId": "paid-task", "fingerprint": hashlib.sha256(json.dumps(old_data, sort_keys=True).encode()).hexdigest()}
            batch_image._save_task_record(batch_image._task_checkpoint(path), record)
            async def resolve(response, output, fingerprint):
                self.assertEqual(batch_image.model(), "old-model")
                self.assertEqual(response.json()["task_id"], "paid-task")
                return httpx.Response(200, json={"ok": True})
            with patch.object(batch_image, "_post_with_route_fallback", new_callable=AsyncMock) as post, patch.object(batch_image, "_resolve_task", side_effect=resolve):
                await batch_image._submit_or_resume(dict(old_data, model="new-model"), [], path)
                post.assert_not_awaited()
            self.assertEqual(batch_image.model(), "new-model")
