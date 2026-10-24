import unittest

from backend import batch_ai


class PositiveCopyTests(unittest.TestCase):
    def test_unsafe_source_cannot_return_through_fallback_or_tags(self):
        for kind in ("dance", "singing"):
            result = {"title": "来 杀 我", "introduction": "来杀我，想死了", "tags": ["来杀我", "想死", "卡点舞"], "song_name": "杀了我", "song_mood": "要命"}
            batch_ai.ensure_copy_fields(result, kind=kind, description="来杀我", source_tags=["自杀", "弄死"])
            self.assertFalse(batch_ai.has_disallowed_copy(result["title"]))
            self.assertFalse(batch_ai.has_disallowed_copy(result["introduction"]))
            self.assertEqual(len(result["tags"]), 5)
            self.assertTrue(all(not batch_ai.has_disallowed_copy(tag) for tag in result["tags"]))
            original = dict(result)
            batch_ai.ensure_copy_fields(result, kind=kind, description="来杀我")
            self.assertEqual(result, original)

    def test_positive_copy_is_preserved(self):
        result = {"title": "把快乐跳进节拍里", "introduction": "跟着轻快的节奏，跳出今天的好心情。", "tags": ["甜妹舞", "卡点舞", "手势舞", "快乐舞蹈", "舞蹈日常"]}
        original = dict(result)
        batch_ai.ensure_copy_fields(result, kind="dance")
        self.assertEqual(result, original)

    def test_minor_themed_clickbait_is_removed_from_all_copy_paths(self):
        for kind in ("dance", "singing"):
            for text in ("都说萝莉有三好", "都说蘿莉有三好", "萝 莉有三好", "ＬＯＬＩ", "都说loli有三好", "shota", "幼齿诱惑", "幼态擦边", "小学生性感舞蹈"):
                with self.subTest(kind=kind, text=text):
                    result = {"title": text, "introduction": text, "tags": [text], "song_name": text}
                    batch_ai.ensure_copy_fields(result, kind=kind, description=text, source_tags=[text])
                    self.assertFalse(batch_ai.has_disallowed_copy(result["title"]))
                    self.assertFalse(batch_ai.has_disallowed_copy(result["introduction"]))
                    self.assertEqual(len(result["tags"]), 5)
                    self.assertTrue(all(not batch_ai.has_disallowed_copy(tag) for tag in result["tags"]))
                    fallback = batch_ai.fallback_result(kind=kind, description=text, tags=[text])
                    self.assertFalse(batch_ai.has_disallowed_copy(fallback["title"]))

    def test_normal_cute_and_childhood_themes_are_preserved(self):
        for text in ("甜美可爱的舞蹈日常", "童年的旋律", "校园舞蹈", "儿童节快乐", "Lolita服饰分享"):
            self.assertFalse(batch_ai.has_disallowed_copy(text), text)

    def test_cover_rejects_unsafe_existing_copy(self):
        import tempfile
        from pathlib import Path
        from backend import image_workbench

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "发布文案.txt").write_text("标题：都说萝莉有三好\n简介：跟着节拍一起跳", encoding="utf-8")
            for stage in ("bilibili", "douyin"):
                with self.assertRaisesRegex(ValueError, "请先修改发布文案"):
                    image_workbench.prompt(folder, stage, "dance")


if __name__ == "__main__":
    unittest.main()
