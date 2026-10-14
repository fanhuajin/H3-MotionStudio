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


if __name__ == "__main__":
    unittest.main()
