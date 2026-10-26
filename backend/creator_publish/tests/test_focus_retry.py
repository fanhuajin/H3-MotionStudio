"""焦点探测失败仅重试读取，不绕过窗口及字段校验。"""
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from focus_detection import focused_field


class FocusRetryTests(unittest.TestCase):
    def result(self, handle=123):
        return CompletedProcess([], 0,
            '{"window_handle":%d,"controls":[{"focused":true,"name":"作品标题"}]}' % handle, "")

    def test_transient_probe_failures_retry_until_focus_is_readable(self):
        errors = [CompletedProcess([], 1, "", message) for message in (
            "CouldNotAutoloadMatchingModule", "没有可读取的焦点控件。")]
        with patch("focus_detection.subprocess.run", side_effect=errors + [self.result()]) as run, \
                patch("focus_detection.time.sleep"):
            self.assertEqual(focused_field("douyin", 123), "title")
        self.assertEqual(run.call_count, 3)

    def test_persistent_failure_is_bounded_and_preserves_reason(self):
        with patch("focus_detection.subprocess.run", return_value=CompletedProcess(
                [], 1, "", "没有可读取的焦点控件。")) as run, patch("focus_detection.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "没有可读取的焦点控件"):
                focused_field("douyin", 123)
        self.assertEqual(run.call_count, 3)

    def test_changed_window_after_retry_still_blocks_input(self):
        with patch("focus_detection.subprocess.run", side_effect=[CompletedProcess(
                [], 1, "", "没有可读取的焦点控件。"), self.result(456)]), \
                patch("focus_detection.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "窗口与当前浏览器不一致"):
                focused_field("douyin", 123)

    def test_other_script_errors_are_not_retried(self):
        with patch("focus_detection.subprocess.run", return_value=CompletedProcess(
                [], 1, "", "probe failed")) as run:
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                focused_field("douyin", 123)
        run.assert_called_once()
