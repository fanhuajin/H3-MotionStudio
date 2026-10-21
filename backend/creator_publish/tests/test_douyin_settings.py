"""发布选项状态及从设置阶段续跑的离线验证，不发送输入。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from douyin_auto import DouyinDraft


class SettingsTests(unittest.TestCase):
    def test_hour_rejects_partially_clipped_loop_copy(self):
        from douyin_schedule import inside_time_list
        bounds=SimpleNamespace(left=100,top=875,right=277,bottom=1209)
        outer=SimpleNamespace(ControlTypeName="GroupControl",ClassName="semi-scrolllist-list-outer",BoundingRectangle=bounds)
        # The inner list is taller than its viewport, and UIA reports the clipped item visible.
        inner=SimpleNamespace(ControlTypeName="ListControl",ClassName="",BoundingRectangle=SimpleNamespace(left=100,top=30,right=277,bottom=2355),GetParentControl=lambda:outer)
        item=SimpleNamespace(BoundingRectangle=SimpleNamespace(left=100,top=840,right=277,bottom=885),GetParentControl=lambda:inner)
        self.assertFalse(inside_time_list(item))
        item.BoundingRectangle=SimpleNamespace(left=100,top=885,right=277,bottom=930)
        self.assertTrue(inside_time_list(item))

    def test_hour_column_excludes_identical_minute(self):
        draft=object.__new__(DouyinDraft); draft.visible=lambda c:True
        def item(x,name):
            return SimpleNamespace(Name=name,ControlTypeName="ListItemControl",
                BoundingRectangle=SimpleNamespace(left=x,top=300))
        hour=item(400,"02"); minute=item(500,"02")
        draft.controls=lambda:[minute,hour,item(400,"03"),item(500,"03")]
        self.assertEqual(draft.hour_options(SimpleNamespace(left=600,right=680,top=600)),
                         [hour,draft.controls()[-2]])

    def test_single_time_column_is_rejected(self):
        draft=object.__new__(DouyinDraft); draft.visible=lambda c:True
        draft.controls=lambda:[SimpleNamespace(Name="02",ControlTypeName="ListItemControl",
            BoundingRectangle=SimpleNamespace(left=500,top=300))]
        with self.assertRaisesRegex(RuntimeError,"两列"):
            draft.hour_options(SimpleNamespace(left=600,right=680,top=600))

    def test_find_returns_to_option_above_viewport(self):
        draft=object.__new__(DouyinDraft)
        rect=SimpleNamespace(left=500,top=50,right=540,bottom=70,
                             width=lambda:40,height=lambda:20)
        control=SimpleNamespace(Name="不允许",ControlTypeName="TextControl",BoundingRectangle=rect)
        draft.window=SimpleNamespace(BoundingRectangle=SimpleNamespace(
            left=0,top=0,right=1000,bottom=800))
        draft.controls=lambda:[control]
        draft.visible=Mock(side_effect=[False,True])
        draft.scroll_at=Mock()
        with patch("douyin_auto.time.sleep"):
            self.assertIs(draft.find("不允许"),control)
        draft.scroll_at.assert_called_once_with("up",1,(520,140))

    def test_find_scrolls_inside_form_column(self):
        draft=object.__new__(DouyinDraft)
        rect=SimpleNamespace(left=500,top=900,right=540,bottom=920,
                             width=lambda:40,height=lambda:20)
        control=SimpleNamespace(Name="不允许",ControlTypeName="TextControl",BoundingRectangle=rect)
        draft.window=SimpleNamespace(BoundingRectangle=SimpleNamespace(
            left=0,top=0,right=1000,bottom=800))
        draft.controls=lambda:[control]
        draft.visible=Mock(side_effect=[False,True])
        draft.scroll_at=Mock()
        with patch("douyin_auto.time.sleep"):
            self.assertIs(draft.find("不允许"),control)
        draft.scroll_at.assert_called_once_with("down",1,(520,720))

    def draft(self):
        draft = object.__new__(DouyinDraft)
        draft.find = Mock()
        draft.visible = lambda c: True
        draft.now = Mock(side_effect=range(20))
        return draft

    def option(self, states):
        box = SimpleNamespace(left=10, top=10, right=20, bottom=20)
        return SimpleNamespace(Name="不允许", ControlTypeName="CheckBoxControl",
                               BoundingRectangle=box,
                               GetTogglePattern=Mock(side_effect=[SimpleNamespace(ToggleState=s) for s in states]))

    def test_waits_for_selected_state(self):
        draft=self.draft(); control=self.option([0,0,1])
        draft.controls=lambda:[control]; draft.click=Mock()
        with patch("douyin_auto.time.sleep"):
            draft.disable_saving()
        draft.click.assert_called_once_with(control)

    def test_does_not_toggle_selected_option(self):
        draft=self.draft(); control=self.option([1])
        draft.controls=lambda:[control]; draft.click=Mock()
        draft.disable_saving()
        draft.click.assert_not_called()

    def test_resume_skips_upload_and_covers(self):
        draft=self.draft(); draft.resume_stage="settings"
        draft.select_browser=Mock()
        existing=SimpleNamespace(Name="填写作品标题，为作品获得更多流量",ControlTypeName="EditControl",
            BoundingRectangle=SimpleNamespace(left=100,top=-100,right=600,bottom=-60),
            GetValuePattern=lambda:SimpleNamespace(Value="标题"))
        draft.controls=lambda:[existing]
        draft.locate=Mock(return_value=None)
        draft.control_names=lambda:["内容由AI生成"]
        draft.finish_publish=Mock(); draft.upload=Mock(); draft.vertical_cover=Mock()
        draft.run(None,"标题","正文",[],None,None,None)
        draft.finish_publish.assert_called_once_with("标题",None)
        draft.upload.assert_not_called(); draft.vertical_cover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
