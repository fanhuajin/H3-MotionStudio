import unittest
from unittest.mock import Mock, patch
from bilibili_auto import BilibiliDraft


class EntryTests(unittest.TestCase):
    def draft(self):
        draft=object.__new__(BilibiliDraft)
        draft.uia=Mock(); draft.window=Mock()
        draft.locate=Mock(return_value=object()); draft.click=Mock()
        draft.check=Mock(); draft.now=Mock(side_effect=range(50))
        return draft

    def test_upload_page_needs_no_navigation(self):
        draft=self.draft()
        draft.ensure_upload_page("https://member.bilibili.com/platform/upload/video/frame")
        draft.click.assert_not_called()

    def test_manager_enters_video_upload_and_verifies_url(self):
        draft=self.draft()
        with patch("browser_page.read_browser_url",side_effect=[
            "https://member.bilibili.com/platform/upload-manager/article",
            "https://member.bilibili.com/platform/upload/video/frame"]),patch("bilibili_auto.time.sleep"):
            draft.ensure_upload_page("https://member.bilibili.com/platform/upload-manager/article")
        draft.click.assert_called_once()

    def test_unrelated_page_does_not_click(self):
        draft=self.draft()
        with self.assertRaises(RuntimeError):
            draft.ensure_upload_page("https://example.com/platform/upload-manager/article")
        draft.click.assert_not_called()

    def test_missing_entry_has_clear_error(self):
        draft=self.draft(); draft.locate.return_value=None
        with self.assertRaisesRegex(RuntimeError,"未找到.*投稿"):
            draft.ensure_upload_page("https://member.bilibili.com/platform/upload-manager/article")
        draft.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
