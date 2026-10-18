import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from backend import batch_image, batch_worker, image_workbench as workbench


class CoverOriginalOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_covers_keep_original_bytes_without_any_resize(self):
        for stage, size in (("bilibili", (1024, 768)), ("douyin", (768, 1024)),
                            ("bilibili", (319, 241))):
            with self.subTest(stage=stage, size=size), tempfile.TemporaryDirectory() as raw:
                folder = Path(raw)
                reference = folder / batch_worker.COVER_PERSON_NAME
                Image.new("RGB", (64, 64)).save(reference)
                buffer = io.BytesIO()
                Image.new("RGB", size, (70, 90, 80)).save(buffer, "PNG")
                payload = buffer.getvalue()
                response = httpx.Response(200, json={"data": [{
                    "b64_json": base64.b64encode(payload).decode(),
                }]})
                # Real generation/save code, with all external requests and GPU work isolated.
                with patch.object(batch_image, "_prepare_route", new_callable=AsyncMock), \
                     patch.object(batch_image, "api_key", return_value="test-only"), \
                     patch.object(batch_image, "_submit_or_resume", new=AsyncMock(return_value=response)), \
                     patch.object(batch_image, "_upscale_after_relay", new_callable=AsyncMock) as upscale, \
                     patch.object(batch_image, "ensure_deliverable_image") as repair, \
                     patch.object(workbench.ImageOps, "fit") as resize, \
                     patch.object(batch_worker, "mark_publish_copy_locked") as lock_copy, \
                     patch.object(batch_worker, "resume_failed_item_with_complete_images", return_value=None):
                    await workbench._generate_cover(folder, stage, "dance")
                destination = folder / workbench.COVER_NAMES[stage]
                self.assertEqual(destination.read_bytes(), payload)
                with Image.open(destination) as image:
                    self.assertEqual(image.size, size)
                upscale.assert_not_awaited()
                repair.assert_not_called()
                resize.assert_not_called()
                lock_copy.assert_called_once_with(folder)
                self.assertFalse((folder / f".h3-{stage}-request-pending").exists())

    async def test_batch_and_image_page_share_original_cover_path(self):
        for entry in ("batch", "single", "missing"):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as raw:
                folder = Path(raw)
                Image.new("RGB", (64, 64)).save(folder / batch_worker.COVER_PERSON_NAME)
                seen = []

                async def generate(**kwargs):
                    self.assertIs(kwargs["upscale"], False)
                    self.assertEqual(kwargs["scene_image"], folder / batch_worker.COVER_PERSON_NAME)
                    image = io.BytesIO()
                    size = (320, 240) if kwargs["ratio"] == "4:3" else (240, 320)
                    Image.new("RGB", size).save(image, "PNG")
                    payload = image.getvalue()
                    Path(kwargs["output_path"]).write_bytes(payload)
                    seen.append(payload)

                with patch.object(batch_image, "generate_candidate_image", side_effect=generate), \
                     patch.object(workbench, "resolve_folder", return_value=folder), \
                     patch.object(workbench, "infer_kind", return_value="dance"), \
                     patch.object(workbench, "_set_auto"), \
                     patch.object(workbench, "_stop_requested", return_value=False), \
                     patch.object(batch_worker, "_item", return_value={"kind": "dance"}), \
                     patch.object(batch_worker, "_set_item"), \
                     patch.object(batch_worker.batch_store, "add_item_log"), \
                     patch.object(batch_worker, "mark_publish_copy_locked"), \
                     patch.object(batch_worker, "resume_failed_item_with_complete_images", return_value=None):
                    if entry == "batch":
                        self.assertEqual(await batch_worker._generate_covers_via_api("b", "i", folder), [])
                    else:
                        if entry == "missing":
                            Image.new("RGB", (64, 64)).save(folder / workbench.PERSON_NAME)
                        await workbench.run_automatic("i", stage="bilibili" if entry == "single" else None)
                stages = ["bilibili"] if entry == "single" else ["bilibili", "douyin"]
                self.assertEqual(len(seen), len(stages))
                for stage, payload in zip(stages, seen):
                    self.assertEqual((folder / workbench.COVER_NAMES[stage]).read_bytes(), payload)
