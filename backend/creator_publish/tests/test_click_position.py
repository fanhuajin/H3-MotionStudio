import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from bilibili_auto import BilibiliDraft, ControlSnapshot


def rectangle(y):
    return SimpleNamespace(left=200,right=240,top=y,bottom=y+20)


class ClickTests(unittest.TestCase):
    def test_click_uses_updated_live_position(self):
        live=SimpleNamespace(Name="箭头",ControlTypeName="TextControl",
                             BoundingRectangle=rectangle(300),IsOffscreen=False)
        snapshot=ControlSnapshot(live)
        live.BoundingRectangle=rectangle(400)
        draft=object.__new__(BilibiliDraft)
        draft.check=Mock(); draft.visible=lambda c:True
        draft.move_pointer=Mock(); draft.click_at=Mock()
        draft.uia=SimpleNamespace(GetCursorPos=lambda:(220,410))
        draft.click_declaration(snapshot)
        draft.click_at.assert_called_once_with((220,410))

    def test_cursor_mismatch_does_not_click(self):
        live=SimpleNamespace(BoundingRectangle=rectangle(300))
        draft=object.__new__(BilibiliDraft)
        draft.check=Mock(); draft.visible=lambda c:True
        draft.move_pointer=Mock(); draft.click_at=Mock()
        draft.uia=SimpleNamespace(GetCursorPos=lambda:(100,100))
        with self.assertRaisesRegex(RuntimeError,"鼠标未到达控件"):
            draft.click_declaration(live)
        draft.click_at.assert_not_called()

    def test_shared_click_refreshes_snapshot_position(self):
        live=SimpleNamespace(Name="正文",ControlTypeName="TextControl",
                             BoundingRectangle=rectangle(300),IsOffscreen=False)
        snapshot=ControlSnapshot(live)
        live.BoundingRectangle=rectangle(400)
        draft=object.__new__(BilibiliDraft)
        draft.check=Mock(); draft.visible=lambda c:True; draft.click_at=Mock()
        draft.move_pointer=Mock();draft.uia=SimpleNamespace(GetCursorPos=lambda:(220,410))
        draft.click(snapshot)
        draft.click_at.assert_called_once_with((220,410))


if __name__=="__main__":
    unittest.main()
