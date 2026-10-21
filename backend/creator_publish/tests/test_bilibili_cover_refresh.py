import unittest
from _ctypes import COMError
from unittest.mock import Mock, patch
from bilibili_auto import BilibiliDraft
from douyin_auto import DouyinDraft
from xiaohongshu_auto import XiaohongshuDraft


class CoverRefreshTests(unittest.TestCase):
    def test_transient_tree_failure_rechecks_without_clicking(self):
        draft=object.__new__(BilibiliDraft)
        draft.now=Mock(side_effect=[0,1,2,3]); draft.check=Mock(); draft.click=Mock()
        draft.locate=Mock(side_effect=[COMError(-2147220991,"页面刷新",None),object(),None])
        with patch("bilibili_auto.time.sleep"):
            draft.wait_cover_closed()
        self.assertEqual(draft.locate.call_count,3)
        draft.click.assert_not_called()

    def test_persistent_tree_failure_stops(self):
        draft=object.__new__(BilibiliDraft)
        draft.now=Mock(side_effect=[0,1,11]); draft.check=Mock(); draft.click=Mock()
        draft.locate=Mock(side_effect=COMError(-2147220991,"页面刷新",None))
        with patch("bilibili_auto.time.sleep"),self.assertRaisesRegex(RuntimeError,"页面未稳定"):
            draft.wait_cover_closed()
        draft.click.assert_not_called()

    def test_platform_schedule_and_cover_paths_are_distinct(self):
        self.assertIsNot(DouyinDraft.schedule,BilibiliDraft.schedule)
        self.assertIsNot(XiaohongshuDraft.schedule,BilibiliDraft.schedule)
        self.assertNotIn("cover",DouyinDraft.run.__code__.co_names)
        self.assertIn("vertical_cover",DouyinDraft.run.__code__.co_names)
        self.assertTrue("wait_cover_closed" not in XiaohongshuDraft.set_cover.__code__.co_names)


if __name__ == "__main__":
    unittest.main()
