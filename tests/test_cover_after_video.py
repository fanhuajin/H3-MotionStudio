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
                seen["reference_bytes"] = kwargs["scene_image"].read_bytes()
                Path(kwargs["output_path"]).write_bytes(b"cover")

            with patch.object(
                image_workbench.batch_image, "generate_candidate_image", side_effect=fake_generate
            ), patch.object(image_workbench, "save_upload"):
                await image_workbench._generate_cover(folder, "bilibili", "dance")
        self.assertEqual(seen["reference_bytes"], b"frame")

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

    def test_extract_cover_person_falls_back_when_picker_is_unavailable(self):
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


    async def test_covers_go_to_background_after_delivery(self):
        """默认不挡下一条视频：抽完封面人物图就把两张封面丢到后台。"""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(batch_worker, "full_auto_enabled", return_value=True), patch.object(
                batch_worker, "cover_blocking", return_value=False
            ), patch.object(
                batch_worker, "_ensure_video_cover_person", new_callable=AsyncMock
            ) as ensure, patch.object(
                batch_worker, "spawn_item_covers", return_value=True
            ) as spawn:
                await batch_worker._finish_covers_after_delivery("batch", "item", folder)
        ensure.assert_awaited()
        spawn.assert_called_once()

    async def test_cover_blocking_switch_still_awaits_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(batch_worker, "full_auto_enabled", return_value=True), patch.object(
                batch_worker, "cover_blocking", return_value=True
            ), patch.object(
                batch_worker, "_finish_item_covers", new_callable=AsyncMock
            ) as covers, patch.object(batch_worker, "spawn_item_covers") as spawn:
                await batch_worker._finish_covers_after_delivery("batch", "item", folder)
        covers.assert_awaited_once()
        spawn.assert_not_called()

    async def test_background_covers_are_tracked_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow(*_args, **_kwargs):
                started.set()
                await release.wait()

            with patch.object(batch_worker, "_finish_item_covers", side_effect=slow), patch.object(
                batch_worker.batch_store, "add_item_log"
            ):
                try:
                    self.assertTrue(batch_worker.spawn_item_covers("b", "i", folder))
                    self.assertFalse(
                        batch_worker.spawn_item_covers("b", "i", folder), "同一条只排一次"
                    )
                    self.assertTrue(batch_worker.covers_in_flight())
                    await asyncio.wait_for(started.wait(), timeout=2)
                finally:
                    release.set()
                    tasks = list(batch_worker._BG_COVER_TASKS)
                    if tasks:
                        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
                    batch_worker._BG_COVER_TASKS.clear()
                    batch_worker._BG_COVER_KEYS.clear()
        self.assertFalse(batch_worker.covers_in_flight())

    async def test_background_covers_count_as_work_in_flight(self):
        """后台补封面时不许自动关机：它还在写发布目录。"""
        blocker = asyncio.Event()

        async def pending():
            await blocker.wait()

        task = asyncio.create_task(pending())
        batch_worker._BG_COVER_TASKS.add(task)
        try:
            self.assertTrue(batch_worker.work_in_flight())
        finally:
            blocker.set()
            await task
            batch_worker._BG_COVER_TASKS.discard(task)
        self.assertFalse(batch_worker.work_in_flight())

    def test_process_confirmed_marks_delivered_before_covers(self):
        """源码级顺序：先标完成放行下一条，再去补封面（别把顺序改回去）。"""
        import inspect

        source = inspect.getsource(batch_worker._process_confirmed)
        mark_at = source.index("await _mark_delivered")
        covers_at = source.index("_finish_covers_after_delivery", mark_at)
        self.assertLess(mark_at, covers_at, "必须先交付完成，再补封面")

    def test_batch_runner_waits_for_background_covers(self):
        import inspect

        source = inspect.getsource(batch_worker.run_batch)
        self.assertIn("pending_covers", source)
        self.assertIn("_BG_COVER_TASKS", source)


if __name__ == "__main__":
    unittest.main()
