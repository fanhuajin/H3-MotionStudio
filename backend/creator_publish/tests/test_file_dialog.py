import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from bilibili_auto import BilibiliDraft


class FileDialogTests(unittest.TestCase):
    def draft(self, windows, owners):
        draft = object.__new__(BilibiliDraft)
        draft.window = NS(NativeWindowHandle=10, GetChildren=Mock(side_effect=AssertionError("browser UIA enumeration")))
        controls = {w.NativeWindowHandle: w for w in windows}
        draft.uia = NS(ControlFromHandle=lambda handle: controls[handle],
                       GetRootControl=Mock(side_effect=AssertionError("desktop UIA enumeration")))
        patcher = patch("browser_page.native_file_dialog_handles", return_value=list(controls))
        patcher.start()
        self.addCleanup(patcher.stop)
        user = NS(GetWindow=Mock(side_effect=lambda handle, flag: owners.get(handle, 0)))
        return draft, user

    def dialog(self, handle, name="打开", hidden=False):
        return NS(Name=name, NativeWindowHandle=handle, IsOffscreen=hidden)

    def test_owned_dialog_found_when_browser_stays_foreground(self):
        dialog = self.dialog(20)
        draft, user = self.draft([dialog], {20: 30, 30: 10})
        with patch("bilibili_auto.ctypes.windll", NS(user32=user)):
            self.assertIs(draft.file_dialog(), dialog)

    def test_unrelated_or_hidden_dialog_not_used(self):
        draft, user = self.draft([self.dialog(20), self.dialog(30, hidden=True)], {20: 99, 30: 10})
        with patch("bilibili_auto.ctypes.windll", NS(user32=user)):
            self.assertIsNone(draft.file_dialog())

    def test_browser_child_dialog_absent_from_desktop_children(self):
        dialog = self.dialog(20)
        draft, user = self.draft([dialog], {20: 10})
        with patch("bilibili_auto.ctypes.windll", NS(user32=user)):
            self.assertIs(draft.file_dialog(), dialog)

    def test_multiple_owned_dialogs_stop(self):
        draft, user = self.draft([self.dialog(20), self.dialog(30, "Open")], {20: 10, 30: 10})
        with patch("bilibili_auto.ctypes.windll", NS(user32=user)):
            with self.assertRaisesRegex(RuntimeError, "多个文件选择窗口"):
                draft.file_dialog()

    def test_close_check_uses_dialog_visibility_not_browser_focus(self):
        draft, user = self.draft([], {})
        user.IsWindowVisible = Mock(return_value=1)
        with patch("bilibili_auto.ctypes.windll", NS(user32=user)):
            self.assertTrue(draft.file_dialog_visible(self.dialog(20)))
        user.IsWindowVisible.assert_called_once_with(20)


if __name__ == "__main__":
    unittest.main()
