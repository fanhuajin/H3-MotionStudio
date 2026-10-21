import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path
from xiaohongshu_auto import XiaohongshuDraft


class ControlsTests(unittest.TestCase):
    def test_upload_readiness_uses_visible_submit_button_without_scrolling(self):
        draft=object.__new__(XiaohongshuDraft)
        draft.form_find=Mock();draft.scroll_form=Mock()
        draft.now=Mock(side_effect=[0,1]);draft.visible=lambda c:True
        draft.controls=lambda:[SimpleNamespace(Name="定时发布",ControlTypeName="ButtonControl",IsEnabled=True)]
        draft.wait_uploaded()
        draft.form_find.assert_not_called();draft.scroll_form.assert_not_called()

    def test_disabled_submit_waits_in_place_until_ready(self):
        draft=object.__new__(XiaohongshuDraft)
        draft.now=Mock(side_effect=[0,1,2]);draft.visible=lambda c:True
        draft.controls=Mock(side_effect=[
            [SimpleNamespace(Name="定时发布",ControlTypeName="ButtonControl",IsEnabled=False)],
            [SimpleNamespace(Name="定时发布",ControlTypeName="ButtonControl",IsEnabled=True)]])
        draft.scroll_form=Mock()
        with patch("xiaohongshu_auto.time.sleep"):
            draft.wait_uploaded()
        self.assertEqual(draft.controls.call_count,2)
        draft.scroll_form.assert_not_called()

    def test_matching_schedule_is_preserved_without_opening_calendar(self):
        from datetime import datetime,timedelta
        from bilibili_auto import CHINA_TIME
        selected=(datetime.now(CHINA_TIME)+timedelta(days=1)).replace(hour=16,minute=0)
        draft=object.__new__(XiaohongshuDraft)
        label=SimpleNamespace(BoundingRectangle=SimpleNamespace(top=100,bottom=120))
        actual=selected.replace(minute=30)
        entry=SimpleNamespace(ControlTypeName="EditControl",
            BoundingRectangle=SimpleNamespace(top=130,width=lambda:400),
            GetValuePattern=lambda:SimpleNamespace(Value=actual.strftime("%Y-%m-%d %H:%M")))
        draft.form_find=Mock(return_value=label);draft.controls=lambda:[entry]
        draft.visible=lambda c:True;draft.click=Mock()
        draft.schedule(selected)
        self.assertEqual(draft.actual_publish_at.strftime("%Y-%m-%d %H:%M"),actual.strftime("%Y-%m-%d %H:%M"))
        draft.click.assert_not_called()

    def test_schedule_label_scrolls_past_same_named_submit_button(self):
        draft=object.__new__(XiaohongshuDraft)
        button=SimpleNamespace(Name="定时发布",ControlTypeName="ButtonControl")
        label=SimpleNamespace(Name="定时发布",ControlTypeName="TextControl")
        draft.check=Mock();draft.visible=lambda c:True
        draft.controls=Mock(side_effect=[[button],[button,label]])
        draft.scroll_form=Mock()
        self.assertIs(draft.form_find("定时发布"),label)
        draft.scroll_form.assert_called_once_with("down",4)

    def test_existing_body_editor_exposed_as_edit_is_recognized(self):
        from focus_detection import classify_field
        observation={"controls":[{"focused":True,"focus_origin":True,
            "control_type":"ControlType.Edit","class_name":"tiptap ProseMirror ProseMirror-focused",
            "text_read_only":False}]}
        self.assertEqual(classify_field("xiaohongshu",observation),"description")
        observation["controls"][0]["class_name"]=""
        with self.assertRaises(ValueError):
            classify_field("xiaohongshu",observation)

    def test_declaration_scrolls_clear_of_fixed_footer_before_click(self):
        draft=object.__new__(XiaohongshuDraft)
        covered=SimpleNamespace(BoundingRectangle=SimpleNamespace(bottom=1350))
        visible=SimpleNamespace(BoundingRectangle=SimpleNamespace(bottom=1000))
        draft.window=SimpleNamespace(BoundingRectangle=SimpleNamespace(bottom=1380))
        draft.check=Mock();draft.locate=Mock(side_effect=[covered,visible])
        draft.scroll_form=Mock()
        self.assertIs(draft.form_find("添加内容类型声明"),visible)
        draft.scroll_form.assert_called_once_with("down",4)

    def test_declaration_menu_text_is_not_mistaken_for_saved_value(self):
        draft=object.__new__(XiaohongshuDraft)
        label=SimpleNamespace(BoundingRectangle=SimpleNamespace(left=100,top=100,right=600,bottom=156))
        menu=SimpleNamespace(Name="笔记含AI合成内容",BoundingRectangle=SimpleNamespace(left=120,top=200,right=300,bottom=230))
        saved=SimpleNamespace(Name="笔记含AI合成内容",BoundingRectangle=SimpleNamespace(left=120,top=110,right=300,bottom=135))
        draft.form_find=lambda name:label;draft.locate=lambda name:label
        draft.controls=Mock(side_effect=[[menu],[saved]])
        draft.visible=lambda c:True;draft.wait_name=lambda name:menu
        draft.click=Mock();draft.check=Mock();draft.now=Mock(side_effect=[0,1])
        draft.set_declaration()
        self.assertEqual(draft.click.call_count,2)

    def test_saved_declaration_is_not_selected_again(self):
        draft=object.__new__(XiaohongshuDraft)
        label=SimpleNamespace(BoundingRectangle=SimpleNamespace(left=100,top=100,right=600,bottom=156))
        saved=SimpleNamespace(Name="笔记含AI合成内容",BoundingRectangle=SimpleNamespace(left=120,top=110,right=300,bottom=135))
        draft.form_find=lambda name:label;draft.locate=lambda name:label;draft.controls=lambda:[saved]
        draft.visible=lambda c:True;draft.click=Mock()
        draft.set_declaration()
        draft.click.assert_not_called()

    def test_saved_declaration_replaces_placeholder_on_resume(self):
        draft=object.__new__(XiaohongshuDraft)
        saved=SimpleNamespace(Name="笔记含AI合成内容",
            BoundingRectangle=SimpleNamespace(left=100,top=100,right=600,bottom=156))
        draft.locate=lambda name,editable=False:saved if name==saved.Name else None
        draft.controls=lambda:[saved];draft.visible=lambda c:True
        draft.check=Mock();draft.click=Mock();draft.scroll_form=Mock()
        draft.set_declaration()
        draft.click.assert_not_called();draft.scroll_form.assert_not_called()

    def test_correct_title_skips_f6(self):
        draft=object.__new__(XiaohongshuDraft)
        control=SimpleNamespace(GetValuePattern=lambda:SimpleNamespace(Value="早上好～ "))
        draft.keyboard_field=Mock(); draft.click=Mock()
        draft.keyboard_fill(control,"title","早上好～")
        draft.keyboard_field.assert_not_called(); draft.click.assert_not_called()

    def test_title_waits_for_input_focus_before_f6(self):
        draft=object.__new__(XiaohongshuDraft)
        control=SimpleNamespace(GetValuePattern=lambda:SimpleNamespace(Value=""))
        hint="填写标题会有更多赞哦"
        draft.form_find=Mock(return_value=control); draft.click=Mock(); draft.check=Mock()
        draft.now=Mock(side_effect=range(10))
        focused=SimpleNamespace(Name=hint,ControlTypeName="EditControl",AutomationId="")
        draft.uia=SimpleNamespace(GetFocusedControl=Mock(side_effect=[None,focused]))
        draft.keyboard_field=Mock(); draft.verify_edit_value=Mock()
        with patch("xiaohongshu_auto.time.sleep"):
            draft.keyboard_fill(control,"title","早上好～")
        draft.keyboard_field.assert_called_once_with("title")

    def test_description_wrong_focus_prevents_clear(self):
        draft=object.__new__(XiaohongshuDraft)
        draft.window=SimpleNamespace(NativeWindowHandle=123)
        draft.click=Mock(); draft.keys=Mock()
        with patch("focus_detection.focused_field",return_value="title"):
            with self.assertRaisesRegex(RuntimeError,"未清空"):
                draft.focus_description(object())
        draft.keys.assert_not_called()

    def test_cover_clicks_as_soon_as_upload_and_button_are_ready(self):
        draft=object.__new__(XiaohongshuDraft)
        button=SimpleNamespace(IsEnabled=True)
        draft.now=Mock(side_effect=[0,0.1,0.2,0.3])
        draft.check=Mock(); draft.click=Mock()
        draft.locate=Mock(side_effect=[button,None]);draft.uploaded_cover=Mock(return_value=object())
        with patch("xiaohongshu_auto.time.sleep"):
            draft.finish_cover()
        draft.click.assert_called_once_with(button)

    def test_cover_does_not_click_disabled_button(self):
        draft=object.__new__(XiaohongshuDraft)
        disabled=SimpleNamespace(IsEnabled=False); ready=SimpleNamespace(IsEnabled=True)
        draft.now=Mock(side_effect=[0,0.1,0.2,0.3,0.4])
        draft.check=Mock(); draft.click=Mock()
        draft.locate=Mock(side_effect=[disabled,ready,None]);draft.uploaded_cover=Mock(return_value=object())
        with patch("xiaohongshu_auto.time.sleep"):
            draft.finish_cover()
        draft.click.assert_called_once_with(ready)

    def test_missing_control_has_actionable_error(self):
        draft=object.__new__(XiaohongshuDraft)
        with self.assertRaisesRegex(RuntimeError,"定时发布开关"):
            draft.require_control([],lambda c:True,"定时发布开关")

    def test_upload_entry_uses_verified_button(self):
        draft=object.__new__(XiaohongshuDraft)
        draft.select_browser=Mock(); draft.locate=Mock(return_value=None)
        draft.controls=lambda:[]; draft.next_upload=Mock()
        entry=SimpleNamespace(Name="上传视频",ControlTypeName="ButtonControl")
        draft.wait_upload_button=Mock(return_value=entry)
        draft.wait_name=Mock(side_effect=RuntimeError("停止在上传后"))
        draft.click=Mock(); draft.choose_file=Mock()
        with self.assertRaisesRegex(RuntimeError,"停止在上传后"):
            draft.run(Path("work/video.mp4"),"标题","正文",[],None,None,None)
        draft.click.assert_called_once_with(entry)
        draft.choose_file.assert_called_once_with(Path("work/video.mp4"))

    def test_upload_tab_text_is_not_upload_button(self):
        draft=object.__new__(XiaohongshuDraft)
        text=SimpleNamespace(Name="上传视频",ControlTypeName="TextControl")
        draft.controls=lambda:[text]; draft.visible=lambda c:True
        self.assertIsNone(draft.upload_button())

    def test_success_countdown_waits_without_clicking_navigation(self):
        draft=object.__new__(XiaohongshuDraft)
        draft.upload_button=Mock(return_value=None)
        draft.control_names=Mock(return_value=["发布成功", "3秒后跳转"])
        draft.click=Mock(); draft.wait_upload_button=Mock()
        draft.next_upload()
        draft.click.assert_not_called()
        draft.wait_upload_button.assert_called_once()

    def test_waits_through_transition_until_button_enabled(self):
        draft=object.__new__(XiaohongshuDraft)
        button=object()
        draft.now=Mock(side_effect=[0,1,2,3]); draft.check=Mock()
        draft.upload_button=Mock(side_effect=[None,None,button])
        with patch("xiaohongshu_auto.time.sleep"):
            self.assertIs(draft.wait_upload_button(),button)


if __name__ == "__main__":
    unittest.main()
