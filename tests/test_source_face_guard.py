from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image
from backend import batch_image, image_workbench


class SourceFaceGuardTests(unittest.TestCase):
    def test_detection_failure_never_returns_original(self):
        with tempfile.TemporaryDirectory() as raw:
            frame = Path(raw) / "抽帧.png"
            Image.new("RGB", (100, 100)).save(frame)
            with patch.object(batch_image, "_locate_source_faces", return_value=[]):
                with self.assertRaisesRegex(ValueError, "定位失败"):
                    asyncio.run(batch_image.defocus_reference_face(frame, Path(raw) / "guard"))
            self.assertFalse((Path(raw) / "guard").exists())

    def test_all_detected_faces_are_blurred_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as raw:
            frame = Path(raw) / "抽帧.png"
            image = Image.new("RGB", (100, 100), "white")
            for x in range(100):
                for y in range(100):
                    image.putpixel((x,y), (255,255,255) if (x+y)%2 else (0,0,0))
            image.save(frame)
            original = frame.read_bytes()
            boxes = [{"x0": .1, "y0": .1, "x1": .4, "y1": .4},
                     {"x0": .6, "y0": .6, "x1": .9, "y1": .9}]
            with patch.object(batch_image, "_locate_source_faces", return_value=boxes):
                result, ok = asyncio.run(batch_image.defocus_reference_face(frame, Path(raw) / "guard"))
            self.assertTrue(ok)
            self.assertNotEqual(result, frame)
            self.assertEqual(frame.read_bytes(), original)
            with Image.open(result) as blurred:
                self.assertNotEqual(blurred.getpixel((20,20)), image.getpixel((20,20)))
                self.assertNotEqual(blurred.getpixel((70,70)), image.getpixel((70,70)))
                self.assertEqual(blurred.getpixel((0,0)), image.getpixel((0,0)))

    def test_person_api_stops_before_billed_request_on_failed_guard(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            Image.new("RGB", (100,100)).save(folder / "抽帧.jpg")
            generate = AsyncMock()
            with patch.object(batch_image, "defocus_reference_face", AsyncMock(return_value=(folder / "抽帧.jpg", False))), patch.object(batch_image, "generate_person_image", generate):
                with self.assertRaisesRegex(ValueError, "未成功糊脸"):
                    asyncio.run(image_workbench._generate_person(folder, "dance"))
            generate.assert_not_awaited()

    def test_manual_frame_asset_and_materials_are_guarded(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            frame = folder / "抽帧.jpg"
            Image.new("RGB", (100,100)).save(frame)
            guarded = folder / "guarded.png"
            Image.new("RGB", (100,100)).save(guarded)
            with patch.object(batch_image, "prepare_defocused_reference", return_value=guarded) as prepare:
                self.assertEqual(image_workbench.asset_path(folder, "frame"), guarded)
                prepare.assert_called_once_with(frame, folder / ".h3-reference-guard")
            with patch.object(batch_image, "prepare_defocused_reference", side_effect=ValueError("定位失败")):
                with self.assertRaisesRegex(ValueError, "定位失败"):
                    image_workbench.material_zip(folder, "person", "dance")


if __name__ == "__main__":
    unittest.main()
