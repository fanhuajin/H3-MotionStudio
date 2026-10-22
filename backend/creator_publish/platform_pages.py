"""开始发布前，用浏览器原生标签和地址栏补齐平台页面，不注入网页。"""
import os
import ctypes
import time
from _ctypes import COMError
from urllib.parse import urlsplit

from browser_page import read_browser_url

PAGES = {
    "douyin": "https://creator.douyin.com/creator-micro/content/manage",
    "xiaohongshu": "https://creator.xiaohongshu.com/new/note-manager?source=official",
    "bilibili": "https://member.bilibili.com/platform/upload-manager/article",
}
LABELS = {"douyin": "抖音", "xiaohongshu": "小红书", "bilibili": "B站"}
TITLES = {"douyin": "抖音创作者中心", "xiaohongshu": "小红书创作服务平台", "bilibili": "创作中心"}


def platform_for_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        return None
    if parsed.hostname == "creator.douyin.com" and parsed.path.startswith("/creator-micro/content/"):
        return "douyin"
    if parsed.hostname == "creator.xiaohongshu.com" and parsed.path.startswith(("/new/", "/publish/")):
        return "xiaohongshu"
    if parsed.hostname == "member.bilibili.com" and (
        parsed.path.startswith("/platform/upload/video")
        or parsed.path.rstrip("/") in ("/platform/upload-manager/article", "/platform/upload-manager/video")
    ):
        return "bilibili"
    return None


def chrome_controls(window, check=None):
    """只走浏览器外壳，网页自身的 Tab 控件和输入框不参与标签/地址栏判断。"""
    stack = [(window, 0)]
    deadline = time.monotonic() + 15
    visited = 0
    while stack:
        if check:
            check()
        visited += 1
        if visited > 2000 or time.monotonic() > deadline:
            raise RuntimeError("浏览器控件读取超时，未开始发布；请关闭多余窗口后重试。")
        control, depth = stack.pop()
        if control.ControlTypeName == "DocumentControl":
            continue
        yield control
        if depth < 14:
            stack.extend((child, depth + 1) for child in reversed(control.GetChildren()))


class PlatformPages:
    def __init__(self, driver, notice):
        self.driver = driver
        self.notice = notice

    def windows(self):
        # 先用 Win32 筛选浏览器句柄，避免读取桌面上远程工具等无关应用的 UIA 提供程序。
        user32 = ctypes.windll.user32
        handles = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        def collect(handle, _):
            if not user32.IsWindowVisible(handle):
                return True
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(handle, name, len(name))
            if name.value in ("Chrome_WidgetWin_1", "MozillaWindowClass"):
                title = ctypes.create_unicode_buffer(1024)
                user32.GetWindowTextW(handle, title, len(title))
                if any(browser in title.value for browser in ("Edge", "Chrome", "Firefox")):
                    handles.append(handle)
            return True
        user32.EnumWindows(callback_type(collect), 0)
        self.driver.check()
        return [self.driver.uia.ControlFromHandle(handle) for handle in handles]

    def selected(self, window):
        tabs = [c for c in chrome_controls(window, self.driver.check) if c.ControlTypeName == "TabItemControl"]
        selected = [tab for tab in tabs if tab.GetSelectionItemPattern().IsSelected]
        if len(selected) != 1:
            raise RuntimeError("无法确认浏览器当前标签页，未打开重复页面。")
        return tuple(selected[0].GetRuntimeId()), len(tabs)

    def address(self, window):
        values = []
        for control in chrome_controls(window, self.driver.check):
            if control.ControlTypeName != "EditControl" or control.IsOffscreen:
                continue
            if not (any(word in control.Name.lower() for word in ("地址", "address", "search or enter web", "搜索或输入"))
                    or control.AutomationId.lower() in ("omnibox", "addresseditbox")):
                continue
            pattern = control.GetValuePattern()
            if pattern:
                values.append(pattern.Value.strip())
        if not values or len(set(values)) != 1:
            raise RuntimeError("无法读取浏览器地址栏，未打开重复页面。请开启浏览器网页可访问性。")
        value = values[0]
        if value and "://" not in value and "." in value.split("/")[0] and " " not in value:
            value = "https://" + value
        return value

    def focus(self, window):
        self.driver.window = window
        window.SetFocus()
        time.sleep(.2)
        self.driver.check()

    def scan(self):
        # 新标签加载时旧UIA节点会失效，只重读标签栏，不重复打开或发布。
        for attempt in range(4):
            try:
                return self._scan_once()
            except COMError:
                if attempt == 3:
                    raise
                self.driver.check()
                time.sleep(.3)

    def _scan_once(self):
        found = {}
        windows = self.windows()
        for window in windows:
            # 只读标签栏即可找出后台平台页，不逐页切换来读取地址栏。
            for tab in chrome_controls(window, self.driver.check):
                if tab.ControlTypeName != "TabItemControl":
                    continue
                matches = [p for p, title in TITLES.items() if title in tab.Name]
                if len(matches) != 1:
                    continue
                platform = matches[0]
                if platform in found:
                    raise RuntimeError(f"已存在多个{LABELS[platform]}创作页面，请只保留一个后继续；未再打开页面。")
                found[platform] = (window, tuple(tab.GetRuntimeId()))
        return windows, found

    def activate(self, window, target):
        self.focus(window)
        tabs = [tab for tab in chrome_controls(window, self.driver.check)
                if tab.ControlTypeName == "TabItemControl" and tuple(tab.GetRuntimeId()) == target]
        if len(tabs) != 1:
            raise RuntimeError("原平台标签页已关闭，已停止发布。")
        tab = tabs[0]
        if not tab.GetSelectionItemPattern().IsSelected:
            rect = tab.BoundingRectangle
            if rect.width() <= 0 or rect.height() <= 0 or tab.IsOffscreen:
                raise RuntimeError("目标平台标签页不可见，请展开浏览器标签栏。")
            self.driver.click_at(((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2))
        deadline = self.driver.now() + 5
        while self.driver.now() < deadline:
            if self.selected(window)[0] == target:
                return
            self.driver.check()
            time.sleep(.1)
        raise RuntimeError("未确认目标平台标签页，未开始上传。")

    def select_existing(self, platform):
        """进入发布时重新定位平台标签，避免依赖最后就绪的窗口标题。"""
        _, found = self.scan()
        if platform not in found:
            raise RuntimeError(f"{LABELS[platform]}标签页已关闭，未开始上传。")
        self.activate(*found[platform])
        self.wait_ready(platform)

    def wait_ready(self, platform):
        deadline = self.driver.now() + 60
        while self.driver.now() < deadline:
            self.driver.check()
            try:
                address = self.address(self.driver.window)
                document = read_browser_url(self.driver.uia, self.driver.window, "")
                loaded = any(control.ControlTypeName == "DocumentControl" and not control.IsOffscreen
                             and TITLES[platform] in control.Name
                             for control, _ in self.driver.uia.WalkControl(self.driver.window, maxDepth=20))
                if loaded and platform_for_url(address) == platform and platform_for_url(document) == platform:
                    return
            except (RuntimeError, COMError):
                pass
            time.sleep(.3)
        raise RuntimeError(f"{LABELS[platform]}页面尚未就绪，请完成登录或检查加载后继续；未开始发布。")

    def open_page(self, window, platform):
        self.focus(window)
        self.driver.keys("{Ctrl}t")
        self.driver.keys("{Ctrl}l")
        self.driver.paste(PAGES[platform])
        self.driver.keys("{Enter}")
        self.wait_ready(platform)

    def ensure(self):
        self.notice("正在检查已有平台标签页，缺少的页面将自动打开…")
        windows, found = self.scan()
        if not windows:
            # 只启动一次默认浏览器，后续缺失页面在同一个浏览器中用 Ctrl+T 打开。
            os.startfile(PAGES["douyin"])
            deadline = self.driver.now() + 45
            while self.driver.now() < deadline:
                self.driver.check()
                windows = self.windows()
                if windows:
                    break
                time.sleep(.3)
            if not windows:
                raise RuntimeError("默认浏览器未就绪，请打开 Edge 或 Chrome 后继续。")
            self.focus(windows[0])
            self.wait_ready("douyin")
            windows, found = self.scan()
        for platform in PAGES:
            if platform not in found:
                self.notice(f"正在打开{LABELS[platform]}页面…")
                self.open_page(windows[0], platform)
        self.notice("三个平台标签页已备齐，开始执行发布队列。")
