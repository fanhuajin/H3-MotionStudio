"""backend.publish_desktop — 用 **OS 级真实鼠标/键盘** 驱动浏览器网页的发布引擎（Windows）。

## 为什么是这条路（2026-09-29 用户口径：「必须鼠标真实操作 不然会被小红书封号」）

实测过三条路，只有这条同时满足「像真人」和「不碰浏览器调试接口」：

  ① **Playwright/CDP**（`social-auto-upload` 那类）——能力最强、最稳，但会给浏览器挂
     `--remote-debugging-port`，页面能用 `navigator.webdriver` / CDP 特征反查；用户不接受。
  ② **浏览器扩展 RPA**（本机 Edge 里就装着 Ui.Vision RPA）——XClick 也是系统级点击，
     但要装扩展、要付费模块，且扩展本身是显著特征。属于备选，不是首选。
  ③ **本模块**：**不挂 CDP、不加任何浏览器启动开关、不装扩展、不注入 DOM 事件**，
     只做两件事 —— 用 UIA 读控件位置（Chromium 会按需开放渲染树，实测**无需**
     `--force-renderer-accessibility`），再用 `SetCursorPos + mouse_event` 点、
     用 `SendInput`（含 KEYEVENTF_UNICODE）打。跑的就是用户**日常那个已登录的浏览器**，
     指纹与平时完全一致。

## 实测结论（2026-09-29，本机 Edge，125% 缩放，3440×1440）

  · UIA 能读到网页元素与精确矩形，例：按钮 rect `(88,404)-(174,436)`，
    与截图量出来的落点一致 → **定位可靠**；
  · 真实鼠标点击页面按钮 → 页面文字真的变成「已点击_OK」（截图 + UIA 双证）；
  · 真实键盘输入中文 → 输入框里真的出现「H3真实键盘测试」；
  · **踩过的坑**：点完/打完都没反应，真凶是 Edge 首次启动的**同步引导模态框**——
    模态框会吞掉一切输入，而 `PrintWindow` 只截页面层、截不到弹窗层，
    差点误判成「坐标不对」。所以每个动作前后都要 `WindowFromPoint` + 前台校验，
    并在开始前把挡路弹窗点掉。
  · **踩过的第二个坑（危险）**：`GetRootControl()` 全局扫「关闭」按钮会扫到
    **别的进程**的窗口（实测扫到用户满屏 Edge 的关闭按钮和 Notepad++），
    只因先命中离屏坐标才没出事。**本模块所有查找一律限定在目标窗口内**。

## 安全闸（写死不商量）

  1. 点击前 `WindowFromPoint(落点)` 必须属于目标窗口（或其子窗口）→ 否则拒绝点击；
  2. 输入前 `GetForegroundWindow()` 必须是目标窗口 → 否则重新抢前台，仍失败就抛错；
  3. 全程可 **ESC 急停**（`_check_abort`）；
  4. UIA 查找只在目标窗口子树里走，绝不 `GetRootControl()` 全局扫描；
  5. 每一步可选截图留证，落在 `--log-dir` 下。

## CLI（逐步驱动，便于单步标定）

    python -m backend.publish_desktop windows                     # 列出浏览器窗口
    python -m backend.publish_desktop open --url <url>            # 用日常 Edge 打开（无任何开关）
    python -m backend.publish_desktop map --title 投稿            # 打印页面控件清单（标定用）
    python -m backend.publish_desktop shot --title 投稿 --out a.png
    python -m backend.publish_desktop bilibili --folder "<发布成品目录>"
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import logging
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# **必须在任何窗口/截图/UIA 操作之前**设为 DPI 感知，否则 125%/150% 缩放下坐标整体错位
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:  # pragma: no cover
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

from backend import jianying_desktop as jy  # noqa: E402
from backend.jianying_uia import uia_in_thread  # noqa: E402

try:
    import uiautomation as auto
except ImportError as exc:  # pragma: no cover
    raise SystemExit("需要 pip install uiautomation") from exc

logger = logging.getLogger("publish.desktop")

user32 = ctypes.windll.user32
auto.SetGlobalSearchTimeout(3)

# Edge 与 Chrome 的主窗口类名相同；这里只靠标题区分具体页面。
BROWSER_CLASSES = ("Chrome_WidgetWin_1",)

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

VK_ESCAPE = 0x1B


class Aborted(RuntimeError):
    """ESC 急停或安全闸拒绝执行。"""


class GuardError(RuntimeError):
    """安全闸拦截：落点不属于目标窗口 / 前台不是目标窗口。"""


# ---------------------------------------------------------------------------
# 窗口发现
# ---------------------------------------------------------------------------
@dataclass
class BrowserWindow:
    hwnd: int
    title: str
    pid: int
    rect: tuple[int, int, int, int]
    control: Any = None

    def attach(self) -> "BrowserWindow":
        self.control = auto.ControlFromHandle(self.hwnd)
        return self

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


def list_browsers() -> list[BrowserWindow]:
    """列出所有可见的 Edge/Chrome 主窗口（只读，不做任何操作）。"""
    rows: list[BrowserWindow] = []

    def cb(hwnd: int, _param) -> bool:
        try:
            if win32gui.GetClassName(hwnd) not in BROWSER_CLASSES:
                return True
            if not win32gui.IsWindowVisible(hwnd):
                return True
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] < 200 or rect[3] - rect[1] < 200:
                return True
            rows.append(BrowserWindow(hwnd=hwnd, title=win32gui.GetWindowText(hwnd),
                                      pid=win32process.GetWindowThreadProcessId(hwnd)[1],
                                      rect=rect))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return rows


def find_browser(keywords: Iterable[str] = (), *, timeout: float = 0.0,
                 prefer_foreground: bool = True) -> BrowserWindow | None:
    """按标题关键词找浏览器窗口（全部关键词都要命中）。找不到返回 None。

    `prefer_foreground=True` 时优先返回当前前台的窗口（刚 open_url 打开的那个标签就在它里面）。
    """
    keys = [k for k in keywords if k]
    deadline = time.time() + max(0.0, timeout)
    while True:
        rows = list_browsers()
        hit = None
        for w in rows:
            if all(k in w.title for k in keys):
                hit = w
                break
        if hit is not None:
            if prefer_foreground:
                fg = win32gui.GetForegroundWindow()
                same_pid = [w for w in rows if w.pid == hit.pid]
                for w in same_pid:
                    if w.hwnd == fg:
                        return w
            return hit
        if time.time() >= deadline:
            return None
        time.sleep(0.8)


def browser_exe(*, prefer: str = "edge") -> str:
    table = {"edge": EDGE_CANDIDATES, "chrome": CHROME_CANDIDATES}
    for path in table.get(prefer, EDGE_CANDIDATES):
        if Path(path).is_file():
            return path
    # 回退：另一个
    for paths in table.values():
        for path in paths:
            if Path(path).is_file():
                return path
    raise RuntimeError("本机没找到 Edge/Chrome 可执行文件")


def open_url(url: str, *, prefer: str = "edge") -> None:
    """用日常浏览器打开一个地址（**不加任何开关**）。

    浏览器已在跑时，这会复用同一个实例、开一个新标签页 —— 登录态、指纹、扩展全都与平时一致。

    ⚠️ 注意：这样开出来的标签页**可能在后台**（启动进程不在前台，Windows 不保证把它切到前台），
    所以调用方必须自己再去标签栏里把目标标签点开并复验，不能假设「开完就是当前页」。
    """
    subprocess.Popen([browser_exe(prefer=prefer), url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def virtual_screen() -> tuple[int, int, int, int]:
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(76) + user32.GetSystemMetrics(78),
            user32.GetSystemMetrics(77) + user32.GetSystemMetrics(79))


# ---------------------------------------------------------------------------
# 控件定位（只在目标窗口子树里走）
# ---------------------------------------------------------------------------
@dataclass
class Node:
    name: str
    ctype: str
    rect: tuple[int, int, int, int]
    control: Any = None

    @property
    def center(self) -> tuple[int, int]:
        return ((self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2)

    @property
    def visible(self) -> bool:
        return self.rect[2] > self.rect[0] and self.rect[3] > self.rect[1]


def _walk(root: Any, *, limit: int = 4000, max_depth: int = 30) -> list[Node]:
    rows: list[Node] = []
    stack: list[tuple[Any, int]] = [(root, 0)]
    while stack and len(rows) < limit:
        ctrl, depth = stack.pop()
        try:
            r = ctrl.BoundingRectangle
            rect = (r.left, r.top, r.right, r.bottom)
            rows.append(Node(name=(ctrl.Name or "").strip(), ctype=ctrl.ControlTypeName,
                             rect=rect, control=ctrl))
        except Exception:
            pass
        if depth >= max_depth:
            continue
        try:
            for child in ctrl.GetChildren():
                stack.append((child, depth + 1))
        except Exception:
            pass
    return rows


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
@dataclass
class PublishSession:
    """一次发布操作的上下文：定位、真实输入、安全闸、留证。"""

    window: BrowserWindow
    log_dir: Path = field(default_factory=lambda: Path("data/publish-runs"))
    dry_run: bool = False
    shot_every_step: bool = True
    abort_key: int = VK_ESCAPE
    steps: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.window.attach()
        self.log_dir = Path(self.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._shots: list[Path] = []

    # ---------------- 安全闸 ----------------
    def _check_abort(self) -> None:
        if user32.GetAsyncKeyState(self.abort_key) & 0x8000:
            raise Aborted("检测到 ESC —— 已急停（未继续任何输入）")

    def _assert_foreground(self, *, tries: int = 3) -> None:
        hwnd = self.window.hwnd
        for _ in range(max(1, tries)):
            if win32gui.GetForegroundWindow() == hwnd:
                return
            jy.force_foreground_hard(hwnd)
            time.sleep(0.35)
        if win32gui.GetForegroundWindow() != hwnd:
            raise GuardError(f"抢前台失败，拒绝输入（前台={win32gui.GetForegroundWindow()} 目标={hwnd}）")

    def _guard_point(self, x: int, y: int) -> None:
        """落点必须属于目标窗口 —— 否则一律拒绝点击（防点错窗口/被弹窗盖住）。"""
        hwnd_at = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
        if not hwnd_at:
            raise GuardError(f"落点 ({x},{y}) 没有窗口，拒绝点击")
        root = win32gui.GetAncestor(hwnd_at, 2)  # GA_ROOT
        if root != self.window.hwnd:
            try:
                pid = win32process.GetWindowThreadProcessId(root)[1]
            except Exception:
                pid = None
            raise GuardError(
                f"落点 ({x},{y}) 属于别的窗口（hwnd={root} pid={pid} "
                f"class={win32gui.GetClassName(hwnd_at)}），拒绝点击")
        l, t, r, b = self.window.rect
        if not (l <= x <= r and t <= y <= b):
            raise GuardError(f"落点 ({x},{y}) 在目标窗口矩形外 {self.window.rect}，拒绝点击")

    # ---------------- 定位 ----------------
    def in_window(self, rect: tuple[int, int, int, int], *, margin: int = 0) -> bool:
        l, t, r, b = self.window.rect
        return (rect[0] >= l - margin and rect[2] <= r + margin
                and rect[1] >= t - margin and rect[3] <= b + margin)

    def active_page_root(self) -> Any:
        """**当前激活标签页**的渲染子树根。

        ⚠️ 2026-09-29 实测踩到的坑：直接走整窗口的 UIA 树，会把**后台标签页**的内容
        也走一遍 —— 树里混进了另一个标签页（我自己的聊天页）里出现的「上传视频」四个字，
        于是脚本把聊天里的文字当成了 B站 的上传按钮，算出一个 y=-1111 的落点。
        修法：用 `WindowFromPoint(页面中心)` 拿到真正在显示的那个
        `Chrome_RenderWidgetHostHWND`，只走它的子树。
        """
        l, t, r, b = self.window.rect
        for fx, fy in ((0.5, 0.45), (0.5, 0.7), (0.35, 0.5)):
            x, y = int(l + (r - l) * fx), int(t + (b - t) * fy)
            try:
                handle = user32.WindowFromPoint(wt.POINT(x, y))
                if handle and win32gui.GetClassName(handle) == "Chrome_RenderWidgetHostHWND":
                    return auto.ControlFromHandle(handle)
            except Exception:
                continue
        return self.window.control

    def snapshot(self) -> list[Node]:
        """**激活标签页内容**里的控件，且只保留完整落在窗口内的。

        过滤"落在窗口内"是第二道保险：后台标签页的节点偶尔会算出窗口内的坐标。
        """
        return [n for n in _walk(self.active_page_root()) if self.in_window(n.rect)]

    def snapshot_window(self) -> list[Node]:
        """**整窗口**的控件（含浏览器外壳：标签栏、地址栏、工具栏）。

        外壳不在页面的渲染子树里，所以找标签页/地址栏必须用这个。
        """
        return [n for n in _walk(self.window.control) if self.in_window(n.rect)]

    def title_has(self, *keywords: str) -> bool:
        """窗口标题（= 当前激活标签页的标题）里是否含关键词。"""
        return all(k in self.live_title() for k in keywords)

    def live_title(self) -> str:
        """**实时**窗口标题（`window.title` 是发现窗口那一刻的快照，切标签后会过期）。"""
        try:
            return win32gui.GetWindowText(self.window.hwnd)
        except Exception:
            return self.window.title

    def refresh_rect(self) -> tuple[int, int, int, int]:
        try:
            self.window.rect = win32gui.GetWindowRect(self.window.hwnd)
        except Exception:
            pass
        return self.window.rect

    def ensure_fully_visible(self) -> bool:
        """窗口必须**整个在屏幕内**，否则靠右/靠下的元素点不到（实测遇到过右侧超出屏幕的窗口）。

        不在屏幕内就最大化它，并刷新 rect。
        """
        vl, vt, vr, vb = virtual_screen()
        l, t, r, b = self.refresh_rect()
        if l >= vl and t >= vt and r <= vr and b <= vb:
            return True
        logger.info("窗口 %s 超出屏幕 %s，最大化它", (l, t, r, b), (vl, vt, vr, vb))
        try:
            win32gui.ShowWindow(self.window.hwnd, win32con.SW_MAXIMIZE)
        except Exception:
            pass
        time.sleep(1.2)
        self.refresh_rect()
        return True

    def find(self, *, name: str | None = None, contains: str | None = None,
             ctype: str | None = None, regex: str | None = None,
             require_visible: bool = True, nodes: list[Node] | None = None,
             timeout: float = 0.0) -> Node | None:
        """在目标窗口子树里找第一个匹配控件；`timeout` 内轮询等待。"""
        import re

        pattern = re.compile(regex) if regex else None
        deadline = time.time() + max(0.0, timeout)
        while True:
            rows = nodes if nodes is not None else self.snapshot()
            for node in rows:
                if ctype and node.ctype != ctype:
                    continue
                if require_visible and not node.visible:
                    continue
                if name is not None and node.name != name:
                    continue
                if contains is not None and contains not in node.name:
                    continue
                if pattern is not None and not pattern.search(node.name):
                    continue
                return node
            if time.time() >= deadline:
                return None
            time.sleep(0.6)
            nodes = None

    def find_all(self, *, contains: str | None = None, ctype: str | None = None,
                 require_visible: bool = True) -> list[Node]:
        rows = self.snapshot()
        out = []
        for node in rows:
            if ctype and node.ctype != ctype:
                continue
            if require_visible and not node.visible:
                continue
            if contains is not None and contains not in node.name:
                continue
            out.append(node)
        return out

    def map(self, *, limit: int = 400, only_named: bool = True,
            inside_only: bool = True) -> list[dict[str, Any]]:
        """标定用：把**当前激活标签页**里能点到的东西列出来。"""
        rows_source = self.snapshot() if inside_only else _walk(self.active_page_root())
        rows = []
        for node in rows_source[:limit]:
            if only_named and not node.name:
                continue
            rows.append({"type": node.ctype, "name": node.name[:80],
                         "rect": list(node.rect), "visible": node.visible,
                         "inWindow": self.in_window(node.rect)})
        return rows

    # ---------------- 输入 ----------------
    def move_to(self, x: int, y: int, *, steps: int = 12) -> None:
        cur = auto.GetCursorPos()
        cx, cy = (cur.x, cur.y) if hasattr(cur, "x") else (cur[0], cur[1])
        for i in range(1, steps + 1):
            user32.SetCursorPos(int(cx + (x - cx) * i / steps), int(cy + (y - cy) * i / steps))
            time.sleep(random.uniform(0.008, 0.018))

    def click_xy(self, x: int, y: int, label: str = "", *, double: bool = False,
                 settle: float = 0.7) -> tuple[int, int]:
        self._check_abort()
        self._assert_foreground()
        self._guard_point(x, y)
        logger.info("点击 %s (%s, %s)%s", label or "", x, y, " ×2" if double else "")
        if self.dry_run:
            return x, y
        self.move_to(x, y)
        time.sleep(random.uniform(0.25, 0.45))
        for _ in range(2 if double else 1):
            user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(random.uniform(0.06, 0.11))
            user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            if double:
                time.sleep(0.1)
        time.sleep(settle)
        return x, y

    def click(self, node: Node, label: str = "", **kw) -> tuple[int, int]:
        x, y = node.center
        return self.click_xy(x, y, label or node.name, **kw)

    def _send_unicode(self, text: str) -> None:
        """用 SendInput + KEYEVENTF_UNICODE 逐字打（OS 级真实输入，不走剪贴板）。

        自己的实现而不是 pywinauto 的 `send_keys`：后者把 `{}()+^%~` 当语法解析，
        标题/简介里出现这些字符就会打错或抛错。
        """
        for ch in text:
            self._check_abort()
            _send_unicode_text(ch, delay=0.02)

    def type_text(self, text: str, label: str = "", *, clear_first: bool = False,
                  settle: float = 0.5) -> None:
        self._check_abort()
        self._assert_foreground()
        logger.info("输入 %s：%r", label or "", text[:60])
        if self.dry_run:
            return
        if clear_first:
            self.press("^a", label="全选", settle=0.2)
            self.press("{DELETE}", label="清空", settle=0.2)
        self._send_unicode(text)
        time.sleep(settle)

    def press(self, keys: str, label: str = "", *, settle: float = 0.45) -> None:
        self._check_abort()
        self._assert_foreground()
        logger.info("按键 %s %s", label or "", keys)
        if self.dry_run:
            return
        from pywinauto.keyboard import send_keys

        send_keys(keys, pause=0.05)
        time.sleep(settle)

    # ---------------- 取证 ----------------
    def shot(self, tag: str) -> Path | None:
        try:
            img = jy.capture(self.window.hwnd)
            safe = Path(str(tag)).name  # 传进来的是完整路径时只取文件名，避免拼出非法路径
            safe = "".join(ch for ch in safe if ch not in '\\/:*?"<>|').strip() or "shot"
            out = self.log_dir / f"{len(self._shots) + 1:02d}_{safe}.png"
            img.save(out)
            self._shots.append(out)
            return out
        except Exception as error:  # 截图失败不该中断发布
            logger.warning("截图失败（%s）：%s", tag, error)
            return None

    def step(self, title: str, *, shot: bool = True) -> None:
        self.steps.append(title)
        logger.info("=== %s ===", title)
        if shot and self.shot_every_step:
            self.shot(title.replace(" ", "_").replace("/", "_")[:40])


# ---------------------------------------------------------------------------
# 原生文件对话框
# ---------------------------------------------------------------------------
FILE_DIALOG_TITLES = ("打开", "Open", "另存为", "Save As", "选择", "Select")
BROWSER_PROCS = ("msedge.exe", "chrome.exe")


def process_name(pid: int) -> str:
    """进程映像名（用来判断对话框是不是浏览器弹的）。"""
    handle = user32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if user32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
    finally:
        user32.CloseHandle(handle)
    return ""


def file_dialogs() -> dict[int, dict[str, Any]]:
    """当前所有可见的文件对话框（`#32770`）。

    ⚠️ 关键实测（2026-09-29）：Chromium 的文件选择器跑在**独立进程**里 ——
    实测对话框 pid=17848，而浏览器主窗口 pid=20704，所以**不能**用
    「pid 必须等于浏览器窗口的 pid」来筛选（上一版就是这么把它漏掉的，
    表现是「点了上传按钮，但脚本说没看到对话框」）。
    """
    rows: dict[int, dict[str, Any]] = {}

    def cb(hwnd: int, _param) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.GetClassName(hwnd) != "#32770":
                return True
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] < 300 or rect[3] - rect[1] < 200:
                return True
            title = win32gui.GetWindowText(hwnd)
            if not any(t in title for t in FILE_DIALOG_TITLES):
                return True
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            rows[hwnd] = {"hwnd": hwnd, "pid": pid, "proc": process_name(pid),
                          "title": title, "rect": list(rect)}
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return rows


def _filename_edit(dialog: Any) -> Any:
    """对话框里的「文件名」输入框：优先名字带「文件名」的，否则取最靠下的那个。"""
    edits = []
    stack = [dialog]
    while stack:
        ctrl = stack.pop()
        try:
            r = ctrl.BoundingRectangle
            if ctrl.ControlTypeName == "EditControl" and r.right > r.left and r.bottom > r.top:
                edits.append(ctrl)
        except Exception:
            pass
        try:
            stack.extend(ctrl.GetChildren())
        except Exception:
            pass
    for ctrl in edits:
        if "文件名" in (ctrl.Name or ""):
            return ctrl
    if edits:
        return max(edits, key=lambda c: c.BoundingRectangle.top)
    return None


def _open_button(dialog: Any) -> Any:
    stack = [dialog]
    while stack:
        ctrl = stack.pop()
        try:
            name = (ctrl.Name or "").strip()
            r = ctrl.BoundingRectangle
            if (ctrl.ControlTypeName == "ButtonControl" and r.right > r.left
                    and (name.startswith("打开") or name in ("Open", "Save"))):
                return ctrl
        except Exception:
            pass
        try:
            stack.extend(ctrl.GetChildren())
        except Exception:
            pass
    return None


def _send_unicode_text(text: str, *, delay: float = 0.004) -> None:
    """SendInput + KEYEVENTF_UNICODE 逐字打（OS 级真实输入，不依赖剪贴板/焦点外的粘贴）。"""
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", _U)]

    def one(ch: str, flags: int) -> None:
        inp = INPUT(type=INPUT_KEYBOARD,
                    union=_U(ki=KEYBDINPUT(wVk=0, wScan=ord(ch),
                                           dwFlags=KEYEVENTF_UNICODE | flags,
                                           time=0, dwExtraInfo=None)))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    for ch in text:
        one(ch, 0)
        time.sleep(delay)
        one(ch, KEYEVENTF_KEYUP)
        time.sleep(delay)


def _read_edit_value(ctrl: Any) -> str:
    try:
        if ctrl.IsValuePatternAvailable():
            return ctrl.GetValuePattern().Value or ""
    except Exception:
        pass
    try:
        return ctrl.Name or ""
    except Exception:
        return ""


def pick_file_in_dialog(path: Path, *, timeout: float = 25.0,
                        before: set[int] | None = None, log: bool = True,
                        only_procs: tuple[str, ...] | None = None,
                        verify_rounds: int = 2) -> bool:
    """处理浏览器弹出的原生「打开」对话框：**填对路径** → 点「打开」。

    `before`：点击上传按钮**之前**已存在的对话框 hwnd 集合（用来排除别人家的窗口）。
    `only_procs`：只认这些进程名的对话框（处理"屏幕上已开着的"时防误碰别的程序）。

    步骤（每一步都回读校验，不再"看起来在动其实没填进去"）：
      ① 轮询 0.25s 等对话框出现；
      ② 点中「文件名」输入框 → Ctrl+A 清空；
      ③ 用 SendInput 逐字真实键入**完整绝对路径**（Windows 文件对话框里直接输全路径最可靠，
         不受对话框"记住的上次目录"影响 —— 实测它上次停在 024 目录，所以必须打全路径）；
      ④ **回读输入框内容**，确认末尾就是目标文件名；不对就重来一轮；
      ⑤ 点「打开(O)」；3 秒内对话框消失才算成功。
    """
    target = str(Path(path).resolve())
    before = set(before or ())
    wanted = tuple(p.lower() for p in only_procs) if only_procs else None
    deadline = time.time() + max(0.0, timeout)
    info = None
    while time.time() < deadline and info is None:
        for hwnd, row in file_dialogs().items():
            if hwnd in before:
                continue
            if wanted is not None and row["proc"].lower() not in wanted:
                continue
            info = row
            break
        if info is None:
            time.sleep(0.25)
    if info is None:
        return False

    dialog = auto.ControlFromHandle(info["hwnd"])
    if log:
        logger.info("文件对话框：%r proc=%s pid=%s", info["title"], info["proc"], info["pid"])
    jy.force_foreground_hard(info["hwnd"])
    time.sleep(0.4)

    from pywinauto.keyboard import send_keys

    edit = _filename_edit(dialog)
    if edit is None:
        logger.warning("对话框里没找到文件名输入框：%r", info)
        return False

    for attempt in range(1, max(1, verify_rounds) + 1):
        rect = edit.BoundingRectangle
        x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        user32.SetCursorPos(x, y)
        time.sleep(0.25)
        user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.06)
        user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.35)
        send_keys("^a", pause=0.03)
        time.sleep(0.15)
        send_keys("{DELETE}", pause=0.03)
        time.sleep(0.15)
        _send_unicode_text(target)
        time.sleep(0.5)

        value = _read_edit_value(edit)
        tail = Path(target).name
        if tail in value:
            if log:
                logger.info("已填入路径（第 %s 次）：%s", attempt, value[-60:])
            break
        logger.warning("第 %s 次填路径没生效（框里是 %r），重试", attempt, value[-60:])
    else:
        logger.warning("反复都填不进文件名，放弃（不点「打开」，避免选错文件）")
        return False

    button = _open_button(dialog)
    if button is not None:
        brect = button.BoundingRectangle
        user32.SetCursorPos((brect.left + brect.right) // 2, (brect.top + brect.bottom) // 2)
        time.sleep(0.25)
        user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.06)
        user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    else:
        send_keys("{ENTER}", pause=0.03)

    for _ in range(12):  # 最多等 3 秒
        time.sleep(0.25)
        if info["hwnd"] not in file_dialogs():
            if log:
                logger.info("已确认：%s", tail)
            return True
    logger.warning("点了「打开」但对话框没关（可能路径无效）")
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_windows(_args: argparse.Namespace) -> int:
    for w in list_browsers():
        print(f"hwnd={w.hwnd} pid={w.pid} rect={w.rect} title={w.title[:90]!r}")
    return 0


def _cmd_open(args: argparse.Namespace) -> int:
    open_url(args.url, prefer=args.browser)
    print("已用日常浏览器打开（未加任何启动开关）：", args.url)
    return 0


def _attach(args: argparse.Namespace) -> BrowserWindow | None:
    keys = list(args.title or [])
    win = find_browser(keys, timeout=args.timeout)
    if win is None:
        print("没找到匹配的浏览器窗口。当前有：")
        for w in list_browsers():
            print(f"  hwnd={w.hwnd} title={w.title[:90]!r}")
        return None
    return win


def _cmd_map(args: argparse.Namespace) -> int:
    with uia_in_thread():
        win = _attach(args)
        if win is None:
            return 1
        session = PublishSession(window=win, log_dir=Path(args.log_dir), dry_run=True,
                                 shot_every_step=False)
        rows = session.map(limit=args.limit, only_named=not args.all,
                           inside_only=not args.whole_tree)
        print(f"窗口：{win.title!r}  rect={win.rect}  控件 {len(rows)} 个")
        if args.contains:
            rows = [r for r in rows if args.contains in r["name"]]
        for r in rows:
            print(f"  [{r['type']:<22}] {r['name'][:60]!r:<64} {r['rect']}")
        if args.out:
            Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            print("已写出：", args.out)
        if args.shot:
            p = session.shot("map")
            print("截图：", p)
    return 0


def _cmd_shot(args: argparse.Namespace) -> int:
    with uia_in_thread():
        win = _attach(args)
        if win is None:
            return 1
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        jy.capture(win.hwnd).save(out)
        print("截图：", out)
    return 0


def _cmd_fill_dialog(args: argparse.Namespace) -> int:
    """把**现在正开着**的文件对话框填上路径并确认（对话框卡在屏幕上时用）。"""
    with uia_in_thread():
        rows = file_dialogs()
        if not rows:
            print("现在没有开着的文件对话框。")
            return 1
        for hwnd, row in rows.items():
            print(f"  hwnd={hwnd} proc={row['proc']} pid={row['pid']} "
                  f"title={row['title']!r} rect={row['rect']}")
        ok = pick_file_in_dialog(Path(args.path), timeout=3.0)
        print("已提交" if ok else "⚠️ 没成功（对话框仍在）")
        return 0 if ok else 1


def _cmd_bilibili(args: argparse.Namespace) -> int:
    from backend import publish_bilibili

    return publish_bilibili.publish_folder(Path(args.folder), log_dir=Path(args.log_dir),
                                           dry_run=args.dry_run, stop_after=args.stop_after,
                                           skip_upload=args.skip_upload)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="真实鼠标/键盘发布引擎（Windows / Edge）")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("windows", help="列出浏览器窗口")
    w.set_defaults(func=_cmd_windows)

    o = sub.add_parser("open", help="用日常浏览器打开地址（不加任何开关）")
    o.add_argument("--url", required=True)
    o.add_argument("--browser", default="edge", choices=["edge", "chrome"])
    o.set_defaults(func=_cmd_open)

    m = sub.add_parser("map", help="打印窗口内控件清单（标定用）")
    m.add_argument("--title", action="append", default=[], help="标题必须包含（可多次）")
    m.add_argument("--timeout", type=float, default=10.0)
    m.add_argument("--limit", type=int, default=400)
    m.add_argument("--all", action="store_true", help="连没有名字的控件也列")
    m.add_argument("--whole-tree", action="store_true", help="连窗口外的节点也列（含后台标签页）")
    m.add_argument("--contains", default="", help="只看名字含该词的")
    m.add_argument("--shot", action="store_true")
    m.add_argument("--out", default="")
    m.add_argument("--log-dir", default="data/publish-runs")
    m.set_defaults(func=_cmd_map)

    s = sub.add_parser("shot", help="截目标窗口")
    s.add_argument("--title", action="append", default=[])
    s.add_argument("--timeout", type=float, default=10.0)
    s.add_argument("--out", default="data/publish-runs/window.png")
    s.add_argument("--log-dir", default="data/publish-runs")
    s.set_defaults(func=_cmd_shot)

    fd = sub.add_parser("fill-dialog", help="处理现在正开着的文件对话框（填路径→确认）")
    fd.add_argument("--path", required=True, help="要选择的文件完整路径")
    fd.set_defaults(func=_cmd_fill_dialog)

    b = sub.add_parser("bilibili", help="把一条发布成品发到 B站（停在「立即投稿」前）")
    b.add_argument("--folder", required=True, help="发布成品目录")
    b.add_argument("--dry-run", action="store_true", help="只走定位，不做任何输入")
    b.add_argument("--skip-upload", action="store_true", help="跳过上传（页面已在投稿表单时用）")
    b.add_argument("--stop-after", default="form", choices=["upload", "form"],
                   help="upload=只上传并抓表单（标定用）；form=填完表停在投稿前")
    b.add_argument("--log-dir", default="data/publish-runs")
    b.set_defaults(func=_cmd_bilibili)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
