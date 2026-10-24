import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from backend import batch_ai, batch_image, batch_worker as worker, frame_replacement as replacement
from backend import image_workbench as workbench


def frame(color):
    output = io.BytesIO()
    Image.new("RGB", (96, 128), color).save(output, format="JPEG")
    return output.getvalue()


class FrameReplacementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.folder = self.root / "publish"
        self.folder.mkdir()
        self.original = frame("red")
        self.selected = frame("blue")
        self.current = self.folder / "抽帧.jpg"
        self.current.write_bytes(self.original)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"source")
        self.canonical = self.root / "scene-frame.jpg"
        self.canonical.write_bytes(self.original)
        self.item = {"id": "item", "status": "failed", "sourcePath": str(self.source),
                     "ai": {"sceneFramePath": str(self.canonical), "title": "舞蹈"}}
        self.enterContext(patch.object(worker, "DATA_DIR", self.root))
        self.enterContext(patch.object(worker, "_find_item_for_folder", return_value=({"id": "batch"}, self.item)))
        self.enterContext(patch.object(worker, "_item", return_value=self.item))
        self.update = self.enterContext(patch.object(worker, "_set_item"))
        self.enterContext(patch.object(worker.batch_store, "add_item_log"))
        self.enterContext(patch.object(worker, "_ITEM_TASKS", {}))
        self.enterContext(patch.object(workbench, "_AUTO_LOCK", asyncio.Lock()))
        self.enterContext(patch.object(workbench, "AUTOMATIONS", {}))
        self.candidates = self.enterContext(patch.object(replacement, "_candidates", return_value=[self.selected]))
        self.choose = self.enterContext(patch.object(batch_ai, "choose_covered_frame", new_callable=AsyncMock, return_value=1))
        self.blur = self.enterContext(patch.object(batch_image, "prepare_defocused_reference"))

    async def test_replace_updates_both_references_and_archives_old_frame(self):
        await replacement.replace_frame(self.folder)
        self.assertEqual(self.current.read_bytes(), self.selected)
        self.assertEqual(self.canonical.read_bytes(), self.selected)
        history = list((self.folder / ".h3-frame-history").glob("*.jpg"))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].read_bytes(), self.original)
        self.blur.assert_called_once()
        self.update.assert_called_once()
        self.assertEqual(self.candidates.call_args.args[1], [self.original])

    async def test_unavailable_or_unsafe_selection_preserves_old_frame(self):
        for error in (ValueError("没有合适画面"), RuntimeError("视觉接口失败")):
            with self.subTest(error=error):
                self.choose.side_effect = error
                with self.assertRaises(type(error)):
                    await replacement.replace_frame(self.folder)
                self.assertEqual(self.current.read_bytes(), self.original)
                self.assertEqual(self.canonical.read_bytes(), self.original)
                self.update.assert_not_called()

    async def test_blur_failure_and_running_job_preserve_old_frame(self):
        self.blur.side_effect = ValueError("无法定位人脸")
        with self.assertRaisesRegex(ValueError, "无法定位"):
            await replacement.replace_frame(self.folder)
        self.assertEqual(self.current.read_bytes(), self.original)
        self.item["status"] = "running"
        with self.assertRaisesRegex(ValueError, "正在处理"):
            await replacement.replace_frame(self.folder)
        self.update.assert_not_called()

    async def test_source_missing_and_source_change_do_not_replace(self):
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, "不存在"):
            await replacement.replace_frame(self.folder)
        self.source.write_bytes(b"source")
        async def changed(_frames):
            self.item["status"] = "running"
            return 1
        self.choose.side_effect = changed
        with self.assertRaisesRegex(ValueError, "已变化"):
            await replacement.replace_frame(self.folder)
        self.assertEqual(self.current.read_bytes(), self.original)

    async def test_state_save_failure_restores_both_files(self):
        self.update.side_effect = RuntimeError("保存失败")
        with self.assertRaisesRegex(RuntimeError, "保存失败"):
            await replacement.replace_frame(self.folder)
        self.assertEqual(self.current.read_bytes(), self.original)
        self.assertEqual(self.canonical.read_bytes(), self.original)


class CandidateAndJudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_clothing_picker_rejects_no_choice_and_invalid_index(self):
        for index in (0, 2, True, "1"):
            with patch.object(batch_ai, "_chat_structured", new_callable=AsyncMock, return_value={"index": index, "reason": "没有合适的"}):
                with self.assertRaises(ValueError):
                    await batch_ai.choose_covered_frame([frame("red")])
        with patch.object(batch_ai, "_chat_structured", new_callable=AsyncMock, return_value={"index": 1, "reason": "覆盖完整"}):
            self.assertEqual(await batch_ai.choose_covered_frame([frame("blue")]), 1)

    async def test_candidates_exclude_current_history_and_near_duplicates(self):
        old, previous, alternate = frame("red"), frame("green"), frame("blue")
        with patch.object(worker, "_video_duration_seconds", return_value=12), \
             patch.object(worker, "_extract_frame_bytes", side_effect=[old, previous, alternate] * 8):
            result = replacement._candidates(Path("source.mp4"), [old, previous])
        self.assertEqual(result, [alternate])
