import base64
import io
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from PIL import Image

from backend import batch_image, chatgpt_cdp


class ImageResponseValidationTests(unittest.TestCase):
    def test_text_and_invalid_images_preserve_previous_output_and_checkpoint(self):
        responses = [
            {"choices": [{"message": {"content": "只能整理提示词"}}]},
            {"data": [{"b64_json": base64.b64encode(b"only text").decode()}]},
            {"data": [{"b64_json": "not base64!"}]},
        ]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "person.png"
            output.write_bytes(b"previous output")
            checkpoint = batch_image._task_checkpoint(output)
            checkpoint.write_text('{"taskId":"paid"}', encoding="utf-8")
            for payload in responses:
                with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                    batch_image._store(httpx.Response(200, json=payload), output)
                self.assertEqual(output.read_bytes(), b"previous output")
                self.assertTrue(checkpoint.exists())

    def test_http_success_with_error_page_is_not_an_image(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = httpx.Response(200, content=b"<html>error</html>")
        with tempfile.TemporaryDirectory() as folder, patch.object(httpx, "Client", return_value=client), patch.object(batch_image, "_route", return_value=None):
            output = Path(folder) / "person.png"
            with self.assertRaisesRegex(RuntimeError, "不是可用图片"):
                batch_image._store(httpx.Response(200, json={"data": [{"url": "https://example.test/image"}]}), output)
            self.assertFalse(output.exists())
            client.get.assert_called_once()

    def test_valid_image_is_saved_without_reencoding(self):
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), "blue").save(buffer, format="PNG")
        content = buffer.getvalue()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "person.png"
            result = batch_image._store(httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(content).decode()}]}), output)
            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), content)


class ChatImageFailureTests(unittest.TestCase):
    def test_missing_references_and_no_image_fail_explicitly(self):
        for attached, uploaded, references in [(False, True, ["scene.png"]), (True, False, ["scene.png"]), (True, True, [])]:
            with self.subTest(attached=attached, uploaded=uploaded, references=references), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                stack.enter_context(patch("playwright.sync_api.sync_playwright"))
                for name, value in {
                    "ensure_instance": "http://localhost:9444",
                    "ensure_chat_mode": True,
                    "wait_for_composer": MagicMock(),
                    "wait_until_idle": None,
                    "_clear_composer": None,
                    "_list_blobs": [],
                    "attach_images": attached,
                    "wait_for_attachments": uploaded,
                    "snapshot_generated_images": set(),
                    "new_generated_images": [],
                }.items():
                    stack.enter_context(patch.object(chatgpt_cdp, name, return_value=value))
                send = stack.enter_context(patch.object(chatgpt_cdp, "_send", return_value=True))
                output = Path(folder) / "person.png"
                with self.assertRaises(RuntimeError):
                    chatgpt_cdp.generate(references, "generate", save_to=output, new_chat=False, timeout=0.001, poll=0)
                self.assertFalse(output.exists())
                if references:
                    send.assert_not_called()
                else:
                    send.assert_called_once()
