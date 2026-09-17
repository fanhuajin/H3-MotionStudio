from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend import image_workbench as workbench


def png(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (42, 56, 78)).save(output, "PNG")
    return output.getvalue()


class ImageWorkbenchTests(unittest.TestCase):
    def test_scan_recognizes_existing_outputs_without_regenerating(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            folder = root / "001_测试歌曲_123"
            folder.mkdir()
            (folder / "发布文案.txt").write_text("标题：测试歌曲\n简介：测试", encoding="utf-8")
            Image.new("RGB", (1200, 900)).save(folder / "人物图.png")
            Image.new("RGB", (1448, 1086)).save(folder / "B站4x3.png")
            with patch.object(workbench, "BATCH_OUTPUT_ROOT", root):
                rows = workbench.list_items()
            self.assertEqual(rows[0]["title"], "测试歌曲")
            self.assertIsNotNone(rows[0]["assets"]["person"])
            self.assertIsNotNone(rows[0]["assets"]["bilibili"])
            self.assertIsNone(rows[0]["assets"]["douyin"])

    def test_cover_is_normalized_to_exact_publish_size(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            saved = workbench.save_upload(folder, "douyin", png(900, 1200))
            self.assertEqual((saved["width"], saved["height"]), (1086, 1448))
            self.assertTrue((folder / "抖音3x4.png").is_file())

    def test_wrong_person_ratio_is_rejected_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            original = png(1200, 900)
            (folder / "人物图.png").write_bytes(original)
            with self.assertRaisesRegex(ValueError, "4:3"):
                workbench.save_upload(folder, "person", png(900, 1600), "singing")
            self.assertEqual((folder / "人物图.png").read_bytes(), original)

    def test_folder_id_cannot_escape_publish_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(workbench, "BATCH_OUTPUT_ROOT", Path(raw)):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    workbench.resolve_folder("2e2e2f2e2e")


if __name__ == "__main__":
    unittest.main()
