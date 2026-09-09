from __future__ import annotations

import unittest

from backend.lyrics_worker import align_line_times


def _phrase(text: str, start: float, step: float = 0.18) -> list[list[object]]:
    return [
        [char, start + index * step, start + (index + 1) * step]
        for index, char in enumerate(text)
    ]


class LyricsAlignmentTests(unittest.TestCase):
    def test_context_recovers_weak_repeated_lines_and_corrupt_head(self) -> None:
        first_pass = [
            (5.13, "只是你太粗心大意忽略了我的感受"),
            (9.78, "只是我太执着在意拥有你给的温柔"),
            (12.81, "你的借口理由我照单全收"),
            (19.71, "如果说是我太过迁就所以沦为爱囚"),
            (23.34, "活该我独自承受独自寂寞转身怀旧"),
            (27.30, "真心付出不够不适合厮守"),
        ]
        # 每句在官方歌词里都有第二轮，因此单靠“正文是否唯一”无法锚定。
        lyric_lines = [
            {"time": time, "orig": text, "zh": ""} for time, text in first_pass
        ] + [
            {"time": time + 134.0, "orig": text, "zh": ""} for time, text in first_pass
        ]

        words: list[list[object]] = []
        words += _phrase("这是你太做心爱忽灭了我的单说", 0.00)
        words += _phrase("这是我太承受在依律有命给的没有", 4.46)
        words += _phrase("你的借口唯有我找答的取胸", 7.92)
        words += _phrase("如果说是我太过迁就所以沦为爱囚", 13.36)
        words += _phrase("我不赖我独自承受独自寂寞转身怀旧", 19.22)
        words += _phrase("真心付出不够不适合厮守", 23.54)
        # faster-whisper 的典型片尾复读：文字很多，但所有时间戳都退化到同一点。
        words += [[char, 29.16, 29.16] for char in "如果说是我太过迁就所以沦为爱囚"]
        asr = {
            "duration": 29.187,
            "segments": [{"start": 0.0, "end": 29.16, "words": words}],
        }

        times, matched, last_vocal, anchors = align_line_times(asr, lyric_lines)

        self.assertEqual(matched, 6)
        self.assertEqual(anchors, set(range(6)))
        expected = [0.00, 4.46, 7.92, 13.36, 19.22, 23.54]
        for actual, wanted in zip(times[:6], expected):
            self.assertIsNotNone(actual)
            self.assertAlmostEqual(float(actual), wanted, delta=0.2)
        self.assertLess(last_vocal, 29.0)
        self.assertTrue(all(value is None or value >= 100 for value in times[6:]))


if __name__ == "__main__":
    unittest.main()
