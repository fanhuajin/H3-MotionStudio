"""平台页面准备的离线回归：不打开浏览器、不发送实际键鼠输入。"""
from types import SimpleNamespace as NS
import time
import unittest
from unittest.mock import Mock, call, patch

from platform_pages import PAGES, PlatformPages, chrome_controls, platform_for_url


class PlatformPageTests(unittest.TestCase):
    def setUp(self):
        self.driver = Mock()
        self.driver.now.side_effect = lambda: time.monotonic()
        self.pages = PlatformPages(self.driver, Mock())
        self.window = Mock()
        self.found = {p: (self.window, (i,)) for i, p in enumerate(PAGES)}

    def test_all_open_does_not_open_any_page(self):
        with patch.object(self.pages, "scan", return_value=([self.window], self.found)), \
             patch.object(self.pages, "activate") as activate, patch.object(self.pages, "wait_ready") as ready, \
             patch.object(self.pages, "open_page") as opener, patch("platform_pages.os.startfile") as launch:
            self.pages.ensure()
        opener.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(activate.call_count, 3)
        self.assertEqual(ready.call_args_list, [call(p) for p in PAGES])

    def test_only_missing_page_is_opened(self):
        found = {p: v for p, v in self.found.items() if p != "xiaohongshu"}
        with patch.object(self.pages, "scan", return_value=([self.window], found)), \
             patch.object(self.pages, "activate"), patch.object(self.pages, "wait_ready"), \
             patch.object(self.pages, "open_page") as opener:
            self.pages.ensure()
        opener.assert_called_once_with(self.window, "xiaohongshu")

    def test_no_platform_pages_opens_each_once_in_existing_browser(self):
        with patch.object(self.pages, "scan", return_value=([self.window], {})), \
             patch.object(self.pages, "open_page") as opener:
            self.pages.ensure()
        self.assertEqual(opener.call_args_list, [call(self.window, p) for p in PAGES])

    def test_no_browser_launches_default_once_then_reuses_it(self):
        with patch.object(self.pages, "scan", side_effect=[([], {}), ([self.window], {"douyin": self.found["douyin"]})]), \
             patch.object(self.pages, "windows", return_value=[self.window]), patch.object(self.pages, "focus"), \
             patch.object(self.pages, "activate"), patch.object(self.pages, "wait_ready"), \
             patch.object(self.pages, "open_page") as opener, patch("platform_pages.os.startfile") as launch:
            self.pages.ensure()
        launch.assert_called_once_with(PAGES["douyin"])
        self.assertEqual(opener.call_args_list, [call(self.window, "xiaohongshu"), call(self.window, "bilibili")])

    def test_unreadable_existing_tabs_stop_before_any_open(self):
        with patch.object(self.pages, "scan", side_effect=RuntimeError("无法读取")), \
             patch.object(self.pages, "open_page") as opener, patch("platform_pages.os.startfile") as launch:
            with self.assertRaises(RuntimeError):
                self.pages.ensure()
        opener.assert_not_called()
        launch.assert_not_called()

    def test_not_ready_page_stops_before_opening_other_pages(self):
        with patch.object(self.pages, "scan", return_value=([self.window], self.found)), \
             patch.object(self.pages, "activate"), patch.object(self.pages, "wait_ready", side_effect=RuntimeError("请登录")), \
             patch.object(self.pages, "open_page") as opener:
            with self.assertRaisesRegex(RuntimeError, "请登录"):
                self.pages.ensure()
        opener.assert_not_called()

    def test_management_urls_and_existing_uploads_are_recognized(self):
        for platform, url in PAGES.items():
            self.assertEqual(platform_for_url(url), platform)
        self.assertEqual(platform_for_url("https://member.bilibili.com/platform/upload/video/frame"), "bilibili")
        self.assertEqual(platform_for_url("https://creator.douyin.com/creator-micro/content/post/video"), "douyin")
        self.assertEqual(platform_for_url("https://creator.xiaohongshu.com/publish/publish"), "xiaohongshu")

    def test_query_or_trailing_slash_does_not_create_duplicates(self):
        for platform, url in PAGES.items():
            base = url.split("?")[0]
            self.assertEqual(platform_for_url(base + "/?source=another"), platform)

    def test_similar_hosts_http_and_login_are_not_ready(self):
        for url in ("https://creator.douyin.com.evil.test/creator-micro/content/manage",
                    "http://creator.douyin.com/creator-micro/content/manage",
                    "https://creator.douyin.com/login",
                    "https://member.bilibili.com/platform/upload-evil",
                    "https://www.bilibili.com/video/abc"):
            self.assertIsNone(platform_for_url(url))

    def test_open_uses_native_new_tab_and_exact_requested_url(self):
        with patch.object(self.pages, "focus"), patch.object(self.pages, "wait_ready") as ready:
            self.pages.open_page(self.window, "xiaohongshu")
        self.assertEqual(self.driver.keys.call_args_list, [call("{Ctrl}t"), call("{Ctrl}l"), call("{Enter}")])
        self.driver.paste.assert_called_once_with(PAGES["xiaohongshu"])
        ready.assert_called_once_with("xiaohongshu")

    def test_scan_checks_all_tabs_and_stops_at_first_repeated_tab(self):
        with patch.object(self.pages, "windows", return_value=[self.window]), patch.object(self.pages, "focus"), \
             patch.object(self.pages, "selected", side_effect=[((1,), 2), ((2,), 2), ((1,), 2)]), \
             patch.object(self.pages, "address", side_effect=[PAGES["douyin"], PAGES["bilibili"]]), \
             patch.object(self.pages, "next_tab") as advance:
            _, found = self.pages.scan()
        self.assertEqual(set(found), {"douyin", "bilibili"})
        self.assertEqual(advance.call_count, 2)

    def test_duplicate_existing_platforms_are_not_silently_reopened(self):
        with patch.object(self.pages, "windows", return_value=[self.window]), patch.object(self.pages, "focus"), \
             patch.object(self.pages, "selected", side_effect=[((1,), 2), ((2,), 2)]), \
             patch.object(self.pages, "address", return_value=PAGES["douyin"]), patch.object(self.pages, "next_tab"):
            with self.assertRaisesRegex(RuntimeError, "多个抖音"):
                self.pages.scan()

    def test_existing_login_page_is_reused_without_opening_another_tab(self):
        with patch.object(self.pages, "windows", return_value=[self.window]), patch.object(self.pages, "focus"), \
             patch.object(self.pages, "selected", return_value=((1,), 1)), \
             patch.object(self.pages, "address", return_value="https://creator.douyin.com/login"):
            _, found = self.pages.scan()
        self.assertIn("douyin", found)

    def test_webpage_tabs_and_text_fields_are_never_traversed_as_browser_chrome(self):
        webpage = NS(ControlTypeName="DocumentControl", GetChildren=Mock(side_effect=AssertionError("webpage traversed")))
        root = NS(ControlTypeName="WindowControl", GetChildren=lambda: [webpage])
        self.assertEqual(list(chrome_controls(root)), [root])

    def test_address_bar_alone_does_not_mean_page_loaded(self):
        self.driver.uia.WalkControl.return_value = []
        with patch.object(self.pages, "address", return_value=PAGES["douyin"]), \
             patch("platform_pages.read_browser_url", return_value=PAGES["douyin"]), \
             patch("platform_pages.time.monotonic", side_effect=[0, 1, 61]), patch("platform_pages.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "尚未就绪"):
                self.pages.wait_ready("douyin")

    def test_ready_requires_correct_loaded_document(self):
        self.driver.uia.WalkControl.return_value = [(NS(ControlTypeName="DocumentControl", IsOffscreen=False, Name="抖音创作者中心"), 0)]
        with patch.object(self.pages, "address", return_value=PAGES["douyin"]), \
             patch("platform_pages.read_browser_url", return_value=PAGES["douyin"]):
            self.pages.wait_ready("douyin")


if __name__ == "__main__":
    unittest.main()
