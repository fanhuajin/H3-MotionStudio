from __future__ import annotations

import unittest

from scripts.lyrics_force_align import map_tokens_to_lines, normalize_text


class LyricsForceAlignTests(unittest.TestCase):
    def test_normalize_removes_spaces_and_punctuation(self) -> None:
        self.assertEqual(normalize_text("偷偷的，在思念！"), "偷偷的在思念")

    def test_maps_funasr_character_timestamps_back_to_lines(self) -> None:
        lines = [
            {"index": 16, "orig": "恋人怀中樱花草"},
            {"index": 17, "orig": "听见胸膛心在跳"},
            {"index": 18, "orig": "偷偷的 在思念"},
            {"index": 19, "orig": "那是我们相爱的 记号"},
        ]
        text = "".join(normalize_text(line["orig"]) for line in lines)
        tokens = list(text)
        # 失败样本的关键边界：第四句应在 11.41s，而不是旧逻辑的 9.49s。
        starts = [
            230, 890, 1150, 1590, 2090, 2550, 3050,
            3950, 4590, 4870, 5350, 5830, 6270, 6730,
            7630, 8070, 8410, 9530, 9970, 10290,
            11410, 12030, 12390, 12810, 13290, 13750, 14250, 15570, 16050,
        ]
        timestamps = [[start, start + 240] for start in starts]

        aligned = map_tokens_to_lines(lines, tokens, timestamps)

        self.assertEqual([item["index"] for item in aligned], [16, 17, 18, 19])
        self.assertEqual([item["start"] for item in aligned], [0.23, 3.95, 7.63, 11.41])
        self.assertAlmostEqual(aligned[-1]["end"], 16.29, places=2)

    def test_rejects_model_text_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "文本不一致"):
            map_tokens_to_lines(
                [{"index": 1, "orig": "正确歌词"}],
                list("错误歌词"),
                [[0, 200], [200, 400], [400, 600], [600, 800]],
            )


if __name__ == "__main__":
    unittest.main()
