import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import batch_worker, image_workbench


class CoverAfterVideoTests(unittest.IsolatedAsyncioTestCase):
    """2026-10-18：人物图之后先不出封面，出片后从成片抽帧再出封面。"""

    async def test_prep_no_longer_generates_covers(self):
        with patch.object(batch_worker, "_item", return_value={"status": "pending"}), patch.object(
            batch_worker, "_set_item"
        ), patch.object(batch_worker, "_download", new_callable=AsyncMock), patch.object(
            batch_worker, "_prepare_review", new_callable=AsyncMock
        ) as prepare, patch.object(
            batch_worker, "_finish_prep_covers", new_callable=AsyncMock
        ) as covers:
            await batch_worker._run_prep("batch", "item")
        prepare.assert_awaited()
        covers.assert_not_awaited()

    async def test_finish_item_covers_uses_video_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            item = {"status": "completed", "kind": "dance", "ai": {}}
            with patch.object(batch_worker, "_item", return_value=item), patch.object(
                batch_worker, "full_auto_enabled", return_value=True
            ), patch.object(batch_worker, "_covers_ready", return_value=False), patch.object(
                batch_worker, "_ensure_video_cover_person",
                new_callable=AsyncMock, return_value=folder / batch_worker.COVER_PERSON_NAME,
            ) as ensure, patch.object(
                batch_worker, "_generate_covers_via_api", new_callable=AsyncMock, return_value=[]
            ) as generate, patch.object(
                batch_worker.batch_store, "add_item_log"
            ):
                await batch_worker._finish_item_covers("batch", "item", folder)
        ensure.assert_awaited()
        generate.assert_awaited()

    async def test_cover_failure_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            item = {"status": "completed", "kind": "dance", "ai": {}}
            with patch.object(batch_worker, "_item", return_value=item), patch.object(
                batch_worker, "full_auto_enabled", return_value=True
            ), patch.object(batch_worker, "_covers_ready", return_value=False), patch.object(
                batch_worker, "_ensure_video_cover_person", new_callable=AsyncMock, return_value=None
            ), patch.object(
                batch_worker, "_generate_covers_via_api",
                new_callable=AsyncMock, return_value=["B站封面失败"],
            ), patch.object(batch_worker.batch_store, "add_item_log") as log:
                await batch_worker._finish_item_covers("batch", "item", folder)
        self.assertTrue(any("图片页" in str(call) for call in log.call_args_list))

    async def test_generate_cover_prefers_video_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            frame = folder / batch_worker.COVER_PERSON_NAME
            person = folder / "人物图.png"
            frame.write_bytes(b"frame")
            person.write_bytes(b"person")
            (folder / "发布文案.txt").write_text("标题：测试标题\n简介：测试简介", encoding="utf-8")
            seen = {}

            async def fake_generate(**kwargs):
                seen["scene_image"] = kwargs["scene_image"]
                Path(kwargs["output_path"]).write_bytes(b"cover")

            with patch.object(
                image_workbench.batch_image, "generate_candidate_image", side_effect=fake_generate
            ), patch.object(image_workbench, "save_upload"):
                await image_workbench._generate_cover(folder, "bilibili", "dance")
        self.assertEqual(seen["scene_image"], frame)

    async def test_cover_reference_falls_back_to_person_image(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            person = folder / "人物图.png"
            person.write_bytes(b"person")
            self.assertEqual(batch_worker.cover_reference_image({}, folder), person)

    def test_start_all_missing_waits_for_video(self):
        items = [
            {"id": "wait", "assets": {"person": {"name": "p"}, "bilibili": None, "douyin": None}, "canCover": False},
            {"id": "queued", "assets": {"person": {"name": "p"}, "bilibili": None, "douyin": None}, "canCover": True},
            {"id": "done", "assets": {"person": {"name": "p"}, "bilibili": {"name": "b"}, "douyin": {"name": "d"}}, "canCover": True},
        ]
        with patch.object(image_workbench, "list_items", return_value=items), patch.object(
            image_workbench, "start_automatic"
        ) as start:
            result = image_workbench.start_all_missing()
        self.assertEqual(result, {"queued": 1, "waiting": 1})
        start.assert_called_once_with("queued")

    def test_single_cover_generation_refuses_before_video(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "人物图.png").write_bytes(b"person")
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            with patch.object(image_workbench, "resolve_folder", return_value=folder), patch.object(
                image_workbench, "infer_kind", return_value="dance"
            ), patch.object(image_workbench, "_set_auto") as set_auto, patch.object(
                image_workbench, "_generate_cover", new_callable=AsyncMock
            ) as cover:
                asyncio.run(image_workbench.run_automatic("folder", stage="bilibili"))
        cover.assert_not_awaited()
        messages = [str(call) for call in set_auto.call_args_list]
        self.assertTrue(any("还没有最终成片" in message for message in messages))

    def test_single_cover_generation_uses_cover_person(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "人物图.png").write_bytes(b"person")
            (folder / "封面人物图.png").write_bytes(b"cover-person")
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            with patch.object(image_workbench, "resolve_folder", return_value=folder), patch.object(
                image_workbench, "infer_kind", return_value="dance"
            ), patch.object(image_workbench, "_set_auto"), patch.object(
                image_workbench, "_generate_cover", new_callable=AsyncMock
            ) as cover:
                asyncio.run(image_workbench.run_automatic("folder", stage="bilibili"))
        cover.assert_awaited_once()

    def test_whole_automatic_skips_covers_before_video(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "人物图.png").write_bytes(b"person")
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            with patch.object(image_workbench, "resolve_folder", return_value=folder), patch.object(
                image_workbench, "infer_kind", return_value="dance"
            ), patch.object(image_workbench, "_set_auto") as set_auto, patch.object(
                image_workbench, "_generate_cover", new_callable=AsyncMock
            ) as cover:
                asyncio.run(image_workbench.run_automatic("folder"))
        cover.assert_not_awaited()
        self.assertTrue(any("封面等成片后" in str(call) for call in set_auto.call_args_list))

    def test_prep_assets_ready_ignores_covers(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "人物图.png").write_bytes(b"person")
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            item = {"outputs": {"folder": str(folder)}}
            with patch.object(batch_worker, "_recorded_publish_folder", return_value=folder):
                self.assertTrue(batch_worker._prep_assets_ready(item))
                self.assertFalse(batch_worker._covers_ready(item))

    def test_extract_cover_person_from_generated_video(self):
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("本机没有 ffmpeg")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video = folder / "clip.mp4"
            made = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=360x640:rate=24:duration=3",
                    "-pix_fmt", "yuv420p", str(video),
                ],
                capture_output=True,
                timeout=120,
            )
            if made.returncode != 0:
                self.skipTest("本机 ffmpeg 无法生成测试视频")
            target = folder / batch_worker.COVER_PERSON_NAME
            with patch.object(batch_worker, "_pick_hand_cover_person", return_value=False):
                batch_worker.extract_cover_person(video, target)
            self.assertTrue(target.is_file() and target.stat().st_size > 0)
            self.assertEqual(target.read_bytes()[:2], b"\xff\xd8")   # JPEG

    def test_extract_cover_person_prefers_the_hand_picker(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video = folder / "clip.mp4"
            video.write_bytes(b"fake")
            target = folder / batch_worker.COVER_PERSON_NAME

            def fake_pick(video_path, target_path):
                Path(target_path).write_bytes(b"\xff\xd8hand")
                return True

            with patch.object(batch_worker, "_pick_hand_cover_person", side_effect=fake_pick):
                batch_worker.extract_cover_person(video, target)
            self.assertEqual(target.read_bytes(), b"\xff\xd8hand")

    def test_extract_cover_person_falls_back_when_no_hand_is_found(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video = folder / "clip.mp4"
            video.write_bytes(b"fake")
            target = folder / batch_worker.COVER_PERSON_NAME
            with patch.object(batch_worker, "_pick_hand_cover_person", return_value=False), patch.object(
                batch_worker, "_video_duration_seconds", return_value=3.0
            ), patch.object(
                batch_worker, "_scan_all_frames", return_value=[(1.0, b"\xff\xd8fallback")]
            ):
                batch_worker.extract_cover_person(video, target)
            self.assertEqual(target.read_bytes(), b"\xff\xd8fallback")


if __name__ == "__main__":
    unittest.main()
