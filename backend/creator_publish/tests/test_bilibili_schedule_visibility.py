import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from bilibili_auto import BilibiliDraft


class ScheduleVisibilityTests(unittest.TestCase):
    def test_hour_option_must_fit_inside_scroll_column(self):
        draft=object.__new__(BilibiliDraft)
        viewport=NS(ClassName="time-picker-panel-select-wrp",
            BoundingRectangle=NS(left=100,top=200,right=175,bottom=450))
        row=NS(ClassName="time-picker-panel-select-item",GetParentControl=lambda:viewport)
        for top,bottom,expected in ((210,232,True),(440,462,False),(190,212,False)):
            option=NS(BoundingRectangle=NS(left=120,top=top,right=140,bottom=bottom),
                GetParentControl=lambda:row)
            self.assertEqual(draft.hour_option_in_view(option),expected)

    def test_hour_option_without_identified_column_is_rejected(self):
        draft=object.__new__(BilibiliDraft)
        option=NS(BoundingRectangle=NS(left=120,top=210,right=140,bottom=232))
        self.assertFalse(draft.hour_option_in_view(option))

    def test_bottom_layout_with_date_row_space_does_not_keep_scrolling(self):
        draft=object.__new__(BilibiliDraft)
        draft.window=NS(BoundingRectangle=NS(bottom=1398))
        label=NS(BoundingRectangle=NS(bottom=1110))
        draft.find=Mock(return_value=label); draft.scroll_page=Mock()
        self.assertIs(draft.reveal_schedule(),label)
        draft.scroll_page.assert_not_called()

    def test_clipped_date_row_still_scrolls(self):
        draft=object.__new__(BilibiliDraft)
        draft.window=NS(BoundingRectangle=NS(bottom=1398))
        low=NS(BoundingRectangle=NS(bottom=1350))
        ready=NS(BoundingRectangle=NS(bottom=1110))
        draft.find=Mock(side_effect=[low,ready]); draft.scroll_page=Mock()
        self.assertIs(draft.reveal_schedule(),ready)
        draft.scroll_page.assert_called_once_with("down",2)


if __name__ == "__main__":
    unittest.main()
