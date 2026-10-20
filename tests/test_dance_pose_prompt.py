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

    def test_clothing_matches_performance_kind(self):
        for source in ('video', 'redesign'):
            singing = compose_image_prompt('singing', style_source=source)
            self.assertIn('【唱歌着装】', singing)
            self.assertIn('随呼吸和轻微俯身保持稳定', singing)
            self.assertNotIn('【跳舞着装】', singing)
        dance = compose_image_prompt('dance')
        self.assertIn('【跳舞着装】', dance)
        self.assertIn('抬手、转身和弯腰', dance)
        self.assertNotIn('【唱歌着装】', dance)
        for text in (singing, dance):
            self.assertIn('腰腹与背部完整遮盖', text)
            self.assertIn('以下覆盖要求优先于参考图及造型修改意见', text)
            self.assertNotIn('无需改变正常的露肩', text)
