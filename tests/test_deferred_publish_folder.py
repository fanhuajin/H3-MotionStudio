import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend import app, batch_worker as worker


class DeferredPublishFolderTests(unittest.TestCase):
    def test_waiting_materials_are_staged_then_only_selected_item_is_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "person.png"
            Image.new("RGB", (64, 64)).save(source)
            rows = [{"id": key, "index": index, "kind": "dance", "awemeId": key,
                     "status": "confirmed", "ai": {"title": "快乐舞蹈", "reference_image_path": str(source)}}
                    for index, key in enumerate(("a", "b"), 1)]
            state = {"status": "completed", "items": rows}
            def get_item(batch, key):
                return next(row for row in rows if row["id"] == key)
            def set_item(batch, key, **values):
                get_item(batch, key).update(values)
            with patch.object(worker, "DATA_DIR", root / "data"), \
                 patch.object(worker, "BATCH_OUTPUT_ROOT", root / "publish"), \
                 patch.object(worker, "_item", side_effect=get_item), \
                 patch.object(worker, "_set_item", side_effect=set_item), \
                 patch.object(worker.batch_store, "add_item_log"), \
                 patch.object(worker, "_item_frame", return_value=None), \
                 patch.object(app, "_batch_or_404", return_value=state), \
                 patch.object(app.batch_store, "update"), \
                 patch.object(worker.batch_store, "mutate", side_effect=lambda b, change: change(state)), \
                 patch.object(app, "_resume_batch", return_value=state):
                for row in rows:
                    result = worker.deliver_review_materials("batch", row["id"])
                    self.assertTrue(Path(result["copy"]).is_file())
                    self.assertTrue(worker._prep_assets_ready(row))
                    self.assertNotIn("folder", row.get("outputs", {}))
                    worker._sync_publish_images("batch", row["id"])
                    worker._expose_failed_item_in_image_workbench("batch", row["id"])
                self.assertFalse((root / "publish").exists())
                asyncio.run(app.start_batch_render("batch", app.BatchItemIdsRequest(itemIds=["a"])))
                self.assertFalse((root / "publish").exists())
                self.assertTrue(rows[0]["renderQueued"])
                # 排队响应不复制素材；视频 runner 轮到本条时才正式落盘。
                worker.deliver_review_materials("batch", "a")
                folder = Path(rows[0]["outputs"]["folder"])
                self.assertTrue((folder / "人物图.png").is_file())
                self.assertTrue((folder / "发布文案.txt").is_file())
                self.assertTrue(rows[0]["renderQueued"])
                self.assertNotIn("outputs", rows[1])
                self.assertEqual(list((root / "publish" / "dance").iterdir()), [folder])
                self.assertTrue(worker._prep_assets_ready(rows[1]))

    def test_waiting_does_not_modify_existing_publish_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "publish" / "dance" / "001_跳舞_旧标题_a"
            existing.mkdir(parents=True)
            old_copy = existing / "发布文案.txt"
            old_copy.write_text("旧作品文案", encoding="utf-8")
            row = {"id": "a", "index": 1, "kind": "dance", "awemeId": "a", "status": "confirmed",
                   "outputs": {"folder": str(existing)}, "ai": {"title": "新标题"}}
            with patch.object(worker, "DATA_DIR", root / "data"), \
                 patch.object(worker, "BATCH_OUTPUT_ROOT", root / "publish"), \
                 patch.object(worker, "_item", return_value=row), \
                 patch.object(worker, "_set_item", side_effect=lambda b, i, **values: row.update(values)), \
                 patch.object(worker.batch_store, "add_item_log"), \
                 patch.object(worker, "_item_frame", return_value=None):
                worker.deliver_review_materials("batch", "a")
            self.assertEqual(old_copy.read_text(encoding="utf-8"), "旧作品文案")
            self.assertTrue(existing.is_dir())
            self.assertTrue(Path(row["prepOutputs"]["copy"]).is_file())
