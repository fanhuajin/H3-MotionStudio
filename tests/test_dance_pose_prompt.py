import re
import unittest
from backend.batch_ai import compose_image_prompt

class DancePosePromptTests(unittest.TestCase):
    def test_generated_prompts_have_no_duplicate_sections_or_json(self):
        for kind, source in (('singing', 'video'), ('dance', 'video'), ('singing', 'redesign')):
            text = compose_image_prompt(kind, style_source=source)
            sections = re.findall(r'【([^】]+)】', text)
            self.assertEqual(len(sections), len(set(sections)))
            self.assertNotRegex(text, r'[{}]|JSON')
            self.assertLess(len(text), 1400)
            self.assertEqual(text.count('图二是唯一的面部身份参考'), 1)

    def test_video_appearance_follows_frame_without_changing_redesign(self):
        for kind in ('singing', 'dance'):
            text = compose_image_prompt(kind)
            self.assertIn('【发型与配饰】', text)
            self.assertIn('眼镜、发饰、帽子、耳环、项链', text)
            self.assertIn('未佩戴的配饰不凭空添加', text)
            self.assertIn('模糊或遮挡而无法辨认的细节不臆造', text)
            self.assertNotIn('发饰可有可无', text)
            self.assertNotIn('不固定沿用', text)
            self.assertIn('图二是唯一的面部身份参考', text)
        redesign = compose_image_prompt('singing', style_source='redesign')
        self.assertIn('根据歌曲氛围设计自然的发型', redesign)
        self.assertIn('发饰可有可无', redesign)
        self.assertNotIn('【发型与配饰】', redesign)

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

    def test_clothing_is_preserved_with_one_conditional_adjustment_rule(self):
        for kind in ('singing', 'dance'):
            text = compose_image_prompt(kind)
            self.assertEqual(text.count('【服装】'), 1)
            self.assertIn('裙子保持裙子，不改成裤子', text)
            self.assertIn('只有明确非常暴露时才处理', text)
            self.assertIn('拿不准时保持原样', text)
            self.assertNotIn('【唱歌着装】', text)
            self.assertNotIn('【跳舞着装】', text)
            self.assertNotIn('搭配长裤或', text)
        redesign = compose_image_prompt('singing', style_source='redesign')
        self.assertIn('根据歌曲氛围设计自然的发型、服装', redesign)
        self.assertNotIn('保留图一的服装类别', redesign)
        self.assertIn('只有明确非常暴露时才处理', redesign)

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
        self.assertNotIn('服装覆盖、构图与背景重设计以文字要求为准', text)
        self.assertNotIn('唱歌以脸部为重点', text)
