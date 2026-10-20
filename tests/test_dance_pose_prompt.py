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

    def test_singing_closeup_does_not_require_offscreen_limbs(self):
        for source in ('video', 'redesign'):
            text = compose_image_prompt('singing', style_source=source,
                                        song_name='测试歌曲', song_mood='温柔',
                                        feedback='换成蓝色上衣')
            self.assertIn('正脸或轻微侧转', text)
            self.assertIn('手在画外时不补入', text)
            self.assertIn('仅对画面内可见手部', text)
            self.assertNotIn('双臂与躯干边界可辨', text)
            self.assertNotIn('活动空地', text)
            self.assertNotIn('接触阴影', text)
            self.assertIn('脸部高度（发际线到下巴）约占画面高度 50%~55%', text)
            self.assertIn('《测试歌曲》', text)
            self.assertIn('歌曲情绪与氛围：温柔', text)
            self.assertIn('【本次修改】换成蓝色上衣', text)
            self.assertIn('眼形、眼距、鼻形、唇形、脸型及五官相对比例', text)
            self.assertTrue(text.endswith('不照搬图二的发型、服装或背景。'))

    def test_dance_preserves_crop_and_conditional_ground_shadow(self):
        text = compose_image_prompt('dance')
        self.assertIn('画面内可见的手臂与躯干边界清楚', text)
        self.assertIn('只在脚部与地面入镜时', text)
        self.assertIn('不为展示脚部改变既定裁切', text)
        self.assertIn('下边缘切至大腿上部', text)
        self.assertIn('服装覆盖、构图与背景重设计以文字要求为准', text)
        self.assertNotIn('唱歌以脸部为重点', text)
