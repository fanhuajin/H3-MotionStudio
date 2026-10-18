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

    def test_only_the_five_features_are_blurred_not_the_whole_head(self):
        """2026-10-18 用户：「只要糊住五官就可以 不要特别大」——额头、下颌、脸侧要保持原像素。"""
        with tempfile.TemporaryDirectory() as raw:
            frame = Path(raw) / "抽帧.png"
            image = Image.new("RGB", (100, 100))
            for x in range(100):
                for y in range(100):
                    image.putpixel((x, y), (255, 255, 255) if (x + y) % 2 else (0, 0, 0))
            image.save(frame)
            box = {"x0": .2, "y0": .2, "x1": .8, "y1": .8}
            with patch.object(batch_image, "_locate_source_faces", return_value=[box]):
                result, ok = asyncio.run(batch_image.defocus_reference_face(frame, Path(raw) / "guard"))
            self.assertTrue(ok)
            with Image.open(result) as guarded:
                self.assertNotEqual(guarded.getpixel((50, 50)), image.getpixel((50, 50)))   # 五官中段
                self.assertEqual(guarded.getpixel((50, 28)), image.getpixel((50, 28)))     # 额头
                self.assertEqual(guarded.getpixel((50, 88)), image.getpixel((50, 88)))     # 下颌
                self.assertEqual(guarded.getpixel((24, 50)), image.getpixel((24, 50)))     # 脸侧

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


    def test_motion_prompt_retains_actions_but_locks_reference_appearance(self):
        from backend import batch_ai
        actions = "抬右手，向左转身，随节拍点头。"
        result = batch_ai.motion_reference_prompt(actions)
        self.assertTrue(result.startswith(actions))
        self.assertIn("本次人物参考图为唯一视觉依据", result)
        self.assertEqual(batch_ai.motion_reference_prompt(result), result)

    def test_material_zip_rejects_missing_reference_images(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(FileNotFoundError):
                image_workbench.material_zip(Path(raw), "person", "dance")

    def test_exif_orientation_matches_detector_coordinates(self):
        with tempfile.TemporaryDirectory() as raw:
            frame = Path(raw) / "rotated.jpg"
            image = Image.new("RGB", (100,60), "white")
            exif = image.getexif(); exif[274] = 6
            image.save(frame, exif=exif)
            with patch.object(batch_image, "_locate_source_faces", return_value=[{"x0": .1, "y0": .1, "x1": .6, "y1": .6}]):
                guarded = batch_image.prepare_defocused_reference(frame, Path(raw)/"guard")
            with Image.open(guarded) as result:
                self.assertEqual(result.size, (60,100))

    def test_legacy_candidate_uses_guarded_frame_and_current_prompt(self):
        from backend import chatgpt_image, batch_worker
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw)
            frame = work / "scene-frame.jpg"; identity = work / "identity.png"
            guarded = work / "guard.png"; output = work / "result.png"
            for path in (frame,identity,guarded,output):
                Image.new("RGB", (100,100)).save(path)
            item = {"kind":"dance", "ratio":"9:16", "ai":{}}
            with patch.object(chatgpt_image, "_work_dir", return_value=work), patch.object(chatgpt_image, "IDENTITY_IMAGE", identity), patch.object(batch_worker, "_item", return_value=item), patch.object(batch_worker.batch_store, "add_item_log"), patch.object(batch_image, "prepare_defocused_reference", return_value=guarded), patch.object(chatgpt_image, "prompt_for_kind", side_effect=AssertionError("不能读取旧提示词")), patch.object(chatgpt_image, "_out_path", return_value=output), patch.object(chatgpt_image, "write_prompt_attachment", return_value=work/"prompt.txt"), patch.object(chatgpt_image, "_upload_item_image"), patch.object(chatgpt_image, "_mark"), patch.object(chatgpt_image.cd, "generate", return_value=output) as generate:
                chatgpt_image._generate_candidate_sync("batch","item")
            args = generate.call_args.args
            self.assertEqual(args[0], [guarded,identity])
            self.assertIn("搭配合适的新发型", args[1])
            self.assertIn("图二是唯一的面部身份参考", args[1])

if __name__ == "__main__":
    unittest.main()
