"""桌面互斥只检查 Windows 句柄，不操作任何界面。"""
import unittest
from unittest.mock import Mock, patch
from desktop_session import desktop_session


class DesktopSessionTests(unittest.TestCase):
    def kernel(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 123
        return kernel

    def test_success_releases_own_handle(self):
        kernel = self.kernel()
        with patch("desktop_session.ctypes.WinDLL", return_value=kernel), patch("desktop_session.ctypes.get_last_error", return_value=0):
            with desktop_session():
                kernel.CloseHandle.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_existing_owner_is_not_entered_or_closed(self):
        kernel = self.kernel()
        entered = False
        with patch("desktop_session.ctypes.WinDLL", return_value=kernel), patch("desktop_session.ctypes.get_last_error", return_value=183):
            with self.assertRaisesRegex(RuntimeError, "正在使用桌面"):
                with desktop_session():
                    entered = True
        self.assertFalse(entered)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_exception_still_releases_handle(self):
        kernel = self.kernel()
        with patch("desktop_session.ctypes.WinDLL", return_value=kernel), patch("desktop_session.ctypes.get_last_error", return_value=0):
            with self.assertRaisesRegex(ValueError, "test"):
                with desktop_session():
                    raise ValueError("test")
        kernel.CloseHandle.assert_called_once_with(123)
