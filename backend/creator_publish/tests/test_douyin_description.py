"""抖音连续作品简介定位与其他平台行为隔离，不发送系统输入。"""
import unittest
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch, call
from douyin_auto import DouyinDraft
from xiaohongshu_auto import XiaohongshuDraft


def editor():
    return NS(Name="", ControlTypeName="EditControl", AutomationId="",
              ClassName="zone-container editor-kit-container editor-comp-publish",
              BoundingRectangle=NS(left=100,top=200,right=700,bottom=400))


class DescriptionTests(unittest.TestCase):
    def draft(self, cls=DouyinDraft):
        d=object.__new__(cls)
        d.check=Mock();d.visible=lambda c:True
        d.window=NS(NativeWindowHandle=123)
        return d

    def test_two_works_use_current_editors_instead_of_placeholder(self):
        d=self.draft();first=editor();second=editor();hint=object()
        d.locate=Mock(return_value=hint)
        d.controls=Mock(side_effect=[[first],[second]])
        self.assertIs(d.description_editor("添加作品简介","第一条"),first)
        self.assertIs(d.description_editor("添加作品简介","第二条"),second)

    def test_second_work_refreshes_invisible_node_before_input(self):
        d=self.draft();old=object();new=object()
        d.description_editor=Mock(side_effect=[old,new])
        d.click=Mock(side_effect=[RuntimeError("目标控件已离开可见区域，停止输入。"),None])
        with patch("douyin_auto.time.sleep"),patch("focus_detection.focused_field",return_value="description"):
            d.focus_description(old,"第二条")
        self.assertEqual(d.click.call_args_list,[call(old),call(new)])

    def test_persistent_invisible_editor_stops_without_typing(self):
        d=self.draft();d.description_editor=Mock(return_value=object())
        d.click=Mock(side_effect=RuntimeError("目标控件已离开可见区域，停止输入。"))
        d.keys=Mock()
        with patch("douyin_auto.time.sleep"),self.assertRaisesRegex(RuntimeError,"未获得"):
            d.focus_description(object(),"正文")
        self.assertEqual(d.click.call_count,3);d.keys.assert_not_called()

    def test_window_change_is_not_retried(self):
        d=self.draft();d.description_editor=Mock(return_value=object())
        d.click=Mock(side_effect=RuntimeError("当前窗口发生变化"))
        with self.assertRaisesRegex(RuntimeError,"窗口发生变化"):
            d.focus_description(object(),"正文")
        self.assertEqual(d.click.call_count,1)

    def test_xiaohongshu_keeps_existing_placeholder_behavior(self):
        d=self.draft(XiaohongshuDraft);hint=object()
        d.locate=Mock(return_value=hint);d.controls=Mock()
        self.assertIs(d.description_editor("输入正文描述，真诚有价值的分享予人温暖","正文"),hint)
        d.controls.assert_not_called()

    def test_topic_lookup_uses_popup_and_excludes_unrelated_text(self):
        d=self.draft()
        popup=NS(Name="",ClassName="mention-suggest-mount-dom",ControlTypeName="GroupControl",
                 BoundingRectangle=NS(left=100,top=450,right=700,bottom=500))
        def text(name,x,y):
            return NS(Name=name,ClassName="",ControlTypeName="TextControl",
                      BoundingRectangle=NS(left=x,top=y,right=x+80,bottom=y+20))
        target=text("卡点舞",130,460)
        d.controls=lambda:[popup,target,text("卡点舞教程",250,460),text("卡点舞",900,460),
                           text("卡点舞",130,230)]
        self.assertIs(d.douyin_topic_control("卡点舞"),target)

    def topic_draft(self):
        d=self.draft();d.now=time.monotonic;d.keys=Mock();d.paste=Mock();d.verify_text=Mock();d.click=Mock()
        return d

    def test_topics_confirm_candidate_and_accepted_token_for_each_work(self):
        d=self.topic_draft();candidate=object();token=object()
        d.douyin_topic_control=Mock(side_effect=[candidate,candidate,candidate,token]*2)
        with patch("douyin_auto.time.sleep"),patch("focus_detection.focused_field",return_value="description"):
            d.fill_douyin_description("第一条",["卡点舞"])
            d.fill_douyin_description("第二条",["手势舞"])
        self.assertEqual(d.click.call_count,2)
        self.assertEqual(d.paste.call_args_list,[call("第一条 "),call("卡点舞"),call("第二条 "),call("手势舞")])
        self.assertNotIn(call(" "),d.keys.call_args_list)

    def test_missing_topic_blocks_click_and_later_topics(self):
        d=self.topic_draft();d.now=Mock(side_effect=range(20))
        d.douyin_topic_control=Mock(return_value=None)
        with patch("douyin_auto.time.sleep"),patch("focus_detection.focused_field",return_value="description"):
            with self.assertRaisesRegex(RuntimeError,"未找到同名候选"):
                d.fill_douyin_description("正文",["卡点舞","下一标签"])
        d.click.assert_not_called()
        self.assertNotIn(call("下一标签"),d.paste.call_args_list)

    def test_raw_hashtag_is_not_accepted_as_linked_topic(self):
        d=self.draft();body=editor()
        raw=NS(Name="#卡点舞",ClassName="",ControlTypeName="TextControl",
               BoundingRectangle=NS(left=130,top=230,right=250,bottom=250))
        d.controls=lambda:[body,raw]
        self.assertIsNone(d.douyin_topic_control("卡点舞",accepted=True))


if __name__ == "__main__":
    unittest.main()
