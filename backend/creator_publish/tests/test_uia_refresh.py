"""页面刷新、实时点击和独立菜单滚动的回归检查，不操作桌面。"""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
from bilibili_auto import BilibiliDraft,ControlSnapshot,COMError


def box(y=100):
    return NS(left=100,right=300,top=y,bottom=y+100,width=lambda:200,height=lambda:100)


class RefreshTests(unittest.TestCase):
    def test_class_name_is_read_once_before_node_disappears(self):
        class Node:
            Name="";ControlTypeName="GroupControl";BoundingRectangle=box();IsOffscreen=False
            reads=0
            @property
            def ClassName(self):
                self.reads+=1
                if self.reads>1:
                    raise COMError(-2147220991,"事件无法调用任何订户",None)
                return "drop-list-v2-content-wrp"
        live=Node();snapshot=ControlSnapshot(live)
        draft=object.__new__(BilibiliDraft);draft.visible=lambda c:True
        self.assertIs(draft.bilibili_category_menu([snapshot]),snapshot)
        self.assertEqual(live.reads,1)

    def test_tree_refresh_restarts_read_without_repeating_input(self):
        live=NS(Name="舞蹈",ControlTypeName="TextControl",BoundingRectangle=box(),IsOffscreen=False)
        draft=object.__new__(BilibiliDraft);draft.window=object();draft.check=Mock();draft.click=Mock()
        def changing_tree(*args,**kwargs):
            yield live,0
            raise COMError(-2147220991,"事件无法调用任何订户",None)
        draft.uia=NS(WalkControl=Mock(side_effect=[changing_tree(),iter([(live,0)])]))
        with patch("bilibili_auto.time.sleep"):
            controls=draft.controls()
        self.assertEqual([c.Name for c in controls],["舞蹈"])
        self.assertEqual(draft.uia.WalkControl.call_count,2)
        draft.click.assert_not_called()

    def test_stale_menu_option_parent_does_not_leak_com_error(self):
        popup=NS(Name="",ControlTypeName="GroupControl",ClassName="drop-list-v2-content-wrp",BoundingRectangle=box())
        option=NS(Name="舞蹈",ControlTypeName="TextControl",ClassName="",BoundingRectangle=box(120),
            GetParentControl=Mock(side_effect=COMError(-2147220991,"旧节点",None)))
        option.BoundingRectangle.bottom=180
        draft=object.__new__(BilibiliDraft);draft.controls=lambda:[popup,option];draft.visible=lambda c:True
        self.assertIsNone(draft.category_option("舞蹈"))

    def test_scroll_repositions_when_menu_moves_during_pointer_travel(self):
        live=NS(BoundingRectangle=box())
        draft=object.__new__(BilibiliDraft);draft.check=Mock();draft.visible=lambda c:True
        positions=iter([(200,150),(200,350)])
        draft.uia=NS(GetCursorPos=lambda:next(positions));draft.scroll_at=Mock()
        def move(point):
            live.BoundingRectangle=box(300)
        draft.move_pointer=Mock(side_effect=move)
        draft.scroll_in_control(live,"down",1)
        self.assertEqual(draft.move_pointer.call_count,2)
        draft.scroll_at.assert_called_once_with("down",1)


if __name__=="__main__":unittest.main()
