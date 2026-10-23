import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import batch_worker, image_workbench


class CoverAfterVideoTests(unittest.IsolatedAsyncioTestCase):

    """封面不等待视频交付，使用原人物图并在后台完成。"""

    async def test_prep_starts_background_covers_without_video(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(batch_worker, "_item", return_value={"status": "pending"}), patch.object(
                batch_worker, "_set_item"
            ), patch.object(batch_worker, "_download", new_callable=AsyncMock), patch.object(
                batch_worker, "_prepare_review", new_callable=AsyncMock
            ) as prepare, patch.object(batch_worker, "full_auto_enabled", return_value=True), patch.object(
                batch_worker, "_recorded_publish_folder", return_value=folder
            ), patch.object(batch_worker, "spawn_item_covers") as spawn:
                await batch_worker._run_prep("batch", "item")
            prepare.assert_awaited()
            spawn.assert_called_once_with("batch", "item", folder)

    async def test_cover_reference_falls_back_to_person_image(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            person = folder / "人物图.png"
            person.write_bytes(b"person")
            self.assertEqual(batch_worker.cover_reference_image({}, folder), person)

    def test_start_all_missing_waits_for_materials(self):
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

    def test_single_cover_generation_runs_before_video(self):
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
        cover.assert_awaited_once()

    def test_single_cover_generation_uses_person_after_video(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "人物图.png").write_bytes(b"person")
            (folder / "最终成片.mp4").write_bytes(b"video")
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            with patch.object(image_workbench, "resolve_folder", return_value=folder), patch.object(
                image_workbench, "infer_kind", return_value="dance"
            ), patch.object(image_workbench, "_set_auto"), patch.object(
                image_workbench, "_generate_cover", new_callable=AsyncMock
            ) as cover:
                asyncio.run(image_workbench.run_automatic("folder", stage="bilibili"))
        cover.assert_awaited_once()

    def test_whole_automatic_generates_both_covers_before_video(self):
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
        self.assertEqual(cover.await_count, 2)

    def test_prep_assets_ready_ignores_covers(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "人物图.png").write_bytes(b"person")
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            item = {"outputs": {"folder": str(folder)}}
            with patch.object(batch_worker, "_recorded_publish_folder", return_value=folder):
                self.assertTrue(batch_worker._prep_assets_ready(item))
                self.assertFalse(batch_worker._covers_ready(item))


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
