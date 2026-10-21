import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from bilibili_auto import BilibiliDraft


class DeclarationTests(unittest.TestCase):
    def draft(self):
        draft=object.__new__(BilibiliDraft)
        draft.window=SimpleNamespace(BoundingRectangle=SimpleNamespace(top=0,bottom=900))
        draft.now=Mock(side_effect=range(20)); draft.click=Mock(); draft.scroll_page=Mock()
        draft.click_declaration=Mock()
        draft.controls=lambda:[]; draft.visible=lambda c:True
        return draft

    def test_observed_dropdown_arrow_is_preferred_over_input_center(self):
        draft=self.draft()
        def box(left,top,right,bottom):
            return SimpleNamespace(left=left,top=top,right=right,bottom=bottom,
                                   width=lambda:right-left,height=lambda:bottom-top)
        entry=SimpleNamespace(BoundingRectangle=box(1388,885,1791,925))
        arrow=SimpleNamespace(Name="\ue64b",ControlTypeName="TextControl",
                              BoundingRectangle=box(1763,901,1776,914))
        unrelated=SimpleNamespace(Name="提示",ControlTypeName="ImageControl",
                                  BoundingRectangle=box(1317,895,1336,914))
        draft.controls=lambda:[unrelated,arrow]
        self.assertIs(draft.declaration_opener(entry),arrow)

    def entry(self,top,value):
        return SimpleNamespace(Name="请选择符合您视频内容的创作声明",BoundingRectangle=SimpleNamespace(left=200,right=600,top=top,bottom=top+30),
            GetValuePattern=lambda:SimpleNamespace(Value=value))

    def test_open_menu_is_not_closed_by_clicking_entry(self):
        draft=self.draft(); option=object()
        draft.find=Mock(side_effect=[self.entry(300,""),self.entry(300,"含AI生成内容")])
        draft.locate=Mock(return_value=option)
        draft.declaration()
        draft.click.assert_called_once_with(option)

    def test_relocates_after_single_scroll(self):
        draft=self.draft(); option=object(); moved=self.entry(500,"")
        draft.find=Mock(side_effect=[self.entry(700,""),self.entry(500,"含AI生成内容")])
        draft.locate=Mock(side_effect=[None,moved]); draft.wait_name=Mock(return_value=option)
        draft.declaration()
        draft.scroll_page.assert_called_once_with("down",1)
        draft.click_declaration.assert_called_once_with(moved)

    def test_focused_input_can_open_menu_with_arrow(self):
        draft=self.draft(); entry=self.entry(300,""); option=object()
        draft.find=Mock(side_effect=[entry,self.entry(300,"含AI生成内容")])
        draft.locate=Mock(return_value=None)
        draft.wait_name=Mock(side_effect=[RuntimeError("等待“含AI生成内容”超时。"),option])
        draft.uia=SimpleNamespace(GetFocusedControl=lambda:entry); draft.keys=Mock()
        draft.declaration()
        draft.keys.assert_called_once_with("{Down}")

    def test_wrong_focus_does_not_send_arrow(self):
        draft=self.draft(); entry=self.entry(300,"")
        draft.find=Mock(return_value=entry); draft.locate=Mock(return_value=None)
        draft.wait_name=Mock(side_effect=RuntimeError("等待“含AI生成内容”超时。"))
        other=self.entry(300,""); other.Name="标题"
        draft.uia=SimpleNamespace(GetFocusedControl=lambda:other); draft.keys=Mock()
        draft.save_declaration_diagnostic=Mock()
        with self.assertRaisesRegex(RuntimeError,"焦点不在声明框"):
            draft.declaration()
        draft.keys.assert_not_called()


if __name__=="__main__":
    unittest.main()
