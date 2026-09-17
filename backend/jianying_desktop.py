"""backend.jianying_desktop — 驱动剪映专业版（Qt6 原生应用）。

为什么是「截图 + 坐标 + 键盘」这条路（三条路都已实测）：
  - **CDP**：给剪映挂 `--remote-debugging-port` 端口能开（`JIANYING_PC/11.3.0`），
    但主窗口是 Qt 原生渲染，CDP 里只有一个 `about:blank`，**拿不到编辑界面 DOM**。
  - **UIA**：主窗口类 `Qt622QWindowIcon`，`descendants()` 返回 **0 个控件**，
    Qt 默认不对 UIA 暴露内部结构 —— 拿不到按钮。
  - **PrintWindow 截图**：实测可用（非黑图、灰度分布正常），且**不抢前台**就能读状态。

所以本模块提供：窗口定位/置前、窗口内比例与像素坐标点击、剪贴板粘贴中文、
按键、截图、等待画面稳定（用于「识别歌词」这类耗时操作）。
坐标一律相对窗口，与屏幕分辨率、DPI 缩放解耦。

用法（命令行逐步驱动，便于单步验证）：
    python -m backend.jianying_desktop find
    python -m backend.jianying_desktop capture --out .tmp_jy.png
    python -m backend.jianying_desktop click --px 829 150
    python -m backend.jianying_desktop click --ratio 0.57 0.16
    python -m backend.jianying_desktop paste --text "剪映手书"
    python -m backend.jianying_desktop key --keys "{ENTER}"
    python -m backend.jianying_desktop wait-still --quiet 3 --timeout 300
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import time
from pathlib import Path

# **必须在任何窗口/截图操作之前**设为 DPI 感知，否则 125%/150% 缩放下坐标整体错位
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import win32api
import win32clipboard
import win32con
import win32gui
import win32process

from PIL import Image, ImageChops

logger = logging.getLogger("jianying.desktop")

user32 = ctypes.windll.user32

MAIN_TITLE = "剪映专业版"
MAIN_CLASS = "Qt622QWindowIcon"

# 本机实际在用的是 **免安装解锁版 5.9.0.11632**（草稿的 app_version 全是 5.9.0）。
# 注意：`%LOCALAPPDATA%\JianyingPro\Apps\<版本>` 是**自动更新器下载的新版安装位置**（实测被下过
# 11.3.0.14362），用户要求锁死 5.9.0，所以这里只认解锁版目录，绝不去拉起 Apps 下的新版。
#   锁定手段：User Data\Config\globalSetting（enableAutoUpdate/totalSilentUpgradeSwitch=false，只读）
#             + updateInfo（joinBeta=false）+ 5.9.0.11632\update.exe 已改名停用。
JY_ROOT = Path(os.getenv("H3_JIANYING_ROOT")
               or r"D:\soft\剪映以激活版本免安装\剪映以激活版本免安装\剪映解锁版\JianyingPro")
EXE = JY_ROOT / "解锁版.exe"                       # 必须先过解锁启动器，不能直接起内部 exe
EXE_INNER = JY_ROOT / "5.9.0.11632" / "JianyingPro.exe"
EXE_LAUNCHER = EXE


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------
def _window_rows() -> list[tuple[int, str, str, tuple[int, int, int, int], bool, bool]]:
    rows: list[tuple[int, str, str, tuple[int, int, int, int], bool, bool]] = []

    def cb(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        if not (title == MAIN_TITLE or (cls.startswith("Qt6") and "剪映" in title)):
            return
        rows.append((hwnd, cls, title, win32gui.GetWindowRect(hwnd),
                     bool(win32gui.IsWindowVisible(hwnd)), bool(win32gui.IsIconic(hwnd))))

    win32gui.EnumWindows(cb, None)
    return rows


def find_window(*, restore: bool = True) -> int | None:
    """找剪映主窗口；只有最小化的就恢复它。

    注意：最小化窗口 `IsWindowVisible` 仍为 True，但 rect 是 (-25600,…) 且尺寸畸形，
    拿去算坐标会全错，所以这里按「可见且未最小化」优先。
    """
    rows = _window_rows()
    if not rows:
        return None

    def area(r):
        x1, y1, x2, y2 = r[3]
        return max(0, x2 - x1) * max(0, y2 - y1)

    normal = [r for r in rows if r[4] and not r[5]]
    if normal:
        return max(normal, key=area)[0]
    if restore:
        hwnd = max(rows, key=area)[0]
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(1.0)
        except Exception:
            pass
        return hwnd
    return None


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    return win32gui.GetWindowRect(hwnd)


def has_window() -> bool:
    """剪映是否还有任何顶层窗口（含最小化的）——用来判断「剪映还在不在」。

    `find_window(restore=False)` 对**最小化**的窗口也返回 None（那是它刻意的语义：
    最小化窗口的 rect 是畸形值，不能拿去算坐标）。识别过程中要判断剪映有没有被关掉，
    不能把「被最小化」误判成「退出了」，所以单独给一个只看窗口存在性的接口。
    """
    return bool(_window_rows())


def list_windows(pid: int | None = None) -> list[dict]:
    """列出剪映进程的所有顶层窗口。

    **重要**：剪映的 Qt 下拉菜单/对话框/文件选择器都是**独立顶层窗口**，
    PrintWindow 抓主窗口时看不到它们 —— 所以每一步之后要靠这个函数确认
    「弹出层到底出来没有、在哪、多大」。
    """
    if pid is None:
        hwnd = find_window(restore=False)
        if not hwnd:
            return []
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    rows: list[dict] = []

    def cb(h, _):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(h)
            if wpid != pid:
                return
            rect = win32gui.GetWindowRect(h)
            rows.append({
                "hwnd": h,
                "class": win32gui.GetClassName(h),
                "title": win32gui.GetWindowText(h),
                "rect": rect,
                "size": (rect[2] - rect[0], rect[3] - rect[1]),
                "visible": bool(win32gui.IsWindowVisible(h)),
                "popup": bool(win32gui.GetWindowLong(h, win32con.GWL_STYLE) & win32con.WS_POPUP),
            })
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    return [r for r in rows if r["visible"] and r["size"][0] > 40 and r["size"][1] > 20]


def _rect_is_sane(rect: tuple[int, int, int, int]) -> bool:
    x1, y1, x2, y2 = rect
    return (x2 - x1) >= 800 and (y2 - y1) >= 600 and x1 > -10000 and y1 > -10000


def ensure_window(hwnd: int) -> tuple[int, int, int, int]:
    """确保窗口已恢复且尺寸合理，返回可用的 rect（否则抛错，绝不拿错坐标乱点）。"""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(1.0)
    rect = window_rect(hwnd)
    if not _rect_is_sane(rect):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            time.sleep(0.4)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(1.0)
        except Exception:
            pass
        rect = window_rect(hwnd)
    if not _rect_is_sane(rect):
        raise RuntimeError(f"剪映窗口尺寸异常，已拒绝操作：rect={rect}")
    return rect


def force_foreground(hwnd: int, tries: int = 5) -> bool:
    """可靠地把窗口抢到前台（Windows 默认禁止后台进程抢前台，单次调用会随机失败）。"""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.8)

    for _ in range(max(1, tries)):
        if win32gui.GetForegroundWindow() == hwnd:
            break
        try:  # ALT 解锁前台
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, win32con.KEYEVENTF_KEYUP, 0)
        except Exception:
            pass

        fg = win32gui.GetForegroundWindow()
        ft = cur = None
        attached = False
        try:
            ft, _ = win32process.GetWindowThreadProcessId(fg)
            cur = win32api.GetCurrentThreadId()
            if ft and ft != cur:
                attached = bool(user32.AttachThreadInput(cur, ft, True))
        except Exception:
            pass
        try:
            user32.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
        try:
            win32gui.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        finally:
            if attached and ft and cur:
                try:
                    user32.AttachThreadInput(cur, ft, False)
                except Exception:
                    pass
        time.sleep(0.7)
        if win32gui.GetForegroundWindow() == hwnd:
            break
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            time.sleep(0.3)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        time.sleep(0.8)

    if win32gui.IsIconic(hwnd):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.8)
        except Exception:
            pass
    return win32gui.GetForegroundWindow() == hwnd


def force_foreground_hard(hwnd: int, tries: int = 8) -> bool:
    """**最强**的抢前台：AttachThreadInput(自己+目标+当前前台) + SetForegroundWindow
    + BringWindowToTop + SetFocus，失败再靠最小化/还原抖动夺回。

    为什么单独再写一个（2026-09-16 实测）：`force_foreground()` 在这台机器上会返回 True
    但实际前台仍是浏览器（Edge），于是后续所有点击都打到浏览器上——表现为"怎么点都不中"，
    排查了很久。凡是要点击的场合，都用这个并在点前校验 `GetForegroundWindow()`。
    """
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.5)
    for attempt in range(max(1, tries)):
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        fg = win32gui.GetForegroundWindow()
        tid_self = win32api.GetCurrentThreadId()
        tids = {win32process.GetWindowThreadProcessId(hwnd)[0],
                win32process.GetWindowThreadProcessId(fg)[0],
                tid_self}
        attached = []
        for tid in tids:
            if tid and tid != tid_self:
                try:
                    if user32.AttachThreadInput(tid_self, tid, True):
                        attached.append(tid)
                except Exception:
                    pass
        try:
            win32gui.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
        except Exception:
            pass
        finally:
            for tid in attached:
                try:
                    user32.AttachThreadInput(tid_self, tid, False)
                except Exception:
                    pass
        time.sleep(0.45)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        if attempt >= 3:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                time.sleep(0.25)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            except Exception:
                pass
            time.sleep(0.6)
    return win32gui.GetForegroundWindow() == hwnd


def maximize(hwnd: int) -> tuple[int, int, int, int]:
    """最大化：剪映编辑界面的面板密度随窗口尺寸变化，固定成最大化才好定位坐标。"""
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    time.sleep(1.2)
    return window_rect(hwnd)


def ensure_running(timeout: float = 90.0) -> int:
    """确保剪映在跑，返回主窗口句柄。不在跑就启动（用用户真实 profile，草稿/字体都在里面）。"""
    hwnd = find_window()
    if hwnd:
        return hwnd
    exe = EXE if EXE.is_file() else EXE_LAUNCHER
    if not exe.is_file():
        raise FileNotFoundError(f"找不到剪映可执行文件：{EXE}")
    logger.info("启动剪映：%s", exe)
    subprocess.Popen([str(exe)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        hwnd = find_window()
        if hwnd and _rect_is_sane(window_rect(hwnd)):
            time.sleep(4.0)  # 让首页加载完
            return hwnd
    raise RuntimeError(f"剪映在 {timeout:.0f} 秒内没有出现可用窗口")


# ---------------------------------------------------------------------------
# 截图 / 差异
# ---------------------------------------------------------------------------
def capture(hwnd: int) -> Image.Image:
    """PrintWindow 抓窗口自身内容：不受遮挡影响，且不抢前台。

    注意每次都要重新取 rect —— 最大化/切页面后窗口尺寸会变。
    """
    import win32ui

    ensure_window(hwnd)
    x1, y1, x2, y2 = window_rect(hwnd)
    w, h = x2 - x1, y2 - y1
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    try:
        ok = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
        if not ok:
            user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        info = bmp.GetInfo()
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                               bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
        return img.copy()
    finally:
        try:
            win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def cap_window(hwnd: int) -> Image.Image:
    """抓**任意**窗口（含 Qt 弹出菜单、文件对话框这类小窗口），不做尺寸合理性检查。"""
    import win32ui

    x1, y1, x2, y2 = win32gui.GetWindowRect(hwnd)
    w, h = max(1, x2 - x1), max(1, y2 - y1)
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    try:
        if not user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2):
            user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        info = bmp.GetInfo()
        return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1).copy()
    finally:
        try:
            win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def grab_screen(rect: tuple[int, int, int, int] | None = None, hwnd: int | None = None) -> Image.Image:
    """抓**屏幕实际像素**（用于 PrintWindow 抓不到黑图的 Qt 弹出层/对话框）。

    前提：窗口得在屏幕最上层（调用方负责先置前），否则抓到的是压在它上面的窗口。
    """
    from PIL import ImageGrab

    if hwnd is not None:
        rect = win32gui.GetWindowRect(hwnd)
    return ImageGrab.grab(bbox=rect, all_screens=True)


def diff_score(a: Image.Image, b: Image.Image) -> float:
    """两张图平均像素差（0~255）。用于判断画面是否还在变。"""
    if a.size != b.size:
        return 255.0
    d = ImageChops.difference(a.convert("L"), b.convert("L"))
    hist = d.histogram()
    total = max(1, sum(hist))
    return sum(i * n for i, n in enumerate(hist)) / total


def wait_still(hwnd: int, *, quiet: float = 3.0, timeout: float = 300.0,
               threshold: float = 1.2, poll: float = 1.0,
               on_tick=None) -> tuple[bool, Image.Image]:
    """等画面稳定：连续 `quiet` 秒的变化都小于阈值即视为完成。

    用于「识别歌词」「导出」这类没有进度接口的耗时步骤。
    返回 (是否稳定, 最后一张截图)。
    """
    deadline = time.time() + timeout
    prev = capture(hwnd)
    stable_since = time.time()
    while time.time() < deadline:
        time.sleep(poll)
        now = capture(hwnd)
        score = diff_score(prev, now)
        if on_tick:
            on_tick(score, time.time())
        if score <= threshold:
            if time.time() - stable_since >= quiet:
                return True, now
        else:
            stable_since = time.time()
        prev = now
    return False, prev


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------
def click_px(hwnd: int, x: int, y: int, *, double: bool = False, settle: float = 0.6) -> tuple[int, int]:
    """点窗口内像素坐标（相对窗口左上角）。会先把窗口置前。"""
    if not force_foreground(hwnd):
        logger.warning("置前失败，仍尝试点击")
    x1, y1, _, _ = ensure_window(hwnd)
    sx, sy = x1 + int(x), y1 + int(y)
    user32.SetCursorPos(sx, sy)
    time.sleep(0.15)
    for _ in range(2 if double else 1):
        user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        if double:
            time.sleep(0.08)
    time.sleep(settle)
    return sx, sy


def click_ratio(hwnd: int, fx: float, fy: float, **kw) -> tuple[int, int]:
    """点窗口内比例坐标（0~1），与分辨率/DPI 解耦。"""
    x1, y1, x2, y2 = ensure_window(hwnd)
    return click_px(hwnd, (x2 - x1) * fx, (y2 - y1) * fy, **kw)


def drag_px(hwnd: int, x1: int, y1: int, x2: int, y2: int, *, steps: int = 24, settle: float = 0.6):
    """从一点拖到另一点（拖拽素材入轨、拖动播放头）。"""
    if not force_foreground(hwnd):
        logger.warning("置前失败，仍尝试拖拽")
    wx, wy, _, _ = ensure_window(hwnd)
    sx1, sy1 = wx + int(x1), wy + int(y1)
    sx2, sy2 = wx + int(x2), wy + int(y2)
    user32.SetCursorPos(sx1, sy1)
    time.sleep(0.2)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.25)
    for i in range(1, steps + 1):
        user32.SetCursorPos(int(sx1 + (sx2 - sx1) * i / steps), int(sy1 + (sy2 - sy1) * i / steps))
        time.sleep(0.02)
    time.sleep(0.25)
    user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(settle)


_VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "space": 0x20, "delete": 0x2E, "del": 0x2E, "back": 0x08,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "f2": 0x71, "f5": 0x74,
}


def _vk(name: str) -> int:
    n = name.strip().lower()
    if n in _VK:
        return _VK[n]
    if len(n) == 1 and (n.isalpha() or n.isdigit()):
        return ord(n.upper())
    raise ValueError(f"不认识的按键：{name}")


def post_hotkey(hwnd: int, keys: str, *, settle: float = 0.8) -> None:
    """**不移动鼠标、不抢前台**地把组合键投递给剪映窗口（PostMessage）。

    用途：用户正在用电脑时也要能操作剪映。剪映有原生快捷键（导入素材 = Ctrl+I），
    比点按钮更稳、更无干扰。
    """
    parts = [_vk(p) for p in keys.replace(" ", "").split("+") if p]
    for vk in parts:
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0x00000001)
        time.sleep(0.03)
    for vk in reversed(parts):
        win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0xC0000001)
        time.sleep(0.03)
    time.sleep(settle)


def post_click(hwnd: int, x: int, y: int, *, double: bool = False, button: str = "left",
               settle: float = 0.6, move_first: bool = True) -> None:
    """**不移动鼠标**地点窗口内某点：PostMessage 投递鼠标消息（Qt 一般认）。

    x,y 与其它函数一致（相对**窗口**左上角），内部换算成客户区坐标。
    `button="right"` 投递右键（用于在剪映里调出素材右键菜单）。
    `move_first=False`：不先投递 WM_MOUSEMOVE —— 用于「悬停浮出的按钮」：
    先用 post_hover 停在按钮上等它激活，再只发按下/抬起，否则按钮还没激活，
    按下会落到下面的缩略图上（实测会变成"预览"而不是"添加到轨道"）。
    """
    wx, wy, _, _ = win32gui.GetWindowRect(hwnd)
    cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
    px, py = int(x) - (cx - wx), int(y) - (cy - wy)
    lp = win32api.MAKELONG(px, py)
    down, up, wparam = ((win32con.WM_RBUTTONDOWN, win32con.WM_RBUTTONUP, win32con.MK_RBUTTON)
                        if button == "right" else
                        (win32con.WM_LBUTTONDOWN, win32con.WM_LBUTTONUP, win32con.MK_LBUTTON))
    for _ in range(2 if double else 1):
        if move_first:
            win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)
            time.sleep(0.03)
        win32gui.PostMessage(hwnd, down, wparam, lp)
        time.sleep(0.05)
        win32gui.PostMessage(hwnd, up, 0, lp)
        time.sleep(0.08)
    time.sleep(settle)


def post_drag(hwnd: int, x1: int, y1: int, x2: int, y2: int, *, steps: int = 24,
              settle: float = 1.0) -> None:
    """**不移动真实鼠标**地把素材从 A 拖到 B（PostMessage 按住左键 + 连续移动）。

    剪映把素材加进轨道有两条路：悬停浮出的「+」按钮，或直接拖到轨道。拖拽更通用。
    """
    wx, wy, _, _ = win32gui.GetWindowRect(hwnd)
    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    dx, dy = ox - wx, oy - wy

    def lp(x: float, y: float) -> int:
        return win32api.MAKELONG(int(x) - dx, int(y) - dy)

    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp(x1, y1))
    time.sleep(0.15)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp(x1, y1))
    time.sleep(0.30)
    for i in range(1, steps + 1):
        x = x1 + (x2 - x1) * i / steps
        y = y1 + (y2 - y1) * i / steps
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, lp(x, y))
        time.sleep(0.03)
    time.sleep(0.30)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp(x2, y2))
    time.sleep(settle)


def post_hover(hwnd: int, x: int, y: int, *, settle: float = 0.8) -> None:
    """**不移动真实光标**地让窗口进入悬停状态（PostMessage WM_MOUSEMOVE）。

    剪映的素材缩略图要靠悬停才浮出「+ 添加到轨道」按钮，这个函数用来触发它。
    """
    wx, wy, _, _ = win32gui.GetWindowRect(hwnd)
    cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
    lp = win32api.MAKELONG(int(x) - (cx - wx), int(y) - (cy - wy))
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)
    time.sleep(0.35)
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)
    time.sleep(settle)


def set_clipboard_text(text: str) -> None:
    """写 Unicode 文本到剪贴板（中文/长路径必须走粘贴，键盘直打会丢字）。"""
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def paste_text(hwnd: int, text: str, *, settle: float = 0.8) -> None:
    """置前 → 粘贴文本（Ctrl+V）。"""
    force_foreground(hwnd)
    set_clipboard_text(text)
    time.sleep(0.2)
    press_keys(hwnd, "^v", settle=settle)


def press_keys(hwnd: int, keys: str, *, settle: float = 0.5) -> None:
    """发送按键（pywinauto 语法，如 `^a`、`{ENTER}`、`{ESC}`）。"""
    force_foreground(hwnd)
    from pywinauto.keyboard import send_keys

    send_keys(keys, pause=0.05)
    time.sleep(settle)


def type_number(hwnd: int, value: str, *, settle: float = 0.6) -> None:
    """在一个已聚焦的数字输入框里替换成 value（全选后输入）。"""
    press_keys(hwnd, "^a", settle=0.2)
    press_keys(hwnd, "{DELETE}", settle=0.2)
    press_keys(hwnd, value, settle=settle)


# ---------------------------------------------------------------------------
# CLI（逐步驱动）
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="剪映驱动 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("find")
    sub.add_parser("maximize")
    w = sub.add_parser("windows")
    w.add_argument("--save", help="把每个窗口截图存到该前缀_<序号>.png")
    c = sub.add_parser("capture")
    c.add_argument("--out", default=r"D:\project\H3-MotionStudio\.tmp_jy_shot.png")
    s = sub.add_parser("shot", help="抓屏幕实际像素（能抓到 Qt 弹出层；先置前目标窗口）")
    s.add_argument("--hwnd", type=int, required=True)
    s.add_argument("--out", default=r"D:\project\H3-MotionStudio\.tmp_jy_popup.png")
    c = sub.add_parser("click")
    c.add_argument("--px", nargs=2, type=int)
    c.add_argument("--ratio", nargs=2, type=float)
    c.add_argument("--double", action="store_true")
    c.add_argument("--right", action="store_true", help="投递右键")
    c.add_argument("--nomove", action="store_true", help="PostMessage 投递点击，不移动鼠标")
    h = sub.add_parser("hotkey", help="不移动鼠标地投递组合键，如 ctrl+i")
    h.add_argument("keys")
    h.add_argument("--nomove", action="store_true", default=True)
    hv = sub.add_parser("hover", help="不移动真实光标地触发悬停")
    hv.add_argument("--px", nargs=2, type=int, required=True)
    c = sub.add_parser("drag")
    c.add_argument("--from", dest="src", nargs=2, type=int, required=True)
    c.add_argument("--to", dest="dst", nargs=2, type=int, required=True)
    c = sub.add_parser("paste")
    c.add_argument("--text", required=True)
    c = sub.add_parser("key")
    c.add_argument("--keys", required=True)
    c = sub.add_parser("wait-still")
    c.add_argument("--quiet", type=float, default=3.0)
    c.add_argument("--timeout", type=float, default=300.0)

    a = p.parse_args(argv)
    hwnd = find_window()
    if not hwnd:
        print("剪映没在运行（先跑 ensure_running 或手工打开）")
        return 1

    if a.cmd == "find":
        rect = window_rect(hwnd)
        print(f"hwnd={hwnd} class={win32gui.GetClassName(hwnd)} title={win32gui.GetWindowText(hwnd)!r}")
        print(f"rect={rect} size={rect[2]-rect[0]}x{rect[3]-rect[1]}")
    elif a.cmd == "windows":
        rows = list_windows()
        for i, r in enumerate(rows):
            print(f"[{i}] hwnd={r['hwnd']} class={r['class']:<28} popup={int(r['popup'])} "
                  f"size={r['size'][0]}x{r['size'][1]} @{r['rect'][:2]} title={r['title']!r}")
            if a.save:
                p = Path(f"{a.save}_{i}.png")
                try:
                    cap_window(r["hwnd"]).save(p)
                    print(f"      -> {p}")
                except Exception as e:
                    print(f"      截图失败 {type(e).__name__}: {e}")
    elif a.cmd == "maximize":
        print("maximized rect:", maximize(hwnd))
    elif a.cmd == "capture":
        img = capture(hwnd)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        img.save(a.out)
        print(f"saved {a.out} {img.size}")
    elif a.cmd == "shot":
        force_foreground(a.hwnd)
        time.sleep(0.6)
        img = grab_screen(hwnd=a.hwnd)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        img.save(a.out)
        print(f"saved {a.out} {img.size}（屏幕抓取 hwnd={a.hwnd}）")
    elif a.cmd == "click":
        if a.nomove:
            if not a.px:
                print("--nomove 目前只支持 --px")
                return 2
            post_click(hwnd, *a.px, double=a.double)
            print(f"post_click(不移动鼠标) px={a.px} double={a.double}")
        elif a.px:
            print("clicked px:", click_px(hwnd, *a.px, double=a.double))
        elif a.ratio:
            print("clicked ratio:", click_ratio(hwnd, *a.ratio, double=a.double))
        else:
            print("需要 --px 或 --ratio")
            return 2
    elif a.cmd == "hotkey":
        post_hotkey(hwnd, a.keys)
        print(f"post_hotkey(不移动鼠标) {a.keys}")
    elif a.cmd == "hover":
        post_hover(hwnd, *a.px)
        print(f"post_hover(不移动光标) px={a.px}")
    elif a.cmd == "drag":
        drag_px(hwnd, *a.src, *a.dst)
        print("dragged")
    elif a.cmd == "paste":
        paste_text(hwnd, a.text)
        print("pasted")
    elif a.cmd == "key":
        press_keys(hwnd, a.keys)
        print("keys sent")
    elif a.cmd == "wait-still":
        t0 = time.time()
        def tick(score, _now):
            print(f"  Δ={score:6.2f}  t={time.time()-t0:6.1f}s", flush=True)
        ok, img = wait_still(hwnd, quiet=a.quiet, timeout=a.timeout, on_tick=tick)
        out = r"D:\project\H3-MotionStudio\.tmp_jy_shot.png"
        img.save(out)
        print(f"stable={ok} 用时 {time.time()-t0:.1f}s -> {out}")
        return 0 if ok else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(__import__("sys").argv[1:]))
