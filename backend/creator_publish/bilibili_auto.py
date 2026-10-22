"""B站投稿准备：读取本地控件并发送鼠标键盘事件，核对完成后点击投稿。"""

import argparse
from _ctypes import COMError
import ctypes
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import sys
from pathlib import Path
import time
import traceback
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit

from fill_helper import (
    AUTOMATION_READY, AUTOMATION_REQUEST, AUTOMATION_RESULT,
    read_copy, write_keyboard_state, read_keyboard_state, keyboard_helper_ready,
)
from native_mouse import mouse_event, pointer_path

CHINA_TIME = timezone(timedelta(hours=8))


class ControlSnapshot:
    """一次捕获控件定位属性，避免同轮遍历后重复访问已被页面销毁的节点。"""

    def __init__(self, control):
        self.control = control
        self.Name = control.Name
        self.ControlTypeName = control.ControlTypeName
        self.BoundingRectangle = control.BoundingRectangle
        self.IsOffscreen = control.IsOffscreen
        self.ClassName = self._optional(control, "ClassName", "")
        self.AutomationId = self._optional(control, "AutomationId", "")
        self.IsEnabled = self._optional(control, "IsEnabled", True)

    @staticmethod
    def _optional(control, name, default):
        # 次要属性在页面刷新时可能抛 COMError（getattr 默认值拦不住），按缺省值处理。
        try:
            return getattr(control, name, default)
        except COMError:
            return default

    def __getattr__(self, name):
        # 点击和状态模式仍使用实时控件，失效时停止，不盲目重发操作。
        return getattr(self.control, name)


def category_from_folder(folder):
    """只按用户指定的目录标记分类；缺失或混合标记都拒绝猜测。"""
    matches = [
        category for marker, category in (("_跳舞", "舞蹈"), ("_唱歌", "音乐"))
        if marker in folder.name
    ]
    if len(matches) != 1:
        raise ValueError("目录名必须明确包含 _跳舞 或 _唱歌，不能同时包含两个标记。")
    return matches[0]


def cover_from_folder(folder):
    """两种比例共用一张已有封面，优先B站4x3.png，不随意选择人物素材。"""
    for name in ("B站4x3.png", "封面.png", "封面.jpg"):
        path = folder / name
        if path.is_file():
            return path.resolve()
    raise ValueError("没有找到封面：请提供 B站4x3.png、封面.png 或封面.jpg。")


def parse_publish_time(value, now=None, *, require_five_minutes=True):
    """按北京时间解析分钟精度；截图中页面允许当前时间5分钟至15天内。"""
    if not value.strip():
        return None
    try:
        selected = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=CHINA_TIME)
    except ValueError as error:
        raise ValueError("定时时间格式应为 YYYY-MM-DD HH:MM，例如 2026-10-10 18:30。") from error
    current = now or datetime.now(CHINA_TIME)
    if current.tzinfo is None:
        raise ValueError("校验时间必须带时区。")
    if not current + timedelta(minutes=5) <= selected <= current + timedelta(days=15):
        raise ValueError("定时时间应在北京时间当前时间5分钟之后、15天以内。")
    if require_five_minutes and selected.minute % 5:
        raise ValueError("页面时间选项以5分钟为间隔，请选择例如18:30或18:35。")
    return selected


def prepare_files(folder):
    """仅接受单个明确视频；优先最终成片，避免批量误上传其他素材。"""
    title, description, tags = read_copy(folder / "发布文案.txt")
    preferred = folder / "最终成片.mp4"
    videos = [preferred] if preferred.is_file() else [
        item for item in folder.iterdir()
        if item.is_file() and item.suffix.lower() in (".mp4", ".mov", ".mkv")
    ]
    if len(videos) != 1:
        raise ValueError("目录中必须有最终成片.mp4，或只有一个视频文件。")
    if len(title) > 80 or len(description) > 2000:
        raise ValueError("标题超过80字或简介超过2000字。")
    if len(tags) > 10 or any(not tag or len(tag) > 20 for tag in tags):
        raise ValueError("B站最多10个标签，每个标签应为1至20字。")
    return videos[0].resolve(), title, description, tags


def is_upload_url(value):
    """校验精确域名和投稿路径，不接受相似域名或普通视频页面。"""
    url = urlsplit(value)
    return (
        url.scheme == "https"
        and url.hostname == "member.bilibili.com"
        and url.path.startswith("/platform/upload/video")
    )


def choose_control(found, editable=False):
    """合并浏览器同一位置的容器/文字节点，独立同名字段仍保留歧义。"""
    if not editable:
        buttons = [item for item in found if item.ControlTypeName == "ButtonControl"]
        texts = [item for item in found if item.ControlTypeName == "TextControl"]
        found = buttons or texts or found
    unique = {}
    for item in found:
        rect = item.BoundingRectangle
        unique[(rect.left, rect.top, rect.right, rect.bottom)] = item
    found = list(unique.values())
    if len(found) > 1 and not editable:
        boxes = [item.BoundingRectangle for item in found]
        centers = [((box.left + box.right) / 2, (box.top + box.bottom) / 2) for box in boxes]
        if (
            max(x for x, _ in centers) - min(x for x, _ in centers) <= 30
            and max(y for _, y in centers) - min(y for _, y in centers) <= 20
        ):
            found = [min(found, key=lambda item: item.BoundingRectangle.width() * item.BoundingRectangle.height())]
    if len(found) > 1:
        details = [
            (item.ControlTypeName, item.BoundingRectangle.left, item.BoundingRectangle.top,
             item.BoundingRectangle.right, item.BoundingRectangle.bottom)
            for item in found
        ]
        raise RuntimeError(f"存在多个同名控件，无法安全定位：{details}")
    return found[0] if found else None


class BilibiliDraft:
    def __init__(self):
        # 复用 Windows MCP 已安装的 UIA 和输入实现，无需模型/MCP客户端逐步决策。
        import pyperclip
        import windows_mcp.uia as uia

        self.uia = uia
        self.clipboard = pyperclip
        self.window = None
        self.form_left = None
        self.copy_file = None
        self.on_tick = None
        self.on_pause = None
        self.paused_seconds = 0.0

    def check(self):
        """每步输入前检查取消键及窗口，窗口被切走即停，不抢回焦点。"""
        if self.on_tick:
            self.on_tick()
        if ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000:
            if not self.on_pause:
                raise RuntimeError("用户按下 Esc，流程已停止。")
            started = time.monotonic()
            self.on_pause()
            # 继续按钮可能在Esc尚未松开时被点击；等待松开，避免立刻再次暂停。
            while ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000:
                if self.on_tick:
                    self.on_tick()
                time.sleep(0.05)
            self.paused_seconds += time.monotonic() - started
            if self.window:
                self.window.SetFocus()
                time.sleep(0.2)
        if self.window:
            self.ensure_foreground()

    def ensure_foreground(self, grace=6.0):
        """前台窗口短暂被弹窗/通知抢走时只读等待，不发送任何输入；超时才停止。"""
        deadline = time.monotonic() + grace
        while True:
            try:
                current = self.uia.GetForegroundControl()
                if current is not None and current.NativeWindowHandle == self.window.NativeWindowHandle:
                    return
            except COMError:
                pass  # 窗口切换瞬间读取失败，视为暂时不匹配并重试。
            if time.monotonic() >= deadline:
                platform = getattr(self, "page_title", "B站")
                raise RuntimeError(f"当前窗口发生变化，流程已停止，请切回{platform}后重新运行。")
            if self.on_tick:
                self.on_tick()
            time.sleep(0.2)

    def now(self):
        return time.monotonic() - self.paused_seconds

    def controls(self):
        """只遍历选定浏览器窗口，控件不可见时不用于坐标操作。"""
        last_error=None
        for attempt in range(4):
            self.check()
            result=[]
            try:
                for control, _ in self.uia.WalkControl(self.window, maxDepth=45):
                    result.append(ControlSnapshot(control))
                    if len(result)>4000:
                        raise RuntimeError("控件数量过多，停止以避免定位到其他区域。")
                return result
            except COMError as error:
                last_error=error
                time.sleep(0.15)
        raise RuntimeError("页面控件正在刷新，重新读取后仍未稳定，已保留当前稿件。") from last_error

    def visible(self, control):
        rect = control.BoundingRectangle
        window = self.window.BoundingRectangle
        return (
            rect.width() > 0 and rect.height() > 0
            and window.left < (rect.left + rect.right) / 2 < window.right
            and window.top + 100 < (rect.top + rect.bottom) / 2 < window.bottom - 20
            and not control.IsOffscreen
        )

    def control_names(self):
        """读取上传/标签状态时即时捕获名称，跳过页面刷新中失效的节点。"""
        names = []
        for control in self.controls():
            try:
                names.append(control.Name)
            except COMError:
                continue
        return names

    def locate(self, name, editable=False):
        found = []
        for control in self.controls():
            try:
                if control.Name == name and self.visible(control) and (
                    not editable or control.ControlTypeName == "EditControl"
                ):
                    found.append(control)
            except COMError:
                continue
        return choose_control(found, editable)

    def find(self, name, editable=False):
        """从当前位置向下查找，处理完上一项后不再返回页首。"""
        control = self.locate(name, editable)
        if control:
            return control
        for _ in range(14):
            control = self.locate(name, editable)
            if control:
                return control
            self.scroll_page("down", 4)
            time.sleep(0.12)
        raise RuntimeError(f"没有找到“{name}”，页面或浏览器可访问性可能发生变化。")

    def move_pointer(self, target):
        """使用可见的短移动轨迹，每一步仍检查暂停及窗口状态。"""
        start = self.uia.GetCursorPos()
        for point in pointer_path(start, target):
            self.check()
            mouse_event(0x0001, position=point)
            time.sleep(0.01)

    def click_at(self, target):
        self.check()
        self.move_pointer(target)
        self.check()
        before_press = getattr(self, "before_click_press", None)
        if before_press:
            before_press()
        mouse_event(0x0002)  # LEFTDOWN，不附加坐标，不触发二次瞬移。
        try:
            time.sleep(0.04)
        finally:
            mouse_event(0x0004)  # LEFTUP，保证按钮释放。
        time.sleep(0.12)

    def scroll_at(self, direction, amount=1, target=None):
        """先连续移动到滚动区域，再逐格发送滚轮事件。"""
        if direction not in ("up", "down") or amount < 0:
            raise ValueError("滚轮方向或次数无效。")
        if target is not None:
            self.move_pointer(target)
        for _ in range(amount):
            self.check()
            mouse_event(0x0800, wheel=120 if direction == "up" else -120)
            time.sleep(0.04)

    def scroll_in_control(self, control, direction, amount=1):
        """鼠标移动后再次核验实际滚动容器，位置变化时重定位而不滚整页。"""
        live=getattr(control,"control",control)
        for _ in range(4):
            self.check()
            rect=live.BoundingRectangle
            self.move_pointer(((rect.left+rect.right)//2,(rect.top+rect.bottom)//2))
            latest=live.BoundingRectangle
            x,y=self.uia.GetCursorPos()
            if self.visible(live) and latest.left<x<latest.right and latest.top<y<latest.bottom:
                self.scroll_at(direction,amount)
                return
        raise RuntimeError("滚动区域持续改变位置，未发送滚轮。")

    def page_scroll_point(self):
        rect = self.window.BoundingRectangle
        current = self.uia.GetCursorPos()
        controls = [item for item in self.controls() if self.visible(item)]
        interactive = {"EditControl", "ComboBoxControl", "ListControl", "ListItemControl",
                       "MenuControl", "MenuItemControl", "ButtonControl", "SliderControl",
                       "CheckBoxControl", "RadioButtonControl", "TreeControl"}
        blocked = []
        for item in controls:
            bounds = item.BoundingRectangle
            if item.ControlTypeName in interactive or (
                item.ControlTypeName in ("DocumentControl", "GroupControl", "CustomControl")
                and bounds.width() < rect.width() * 0.7 and bounds.height() < rect.height() * 0.6
            ):
                blocked.append(bounds)
        def safe(point):
            x, y = point
            return (rect.left + 200 < x < rect.right - 30
                    and rect.top + 130 < y < rect.bottom - 50
                    and not any(b.left - 8 <= x <= b.right + 8 and b.top - 8 <= y <= b.bottom + 8 for b in blocked))
        if safe(current):
            return current
        # 在鼠标附近寻找页面空白，不再每次移到窗口最右侧。
        candidates = [(current[0] + dx, current[1] + dy)
                      for dx in range(-300, 301, 50) for dy in range(-150, 151, 50)]
        candidates += [(rect.left + int(rect.width() * fraction), (rect.top + rect.bottom) // 2)
                       for fraction in (0.3, 0.4, 0.6, 0.75, 0.85)]
        candidates.sort(key=lambda point: (point[0] - current[0]) ** 2 + (point[1] - current[1]) ** 2)
        for point in candidates:
            if safe(point):
                return point
        raise RuntimeError("未找到适合滚动页面的空白区域。")

    def scroll_page(self, direction, amount):
        self.check()
        self.scroll_at(direction, amount, self.page_scroll_point())
        time.sleep(0.15)

    def input_column(self):
        fields = [item for item in self.controls() if item.Name == "请输入稿件标题"]
        if len(fields) != 1:
            raise RuntimeError("无法识别表单输入列。")
        left = fields[0].BoundingRectangle.left
        if self.window.BoundingRectangle.left + 200 < left < self.window.BoundingRectangle.right - 100:
            self.form_left = left
        if self.form_left is None:
            raise RuntimeError("输入列不可见，请先定位标题。")
        return self.form_left

    def wait_name(self, name, timeout=8):
        deadline = self.now() + timeout
        while self.now() < deadline:
            control = self.locate(name)
            if control:
                return control
            time.sleep(0.15)
        raise RuntimeError(f"等待“{name}”超时。")

    def keys(self, keys):
        self.check()
        self.uia.SendKeys(keys, waitTime=0.12)

    def click(self, control):
        if isinstance(control,ControlSnapshot):
            return self.click_declaration(control)
        self.check()
        if not self.visible(control):
            raise RuntimeError("目标控件已离开可见区域，停止输入。")
        rect = control.BoundingRectangle
        self.click_at(((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2))

    def submit(self, control):
        """最终提交入口，和设置页中的同名标签分开记录。"""
        if not getattr(self,"tracks_submit_click",False) and getattr(self,"on_submit",None):
            self.on_submit()
        self.click(control)

    def click_declaration(self, control):
        """仅声明菜单使用实时位置检查，其他平台沿用原有点击流程。"""
        self.check()
        live = control.control if isinstance(control, ControlSnapshot) else control
        for _ in range(4):
            if not self.visible(live):
                raise RuntimeError("目标控件已离开可见区域，停止输入。")
            rect = live.BoundingRectangle
            target = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            self.move_pointer(target)
            # 图片加载或展开动画会改变排版，移动结束后再读实时位置。
            latest = live.BoundingRectangle
            current = ((latest.left + latest.right) // 2, (latest.top + latest.bottom) // 2)
            if current != target:
                continue
            cursor = self.uia.GetCursorPos()
            if abs(cursor[0]-target[0]) > 2 or abs(cursor[1]-target[1]) > 2:
                raise RuntimeError(f"鼠标未到达控件：目标{target}，实际{cursor}，未点击。")
            self.click_at(target)
            return
        raise RuntimeError("控件在鼠标移动期间持续改变位置，未点击，请等待页面加载完成。")

    def paste(self, value):
        """通过剪贴板和Ctrl+V保留emoji；不调用网页赋值或控件SetValue。"""
        self.check()
        self.clipboard.copy(value)
        self.keys("{Ctrl}v")

    def verify_text(self, expected, *, paragraph_copy=False):
        """从当前编辑框复制回读，验证完整文本，禁止只看发送成功日志。"""
        self.keys("{Ctrl}a")
        self.clipboard.copy("")
        self.keys("{Ctrl}c")
        deadline=self.now()+1
        expected_text=expected.replace("\r\n","\n").strip()
        # B站富文本段落在纯文本剪贴板中以两个换行分隔；仅简介允许
        # 整体一致的段落转换，不折叠任意空白，也不放宽标题等字段。
        accepted={expected_text}
        if paragraph_copy:
            accepted.add(expected_text.replace("\n", "\n\n"))
        actual=""
        while self.now()<deadline:
            actual = self.clipboard.paste().replace("\r\n", "\n").strip()
            if actual in accepted:
                break
            time.sleep(0.05)
        self.keys("{End}")
        if actual not in accepted:
            raise RuntimeError(f"输入结果与文案不一致：期望{expected_text!r}，回读{actual!r}，流程已停止。")

    def verify_edit_value(self, hint, expected):
        """输入框原生只读值不依赖复制快捷键或剪贴板更新时间。"""
        deadline=self.now()+5
        actual=None
        while self.now()<deadline:
            self.check()
            entry=self.locate(hint,editable=True)
            if entry is not None:
                pattern=entry.GetValuePattern()
                actual=pattern.Value if pattern is not None else None
                if actual is not None and actual.strip()==expected.strip():
                    return
            time.sleep(0.1)
        raise RuntimeError(f"输入框核验失败：字段{hint!r}，期望{expected!r}，实际{actual!r}，未继续发布。")

    def fill(self, control, value):
        self.click(control)
        self.keys("{Ctrl}a")
        self.paste(value)
        self.verify_text(value)

    def start_keyboard_helper(self, copy_file):
        """复用正在运行的按键版；未运行时启动，F6仍由按键版注册和处理。"""
        self.copy_file = copy_file
        from keyboard_session import start
        start(copy_file)
        deadline = self.now() + 5
        while self.now() < deadline:
            self.check()
            ready = read_keyboard_state(AUTOMATION_READY)
            if keyboard_helper_ready(ready):
                return
            time.sleep(0.1)
        raise RuntimeError("按键版未准备好。如果旧版正在运行，请按F9退出一次后重试。")

    def keyboard_field(self, field):
        """聚焦完成后触发F6，等待按键版明确回执，不自行输入文案。"""
        request_id = uuid.uuid4().hex
        self.check()
        write_keyboard_state(AUTOMATION_REQUEST, {
            "id": request_id, "window": self.window.NativeWindowHandle,
            "field": field, "copy_file": str(self.copy_file),
        })
        try:
            self.keys("{F6}")
            deadline = self.now() + (90 if field == "description" else 30)
            while self.now() < deadline:
                self.check()
                result = read_keyboard_state(AUTOMATION_RESULT)
                if result is not None and result.get("id") == request_id:
                    if not result.get("ok"):
                        raise RuntimeError(result.get("message", "按键版填写失败。"))
                    return
                time.sleep(0.1)
            raise RuntimeError("等待按键版F6填写超时，停止后续操作。")
        finally:
            AUTOMATION_REQUEST.unlink(missing_ok=True)

    def keyboard_fill(self, control, field, value):
        if field=="title":
            pattern=control.GetValuePattern()
            if pattern is not None and pattern.Value.strip()==value.strip():
                return
        self.click(control)
        if field=="title":
            deadline=self.now()+2
            while self.now()<deadline:
                self.check()
                focus=self.uia.GetFocusedControl()
                if (focus and focus.ControlTypeName=="EditControl"
                        and focus.Name==control.Name and focus.AutomationId!="RootWebArea"):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("标题点击后未确认输入焦点，未触发F6。")
        self.keyboard_field(field)
        if field=="title":
            self.verify_edit_value(control.Name,value)
        else:
            self.verify_text(value, paragraph_copy=field=="description" and getattr(self,"page_title","B站")=="B站")

    def select_browser(self):
        windows = [
            item for item in self.uia.GetRootControl().GetChildren()
            if "创作中心" in item.Name and "哔哩哔哩" in item.Name
            and any(browser in item.Name for browser in ("Edge", "Chrome", "Firefox"))
        ]
        if len(windows) != 1:
            raise RuntimeError("请只打开一个B站创作中心浏览器窗口。")
        self.window = windows[0]
        self.window.SetFocus()
        time.sleep(0.3)
        from browser_page import read_browser_url
        url = read_browser_url(self.uia, self.window, "创作中心")
        self.ensure_upload_page(url)

    def ensure_upload_page(self, url):
        if is_upload_url(url):
            return
        parsed = urlsplit(url)
        # 管理页是发布后及重新打开创作中心的常见入口。
        # 仅在核验官方管理页后点击投稿，不在其他网页上查找同名按钮。
        if (parsed.scheme != "https" or parsed.hostname != "member.bilibili.com"
                or parsed.path.rstrip("/") not in (
                    "/platform/upload-manager/article", "/platform/upload-manager/video")):
            raise RuntimeError(f"当前标签页不是B站视频投稿页或稿件管理页：{url}")
        entry = self.locate("投稿") or self.locate("视频投稿")
        if entry is None:
            raise RuntimeError("当前是B站稿件管理页，未找到“投稿”入口，请打开视频投稿页后继续。")
        self.click(entry)
        from browser_page import read_browser_url
        deadline = self.now() + 20
        while self.now() < deadline:
            self.check()
            current = read_browser_url(self.uia, self.window, "创作中心")
            if is_upload_url(current):
                return
            time.sleep(0.2)
        raise RuntimeError("点击B站投稿入口后未进入视频投稿页，未上传或提交。")

    def upload(self, video):
        names = self.control_names()
        if "请输入稿件标题" in names:
            if video.stem not in names:
                raise RuntimeError("当前稿件的视频名与素材不一致，请打开空白投稿页面后重试。")
            print("检测到已有稿件，填写当前稿件，不重复上传。", flush=True)
            return
        # 上传区域的文本节点也有矩形，使用真实鼠标点击它。
        self.click(self.find("上传视频"))
        self.choose_file(video)

    def file_dialog(self):
        """Edge的文件框可能已显示但浏览器仍为前台；只接受本窗口的弹窗。"""
        from ctypes import wintypes
        user = ctypes.windll.user32
        user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetWindow.restype = wintypes.HWND
        browser = self.window.NativeWindowHandle
        found = []
        from browser_page import native_file_dialog_handles
        for handle in native_file_dialog_handles():
            owner = user.GetWindow(handle, 4)  # GW_OWNER
            seen = set()
            while owner and owner not in seen:
                if owner == browser:
                    control = self.uia.ControlFromHandle(handle)
                    if control.Name in ("打开", "Open") and not control.IsOffscreen:
                        found.append(control)
                    break
                seen.add(owner)
                owner = user.GetWindow(owner, 4)
        if len(found) > 1:
            raise RuntimeError("当前浏览器存在多个文件选择窗口，未输入文件路径。")
        return found[0] if found else None

    def choose_file(self, path):
        """共用系统文件选择框：填写明确路径，返回浏览器后再执行后续步骤。"""
        deadline = self.now() + 8
        dialog = None
        while self.now() < deadline:
            dialog = self.file_dialog()
            if dialog is not None:
                break
            time.sleep(0.1)
        if dialog is None:
            raise RuntimeError("文件选择窗口未出现。")
        # 文件窗口是唯一允许的临时焦点切换；文件名仅为视频路径，不执行命令。
        browser = self.window
        dialog.SetFocus()
        self.window = dialog
        self.check()
        entries = [
            item for item, _ in self.uia.WalkControl(dialog, maxDepth=12)
            if item.ControlTypeName == "EditControl" and "文件名" in item.Name
        ]
        if len(entries) == 1:
            self.fill(entries[0], str(path))
        elif not entries:
            # 系统文件框部分版本不暴露子控件；Alt+N是“文件名”的原生快捷键。
            # 回读路径一致才确认，避免把文件路径写入其他控件。
            self.keys("{Alt}n")
            self.keys("{Ctrl}a")
            self.paste(str(path))
            self.verify_text(str(path))
        else:
            raise RuntimeError("存在多个文件名输入框。")
        self.keys("{Enter}")
        deadline = self.now() + 8
        while self.file_dialog_visible(dialog):
            if self.now() > deadline:
                raise RuntimeError("文件选择窗口未关闭，请检查文件路径。")
            time.sleep(0.1)
        self.window = browser
        self.check()

    def file_dialog_visible(self, dialog):
        return bool(ctypes.windll.user32.IsWindowVisible(dialog.NativeWindowHandle))

    def cover(self, path):
        label = self.find("封面")
        rect = label.BoundingRectangle
        # 由表单输入列和封面所在行定位预览，避免固定屏幕坐标。
        self.click_at((self.input_column() + 65, (rect.top + rect.bottom) // 2 + 30))
        self.wait_name("封面制作")
        self.click(self.wait_name("上传封面"))
        self.choose_file(path)
        self.wait_name("双比例同步改动")
        # 新上传图片使用页面默认居中位置；同步开启时同一图片用于两种比例。
        sync = self.locate("双比例同步改动")
        rect = sync.BoundingRectangle
        checkbox = [
            item for item in self.controls()
            if item.ControlTypeName == "CheckBoxControl" and self.visible(item)
            and abs(item.BoundingRectangle.top - rect.top) < 25
        ]
        if checkbox and len(checkbox) == 1:
            pattern = checkbox[0].GetTogglePattern()
            if int(pattern.ToggleState) == 0:
                self.click(checkbox[0])
        # 保留上传后的默认居中和缩放，不点击顶部滑块或移动图片。
        self.click(self.wait_name("完成"))
        self.wait_cover_closed()
        print("同一封面已上传；使用默认居中位置，请最终检查两种比例的文字。", flush=True)

    def wait_cover_closed(self):
        """B站保存封面会重建控件树；仅重读确认结果，不重复保存或上传。"""
        deadline = self.now() + 10
        last_error = None
        while self.now() < deadline:
            self.check()
            try:
                if self.locate("封面制作") is None:
                    return
            except COMError as error:
                last_error = error
            time.sleep(0.2)
        raise RuntimeError("B站封面保存后页面未稳定或编辑器未关闭，已停止后续操作。") from last_error

    def declaration(self):
        entry = self.find("请选择符合您视频内容的创作声明", editable=True)
        if entry.GetValuePattern().Value == "含AI生成内容":
            return
        option = self.locate("含AI生成内容")
        if option is None:
            # 为下拉菜单保留空间，逐格滚动并重新读取输入框的位置。
            for _ in range(20):
                rect = entry.BoundingRectangle
                window = self.window.BoundingRectangle
                if rect.top > window.top + 140 and rect.bottom < window.bottom - 250:
                    break
                self.scroll_page("up" if rect.top <= window.top + 140 else "down", 1)
                entry = self.locate("请选择符合您视频内容的创作声明", editable=True)
                if entry is None:
                    raise RuntimeError("创作声明输入框滚动后不可见，未继续发布。")
            else:
                raise RuntimeError("无法为创作声明菜单留出可见空间，未继续发布。")
            self.click_declaration(self.declaration_opener(entry))
            try:
                option = self.wait_name("含AI生成内容", timeout=3)
            except RuntimeError as error:
                if str(error) != "等待“含AI生成内容”超时。":
                    raise
                # 点击有时仅聚焦只读输入框。焦点核验后用系统方向键展开。
                focused = self.uia.GetFocusedControl()
                bounds = focused.BoundingRectangle
                rect = entry.BoundingRectangle
                if (focused.Name != entry.Name or
                        not rect.left <= (bounds.left+bounds.right)/2 <= rect.right or
                        not rect.top <= (bounds.top+bounds.bottom)/2 <= rect.bottom):
                    self.save_declaration_diagnostic()
                    raise RuntimeError("创作声明菜单未展开且焦点不在声明框，诊断已保存，未发布。") from error
                self.keys("{Down}")
                try:
                    option = self.wait_name("含AI生成内容", timeout=10)
                except RuntimeError:
                    self.save_declaration_diagnostic()
                    raise
        self.click(option)
        deadline = self.now() + 5
        while self.now() < deadline:
            entry = self.find("请选择符合您视频内容的创作声明", editable=True)
            if entry.GetValuePattern().Value == "含AI生成内容":
                return
            time.sleep(0.15)
        raise RuntimeError("未确认AI创作声明，未继续发布。")

    def declaration_opener(self, entry):
        """优先定位输入框内实际下拉箭头；只读输入框中央不一定展开菜单。"""
        rect = entry.BoundingRectangle
        candidates=[]
        for control in self.controls():
            bounds=control.BoundingRectangle
            if (self.visible(control)
                    and control.ControlTypeName in ("TextControl","ImageControl","ButtonControl")
                    and rect.right-60 <= bounds.left < bounds.right <= rect.right
                    and rect.top <= bounds.top < bounds.bottom <= rect.bottom
                    and bounds.width() <= 35 and bounds.height() <= 45):
                candidates.append(control)
        return choose_control(candidates) or entry

    def save_declaration_diagnostic(self):
        data=[]
        focused=self.uia.GetFocusedControl()
        if focused:
            rect=focused.BoundingRectangle
            data.append({"name":focused.Name,"type":"FocusedControl",
                         "rect":[rect.left,rect.top,rect.right,rect.bottom],
                         "cursor":self.uia.GetCursorPos()})
        for control in self.controls():
            rect=control.BoundingRectangle
            if self.visible(control):
                data.append({"name":control.Name,"type":control.ControlTypeName,
                             "rect":[rect.left,rect.top,rect.right,rect.bottom]})
        Path(__file__).with_name("bilibili_declaration_diagnostic.json").write_text(
            json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")

    def category(self, value):
        from bilibili_category import set_category
        return set_category(self,value)

    def category_option(self, value):
        from bilibili_category import category_option
        return category_option(self,value)

    def in_bilibili_category_menu(self, control):
        node = getattr(control, "control", control)
        for _ in range(8):
            if node is None:
                break
            try:
                if "drop-list-v2-content-wrp" in getattr(node, "ClassName", ""):
                    return True
                if node.ControlTypeName in ("DocumentControl", "WindowControl"):
                    break
                parent = getattr(node, "GetParentControl", None)
                node = parent() if parent else None
            except COMError:
                return False
        return False

    def bilibili_category_menu(self, controls=None):
        from bilibili_category import category_menu
        return category_menu(self,controls)

    def reveal_bilibili_cover(self):
        for _ in range(20):
            if self.locate("封面") is not None:
                return
            self.scroll_page("up", 2)
        raise RuntimeError("B站封面区域未进入视野，已停止。")

    def bilibili_cover_signature(self, video, cover):
        import hashlib
        stat = video.stat()
        return hashlib.sha256((str(video.resolve()) + str(stat.st_size) + str(stat.st_mtime_ns)
            + hashlib.sha256(cover.read_bytes()).hexdigest()).encode("utf-8")).hexdigest()

    def bilibili_cover_is_confirmed(self, video, title, cover):
        previous = getattr(self, "bilibili_checkpoint", None)
        if not previous:
            return False
        if previous.get("cover_signature") != self.bilibili_cover_signature(video,cover):
            return False
        names = self.control_names()
        fields = [c for c in self.controls() if c.Name == "请输入稿件标题" and c.ControlTypeName == "EditControl"]
        return (video.stem in names and len(fields) == 1
                and fields[0].GetValuePattern().Value.strip() == title.strip())

    def wait_bilibili_form(self):
        deadline = self.now() + 30
        while self.now() < deadline:
            if "请输入稿件标题" in self.control_names():
                return
            time.sleep(0.2)
        raise RuntimeError("B站投稿表单未就绪。")

    def validate_bilibili_draft(self, video, title):
        fields = [c for c in self.controls() if c.Name == "请输入稿件标题" and c.ControlTypeName == "EditControl"]
        if len(fields) > 1:
            raise RuntimeError("B站存在多个投稿标题框，未修改稿件。")
        if fields:
            actual = fields[0].GetValuePattern().Value.strip()
            if actual not in ("",video.stem,title.strip()):
                raise RuntimeError(f"B站当前稿件标题{actual!r}与队列作品{title!r}不一致，未覆盖当前稿件。")

    def bilibili_tags_match(self, tags):
        controls = self.controls()
        names = [c.Name for c in controls]
        # 推荐标签也同名；容量和标签行内节点必须同时匹配。
        if not any(f"还可以添加{10-len(tags)}个标签" in name for name in names):
            return False
        actual=[]
        for c in controls:
            if c.Name not in tags:
                continue
            try:
                if getattr(c.GetParentControl(),"ClassName","")=="label-item-v2-content":
                    actual.append(c.Name)
            except COMError:
                return False
        return sorted(actual) == sorted(tags)

    def bilibili_description_matches(self, value):
        # 已填正文会作为完整Group/Text节点暴露，精确匹配才省略键盘操作。
        return any(c.Name.strip() == value.strip() and c.ControlTypeName in ("GroupControl", "TextControl")
                   for c in self.controls()) if value.strip() else False

    def wait_uploaded(self):
        deadline = self.now() + 180
        while self.now() < deadline:
            names = self.control_names()
            if "上传完成" in names:
                print("视频上传完成。", flush=True)
                return
            if any("上传失败" in name for name in names):
                raise RuntimeError("视频上传失败，请检查网络或文件。")
            time.sleep(0.5)
        raise RuntimeError("上传等待超过3分钟，流程已停止。")

    def fill_tags(self, tags):
        entry = self.find("按回车键Enter创建标签", editable=True)
        self.click(entry)
        self.keys("{Ctrl}a{Back}")
        # 空输入框Backspace逐个删除标签；以页面剩余容量确认，不用固定点击X坐标。
        for _ in range(21):
            if any("还可以添加10个标签" in name for name in self.control_names()):
                break
            self.keys("{Back}")
        else:
            raise RuntimeError("旧标签未能清理，请手动清空标签后重试。")
        self.click(self.find("按回车键Enter创建标签", editable=True))
        self.keyboard_field("tags")
        names = self.control_names()
        expected = f"还可以添加{10 - len(tags)}个标签"
        if not any(expected in name for name in names) or any(tag not in names for tag in tags):
            print("部分标签未被页面接受，保留已添加的标签并继续。", flush=True)
            return False
        return True

    def description(self, value):
        hint = "填写更全面的相关信息，让更多的人能找到你的视频吧"
        for control in self.controls():
            if control.Name == hint and self.visible(control):
                self.keyboard_fill(control, "description", value)
                return
        # 已填内容时提示消失：以“简介”所在行和标题输入框的列定位正文区域。
        # ponytail: 只支持B站当前表单布局；标签与输入列无法同时确认时停止。
        label = self.find("简介")
        title_controls = [item for item in self.controls() if item.Name == "请输入稿件标题"]
        if len(title_controls) != 1:
            raise RuntimeError("无法识别简介所在的表单输入列。")
        left = self.input_column()
        rect = label.BoundingRectangle
        self.check()
        self.click_at((left + 30, (rect.top + rect.bottom) // 2))
        self.keys("{Ctrl}a")
        # 先确认焦点处于正文，而非页面或其他输入框。
        focus = self.uia.GetFocusedControl()
        editor_types = ("EditControl", "DocumentControl", "GroupControl", "CustomControl")
        if focus is None or focus.ControlTypeName not in editor_types:
            raise RuntimeError("简介未获得编辑焦点，停止输入。")
        if focus.AutomationId == "RootWebArea":
            raise RuntimeError("焦点为整个页面，停止输入。")
        self.keyboard_field("description")
        self.verify_text(value, paragraph_copy=True)

    def reveal_schedule(self):
        """为下方一行日期/时间留空间；弹层可向上展开，不要求整张日历在下方。"""
        for _ in range(8):
            label = self.find("定时发布")
            if label.BoundingRectangle.bottom < self.window.BoundingRectangle.bottom - 160:
                return label
            self.scroll_page("down", 2)
        raise RuntimeError("定时发布区域未完整进入视野，请向下滚动页面后重试。")

    def schedule_fields(self):
        """仅识别定时发布行下方的日期/时间文本，不混淆时区输入框。"""
        label = self.find("定时发布")
        top = label.BoundingRectangle.top
        fields = [
            item for item in self.controls()
            if self.visible(item) and item.ControlTypeName == "TextControl"
            and top + 25 < item.BoundingRectangle.top < top + 90
        ]
        dates = [item for item in fields if re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.Name)]
        times = [item for item in fields if re.fullmatch(r"\d{2}:\d{2}", item.Name)]
        return choose_control(dates), choose_control(times)

    def schedule(self, selected):
        """通过日历和小时/分钟滚动列选择时间，并回读最终日期时间。"""
        label = self.reveal_schedule()
        date, clock = self.schedule_fields()
        if bool(date) != bool(selected):
            left = self.input_column()
            label = self.reveal_schedule()
            rect = label.BoundingRectangle
            self.check()
            self.click_at((left + 16, (rect.top + rect.bottom) // 2))
            time.sleep(0.2)
        if selected is None:
            if self.schedule_fields()[0] is not None:
                raise RuntimeError("未确认关闭定时发布。")
            return
        parse_publish_time(selected.strftime("%Y-%m-%d %H:%M"))
        self.reveal_schedule()
        date, clock = self.schedule_fields()
        for _ in range(25):
            if date is not None and clock is not None:
                break
            time.sleep(0.2)
            date, clock = self.schedule_fields()
        if date is None or clock is None:
            raise RuntimeError("未确认定时发布开关已开启或日期/时间控件未就绪，请检查定时发布区域；未提交。")
        try:
            current=datetime.strptime(f"{date.Name} {clock.Name}","%Y-%m-%d %H:%M").replace(tzinfo=selected.tzinfo)
        except ValueError:
            current=None
        if current and (current.date(),current.hour)==(selected.date(),selected.hour):
            self.actual_publish_at=parse_publish_time(current.strftime("%Y-%m-%d %H:%M"),require_five_minutes=False)
            return
        self.click(date)
        target_month = selected.strftime("%Y年%m月").replace("年0", "年")
        for _ in range(2):
            headers = [
                item for item in self.controls() if self.visible(item)
                and re.fullmatch(r"\d{4}年\d{1,2}月", item.Name)
            ]
            header = choose_control(headers)
            if header is None:
                raise RuntimeError("无法识别日期日历。")
            if header.Name == target_month:
                break
            # 日期范围最多15天，仅跨到下一个月；箭头相对日历标题定位。
            rect = header.BoundingRectangle
            self.check()
            self.click_at(((rect.left + rect.right) // 2 + 80, (rect.top + rect.bottom) // 2))
            time.sleep(0.2)
        else:
            raise RuntimeError("日历月份不符合所选日期。")
        rect = header.BoundingRectangle
        days = [
            item for item in self.controls()
            if item.Name.isdigit() and self.visible(item)
            and rect.top + 45 < item.BoundingRectangle.top < rect.top + 240
            and rect.left - 130 < item.BoundingRectangle.left < rect.right + 130
        ]
        from schedule_controls import month_calendar_day, scroll_select, wait_hour_readback
        self.click(month_calendar_day(days,selected.year,selected.month,selected.day,"B站"))
        _, clock = self.schedule_fields()
        self.click(clock)
        clock_rect = clock.BoundingRectangle
        # 用户仅指定小时，保留页面当前分钟；不再滚动分钟列。
        def hours():
            _, current_clock=self.schedule_fields()
            if current_clock is None:
                return []
            current_rect=current_clock.BoundingRectangle
            return [
                item for item in self.controls()
                if self.visible(item) and re.fullmatch(r"\d{2}", item.Name)
                and current_rect.top - 245 < item.BoundingRectangle.top < current_rect.top - 15
                and abs((item.BoundingRectangle.left + item.BoundingRectangle.right) / 2 - (current_rect.left + 15)) < 25
                and self.hour_option_in_view(item)
            ]
        scroll_select(self,hours,selected.hour,"B站小时")
        def value():
            date,clock=self.schedule_fields()
            return f"{date.Name} {clock.Name}" if date and clock else None
        selected_actual=wait_hour_readback(self,value,selected,"B站")
        self.click(self.find("定时发布"))
        selected_actual=wait_hour_readback(self,value,selected,"B站")
        actual = parse_publish_time(selected_actual.strftime("%Y-%m-%d %H:%M"), require_five_minutes=False)
        self.actual_publish_at = actual
        print(f"定时已核对：{actual:%Y-%m-%d %H:%M} 北京时间（保留页面分钟）。", flush=True)

    def hour_option_in_view(self, option):
        """小时数字必须完整位于滚动列内，屏幕可见不代表未被列表裁切。"""
        bounds=option.BoundingRectangle
        node=getattr(option,"control",option)
        for _ in range(5):
            parent=getattr(node,"GetParentControl",None)
            node=parent() if parent else None
            if node is None:
                return False
            if "time-picker-panel-select-wrp" in getattr(node,"ClassName","").split():
                viewport=node.BoundingRectangle
                return (viewport.left<=bounds.left<bounds.right<=viewport.right
                        and viewport.top<=bounds.top<bounds.bottom<=viewport.bottom)
        return False

    def run(self, video, title, description, tags, cover, category, publish_at):
        stage = getattr(self, "on_stage", None) or (lambda value: None)
        stage("entry")
        self.select_browser()
        if self.locate("再投一个"):
            self.next_upload()
        self.validate_bilibili_draft(video,title)
        self.start_keyboard_helper(video.parent / "发布文案.txt")
        self.upload(video)
        self.wait_bilibili_form()
        print("投稿表单已出现，视频上传期间开始填写。", flush=True)
        self.input_column()
        stage("cover")
        if not self.bilibili_cover_is_confirmed(video,title,cover):
            self.reveal_bilibili_cover()
            self.cover(cover)
            checkpoint = getattr(self,"on_bilibili_checkpoint",None)
            if checkpoint:
                checkpoint({"cover_signature":self.bilibili_cover_signature(video,cover)})
        stage("title")
        self.keyboard_fill(self.find("请输入稿件标题", editable=True), "title", title)
        print("标题已核对。", flush=True)
        stage("declaration")
        self.declaration()
        stage("category")
        self.category(category)
        stage("tags")
        if self.bilibili_tags_match(tags) or self.fill_tags(tags):
            print("标签已确认。", flush=True)
        stage("description")
        if not self.bilibili_description_matches(description):
            self.description(description)
        print("简介已核对。", flush=True)
        stage("schedule")
        self.schedule(publish_at)
        stage("upload_ready")
        self.wait_uploaded()
        stage("submit_ready")
        submit = self.find("立即投稿")
        self.check()
        self.submit(submit)
        stage("confirm_result")
        self.wait_name("稿件投递成功", timeout=60)
        print("稿件投递成功。", flush=True)

    def next_upload(self):
        self.click(self.wait_name("再投一个", timeout=10))
        self.wait_name("上传视频", timeout=20)
        self.form_left = None



def select_hour(date, hour, now=None):
    current = now or datetime.now(CHINA_TIME)
    try:
        start = datetime.strptime(f"{date} {hour}", "%Y-%m-%d %H").replace(tzinfo=CHINA_TIME)
    except ValueError as error:
        raise ValueError("请选择有效日期和小时。") from error
    for minute in range(0, 60, 5):
        candidate = start.replace(minute=minute)
        if current + timedelta(minutes=5) <= candidate <= current + timedelta(days=15):
            return candidate
    raise ValueError("这个小时没有可用时间，请选择当前时间5分钟后、15天以内的小时。")



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", nargs="?", type=Path)
    parser.add_argument("--check", action="store_true", help="只检查素材，不操作鼠标键盘")
    parser.add_argument("--publish-at", help="北京时间 YYYY-MM-DD HH:MM；空字符串表示不定时")
    args = parser.parse_args()
    if args.check:
        if args.folder is None:
            parser.error("--check需要指定素材目录")
        prepared = prepare_files(args.folder)
        cover = cover_from_folder(args.folder)
        category = category_from_folder(args.folder)
        parse_publish_time(args.publish_at or "")
        print(f"素材检查通过：{prepared[0]}；封面：{cover.name}；分区：{category}")
        return
    root = tk.Tk()
    root.withdraw()
    status = Path(__file__).with_name("bilibili_auto_status.json")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, "Local\\CreatorPublishHelperBilibiliDraft")
    if not mutex:
        root.destroy()
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        messagebox.showinfo("任务已在运行", "请等待当前任务结束，并关闭结果提示后再启动。")
        root.destroy()
        return
    try:
        from publish_queue import PublishQueue
        PublishQueue(root, BilibiliDraft, select_hour, prepare_files, cover_from_folder,
                     category_from_folder, CHINA_TIME, status, args.folder, args.publish_at).show()
    except Exception as error:
        status.write_text(json.dumps({
            "ok": False, "message": str(error), "traceback": traceback.format_exc(),
        }, ensure_ascii=False), encoding="utf-8")
        print(f"流程停止：{error}", flush=True)
        messagebox.showerror("B站自动化已停止", str(error))
    finally:
        kernel.CloseHandle(mutex)
        root.destroy()


if __name__ == "__main__":
    main()
