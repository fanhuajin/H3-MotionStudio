"""重放抖音/小红书保留稿件、部分正文、日历动画和封面加载边界。"""
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from bilibili_auto import CHINA_TIME
from calendar_navigation import select_calendar_month
from douyin_auto import DouyinDraft
from xiaohongshu_auto import XiaohongshuDraft


def control(name="", kind="TextControl", x=100, y=200, width=100, classes="", enabled=True):
    return NS(Name=name, ControlTypeName=kind, ClassName=classes, AutomationId="",
        IsEnabled=enabled, IsOffscreen=False,
        BoundingRectangle=NS(left=x, top=y, right=x+width, bottom=y+24,
            width=lambda:width, height=lambda:24))


class CalendarNavigationTests(unittest.TestCase):
    def draft(self):
        return NS(check=Mock(), now=time.monotonic, click=Mock())

    def test_delayed_next_month_is_clicked_once(self):
        d=self.draft(); button=object()
        read=Mock(side_effect=[(2026, 10, "old"), (2026, 10, "old"),
            None, (2026, 10, "old"), (2026, 11, "new")])
        navigation=Mock(return_value=button)
        with patch("calendar_navigation.time.sleep"):
            result=select_calendar_month(d, datetime(2026,11,1), read, navigation, "日历")
        self.assertEqual(result,(2026,11,"new"))
        navigation.assert_called_once_with("next");d.click.assert_called_once_with(button)

    def test_previous_month_across_year_uses_observed_navigation(self):
        d=self.draft(); navigation=Mock(return_value="previous-button")
        with patch("calendar_navigation.time.sleep"):
            result=select_calendar_month(d,datetime(2026,12,31),
                Mock(side_effect=[(2027,1,"jan"),(2027,1,"jan"),(2026,12,"dec")]),navigation,"日历")
        self.assertEqual(result,(2026,12,"dec"))
        navigation.assert_called_once_with("previous");d.click.assert_called_once_with("previous-button")

    def test_initial_render_waits_without_navigation(self):
        d=self.draft(); navigation=Mock()
        with patch("calendar_navigation.time.sleep"):
            self.assertEqual(select_calendar_month(d,datetime(2026,10,14),
                Mock(side_effect=[None,None,(2026,10,"header")]),navigation,"日历"),
                (2026,10,"header"))
        d.click.assert_not_called();navigation.assert_not_called()

    def test_missing_previous_button_does_not_click(self):
        d=self.draft()
        with self.assertRaisesRegex(RuntimeError,"上月按钮"):
            select_calendar_month(d,datetime(2026,10,14),lambda:(2026,11,"header"),lambda direction:None,"日历")
        d.click.assert_not_called()

    def test_stalled_transition_does_not_click_again(self):
        d=self.draft();d.now=Mock(side_effect=[0,0,0,0,6])
        with patch("calendar_navigation.time.sleep"),self.assertRaisesRegex(RuntimeError,"未重复点击"):
            select_calendar_month(d,datetime(2026,11,1),lambda:(2026,10,"old"),lambda direction:"next","日历")
        d.click.assert_called_once_with("next")

    def test_both_platform_schedules_wait_for_delayed_month_before_selecting_day(self):
        for cls in (DouyinDraft,XiaohongshuDraft):
            with self.subTest(platform=cls.__name__):
                self.replay_schedule(cls)

    def replay_schedule(self, cls):
        d=object.__new__(cls);d.check=Mock();d.now=time.monotonic;d.visible=lambda c:True
        selected=(datetime.now(CHINA_TIME)+timedelta(days=1)).replace(hour=6,minute=0,second=0,microsecond=0)
        previous=selected.replace(day=1)-timedelta(days=1)
        label=control("定时发布",y=50)
        entry=control("日期和时间",kind="EditControl",y=90,width=400)
        entry.GetValuePattern=lambda:NS(Value=previous.replace(hour=12,minute=17).strftime("%Y-%m-%d %H:%M"))
        button=control("下个月",kind="ButtonControl")
        start=selected.replace(day=1)-timedelta(days=selected.replace(day=1).weekday())
        days=[control(str((start+timedelta(days=i)).day),x=105+i%7*30,y=200+i//7*30,width=24) for i in range(42)]
        state={"navigation":False,"reads":0}
        def controls():
            if state["navigation"]:
                state["reads"]+=1
            date=selected if state["reads"]>=3 else previous
            if cls is DouyinDraft:
                headers=[control(f"{date.year}年{date.month}月",x=220,y=100,width=120)]
            else:
                headers=[control(f"{date.year}年",x=200,y=110),control(f"{date.month}月",x=310,y=110)]
            return [label,entry,button]+headers+days
        def click(c):
            if c is button:
                self.assertFalse(state["navigation"],"不得在月份动画期间重复点击")
                state["navigation"]=True
            elif c in days:
                raise RuntimeError("停止于已定位日期")
        d.controls=controls;d.click=Mock(side_effect=click);d.form_find=lambda name:label
        d.find=lambda name,editable=False:entry
        d.disable_saving=Mock();d.select_option=Mock()
        with patch("calendar_navigation.time.sleep"),self.assertRaisesRegex(RuntimeError,"停止于已定位日期"):
            d.schedule(selected)
        self.assertEqual(sum(call.args[0] is button for call in d.click.call_args_list),1)


class DraftRecoveryTests(unittest.TestCase):
    def test_douyin_publish_entry_prefers_panel_over_duplicate_sidebar(self):
        d=object.__new__(DouyinDraft);d.visible=lambda c:True
        sidebar=control("作品发布",kind="ButtonControl",x=35,width=190)
        menu=control("发布视频",x=86,y=293,width=71)
        panel=control("发布视频",x=305,y=267,width=90)
        d.controls=lambda:[sidebar,menu,panel]
        self.assertIs(d.video_publish_entry(),panel)

    def test_douyin_publish_entry_rejects_multiple_panel_entries(self):
        d=object.__new__(DouyinDraft);d.visible=lambda c:True
        d.controls=lambda:[control("作品发布",kind="ButtonControl",x=35,width=190),
            control("发布视频",x=305),control("发布视频",x=600)]
        with self.assertRaisesRegex(RuntimeError,"多个同名控件"):
            d.video_publish_entry()

    def test_douyin_publish_entry_accepts_single_menu_entry(self):
        d=object.__new__(DouyinDraft);d.visible=lambda c:True
        menu=control("发布视频",x=86)
        d.controls=lambda:[menu]
        self.assertIs(d.video_publish_entry(),menu)

    def draft(self, cls=DouyinDraft):
        d=object.__new__(cls);d.check=Mock();d.locate=Mock(return_value=None)
        d.visible=lambda c:True;d.now=time.monotonic
        return d

    def test_offscreen_matching_douyin_draft_resumes_settings_without_navigation(self):
        d=self.draft();d.select_browser=Mock();d.resume_stage="settings"
        title=control("填写作品标题，为作品获得更多流量",kind="EditControl",y=-130)
        title.GetValuePattern=lambda:NS(Value="标题")
        d.controls=lambda:[title];d.visible=lambda c:False
        d.control_names=lambda:["内容由AI生成"]
        d.finish_publish=Mock();d.upload=Mock();d.next_upload=Mock();d.vertical_cover=Mock()
        d.run(None,"标题","正文",[],None,None,None)
        d.finish_publish.assert_called_once_with("标题",None)
        d.next_upload.assert_not_called();d.upload.assert_not_called();d.vertical_cover.assert_not_called()

    def test_offscreen_other_draft_is_rejected_before_navigation(self):
        d=self.draft();d.select_browser=Mock();d.resume_stage="settings"
        title=control("填写作品标题，为作品获得更多流量",kind="EditControl",y=-130)
        title.GetValuePattern=lambda:NS(Value="另一稿件")
        d.controls=lambda:[title];d.upload=Mock();d.next_upload=Mock()
        with self.assertRaisesRegex(RuntimeError,"稿件与队列作品不一致"):
            d.run(None,"标题","正文",[],None,None,None)
        d.next_upload.assert_not_called();d.upload.assert_not_called()

    def test_partially_filled_editors_are_found_without_text_identity(self):
        for cls,classes in ((DouyinDraft,"zone-container editor-kit-container editor-comp-publish"),
                            (XiaohongshuDraft,"tiptap ProseMirror ProseMirror-focused")):
            with self.subTest(platform=cls.__name__):
                d=self.draft(cls);editor=control("",kind="EditControl",classes=classes)
                d.controls=lambda:[editor,control("部分正文 #第一个话题")]
                self.assertIs(d.description_editor("placeholder","完整正文"),editor)

    def test_unknown_edit_control_is_not_guessed_as_body(self):
        d=self.draft(XiaohongshuDraft);d.controls=lambda:[control("",kind="EditControl")]
        with self.assertRaisesRegex(RuntimeError,"未找到可确认的正文"):
            d.description_editor("placeholder","正文")

    def test_multiple_independent_editors_are_rejected(self):
        d=self.draft(XiaohongshuDraft)
        d.controls=lambda:[control("",kind="EditControl",classes="tiptap",y=200),
            control("",kind="EditControl",classes="tiptap",y=600)]
        with self.assertRaisesRegex(RuntimeError,"多个同名控件"):
            d.description_editor("placeholder","正文")

    def test_douyin_editor_wrong_focus_blocks_input(self):
        d=self.draft();d.window=NS(NativeWindowHandle=123);d.click=Mock();d.keys=Mock()
        with patch("focus_detection.focused_field",return_value="title"),self.assertRaisesRegex(RuntimeError,"未清空或输入"):
            d.focus_description(object())
        d.keys.assert_not_called()

    def test_xiaohongshu_offscreen_editor_is_relocated(self):
        d=self.draft(XiaohongshuDraft);editor=control("",kind="EditControl",classes="ProseMirror",y=-100)
        d.controls=lambda:[editor];d.visible=Mock(side_effect=[False,True])
        d.window=NS(BoundingRectangle=NS(top=0,bottom=800));d.scroll_form=Mock()
        self.assertIs(d.description_editor("placeholder","正文"),editor)
        d.scroll_form.assert_called_once_with("up",1)


class CoverReadinessTests(unittest.TestCase):
    def draft(self):
        d=object.__new__(DouyinDraft);d.now=time.monotonic;d.check=Mock();d.visible=lambda c:True
        return d

    def test_waits_until_disabled_button_is_enabled_and_stable(self):
        d=self.draft();disabled=control("完成",kind="ButtonControl",enabled=False)
        ready=control("完成",kind="ButtonControl")
        d.controls=Mock(side_effect=[[disabled],[ready],[ready]])
        with patch("douyin_auto.time.sleep"):
            self.assertIs(d.wait_cover_ready("完成"),ready)
        self.assertEqual(d.controls.call_count,3)

    def test_upload_indicator_blocks_ready_button(self):
        d=self.draft();button=control("完成",kind="ButtonControl")
        d.controls=Mock(side_effect=[[button,control("图片上传中")],[button],[button]])
        with patch("douyin_auto.time.sleep"):
            self.assertIs(d.wait_cover_ready("完成"),button)
        self.assertEqual(d.controls.call_count,3)

    def test_background_video_upload_does_not_block_cover_editor(self):
        d=self.draft();button=control("完成",kind="ButtonControl")
        d.controls=lambda:[button,control("视频上传中",y=600)]
        with patch("douyin_auto.time.sleep"):
            self.assertIs(d.wait_cover_ready("完成"),button)

    def test_generic_upload_status_is_scoped_to_observed_cover_dialog(self):
        d=self.draft();button=control("完成",kind="ButtonControl",x=300,y=300)
        popup=control("",kind="GroupControl",x=200,y=200,width=500,classes="semi-modal-content")
        popup.BoundingRectangle.bottom=550;popup.BoundingRectangle.height=lambda:350
        d.controls=Mock(side_effect=[
            [popup,button,control("上传中",x=300,y=250)],
            [popup,button,control("上传中",x=700,y=600)],
            [popup,button,control("上传中",x=700,y=600)]])
        with patch("douyin_auto.time.sleep"):
            self.assertIs(d.wait_cover_ready("完成"),button)
        self.assertEqual(d.controls.call_count,3)

    def test_upload_failure_stops_cover_completion(self):
        d=self.draft();d.controls=lambda:[control("封面上传失败")];d.click=Mock()
        with self.assertRaisesRegex(RuntimeError,"封面上传失败"):
            d.wait_cover_ready("完成")
        d.click.assert_not_called()

    def test_never_ready_button_times_out_without_click(self):
        d=self.draft();d.now=Mock(side_effect=[0,0,91]);d.controls=lambda:[control("完成",enabled=False)]
        d.click=Mock()
        with patch("douyin_auto.time.sleep"),self.assertRaisesRegex(RuntimeError,"等待超时"):
            d.wait_cover_ready("完成")
        d.click.assert_not_called()


if __name__=="__main__":
    unittest.main()
