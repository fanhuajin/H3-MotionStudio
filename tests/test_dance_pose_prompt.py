import unittest
from backend.batch_ai import compose_image_prompt

class DancePosePromptTests(unittest.TestCase):
    def test_dance_requires_standing_at_each_ratio(self):
        for ratio in ('9:16', '4:3'):
            text = compose_image_prompt('dance', ratio=ratio)
            self.assertIn('默认保持自然站立', text)
            self.assertIn('不坐、不蹲、不跪', text)
            self.assertNotIn('姿态和人物位置可自然调整', text)
            self.assertTrue(text.endswith('不照搬图二的发型、服装或背景。'))

    def test_singing_retains_pose_freedom(self):
        for source in ('video', 'redesign'):
            text = compose_image_prompt('singing', style_source=source)
            self.assertIn('姿态和人物位置可自然调整', text)
            self.assertNotIn('【跳舞姿态】', text)
