"""backend.chatgpt_desktop — 驱动 ChatGPT 桌面端生成图片（纯 UI 自动化，实测跑通）。

为什么不用 CDP / UIA（都已实测排除）：
  - CDP：应用在**真实登录 profile** 下不开放远程调试端口（临时空 profile 才开）。
  - UIA：Chromium 只暴露窗口按钮，**看不到网页内容**。

已实测跑通的完整链路：
  1. 置前窗口（AttachThreadInput + SwitchToThisWindow + ALT 解锁 + 最小化恢复，多重兜底）
  2. 点「聊天」标签 → 点「新对话」
  3. 剪贴板粘贴 图一 → 图二（新对话后输入框自带焦点，不需要点输入框）
  4. 剪贴板粘贴 提示词
  5. 点右下角发送按钮（比 Enter 可靠）
  6. 等生成完成（连续截图稳定即视为完成）
  7. 左键点生成图 → 打开查看器 → 点右上角下载 → 弹「另存为」
  8. 地址栏填目标目录 → 文件名 → 保存

关键前提：**进程必须 DPI 感知**，否则 125% 缩放下坐标整体错位（踩过）。
坐标全部用窗口比例表示，集中在 _LAYOUT。
"""
from __future__ import annotations

import ctypes
import io
import logging
import os
import time
from pathlib import Path

import win32api
import win32clipboard
import win32con
import win32gui
import win32process

# **必须在任何窗口/截图操作之前**把本进程设为 DPI 感知（125% 缩放下否则坐标全错）
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from PIL import Image, ImageChops
from pywinauto import Desktop, mouse
from pywinauto.keyboard import send_keys

user32 = ctypes.windll.user32
logger = logging.getLogger("batch.chatgpt_desktop")

# 窗口内比例坐标（相对窗口矩形），DPI 感知下 1600x1035 实测
_LAYOUT = {
    "chat_tab": (0.560, 0.073),       # 顶部「聊天」标签
    "new_chat": (0.081, 0.125),       # 侧栏「新对话」
    "send_button": (0.844, 0.865),    # 输入框右下角蓝色发送按钮（贴完长提示词后）
    "image_center": (0.510, 0.500),   # 生成图大致中心（用于左键点开查看器）
    "viewer_download": (0.906, 0.066),  # 查看器右上角下载按钮
}

# 生成图默认落盘目录（用户 2026-09-15：「建议保存到 E 盘」）
DEFAULT_IMAGE_DIR = Path(r"E:\AI_Exports\H3-MotionStudio\ChatGPT生成图")


def image_dir() -> Path:
    """生成图保存目录，可用 H3_CHATGPT_IMAGE_DIR 覆盖。"""
    raw = os.getenv("H3_CHATGPT_IMAGE_DIR")
    p = Path(raw) if raw else DEFAULT_IMAGE_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------- 窗口 ----------------
def find_main_window(*, restore: bool = True) -> int | None:
    """找 ChatGPT 主窗口。

    优先「可见且未最小化」；若只有最小化的，就恢复它。
    注意：最小化窗口 `IsWindowVisible` 仍为 True、但 rect 是 (-25600,...) 且只有 159x27，
    拿去算比例坐标会全错（踩过这个坑）。
    """
    rows: list[tuple[int, bool, bool, tuple[int, int, int, int]]] = []

    def cb(hwnd, _):
        if win32gui.GetWindowText(hwnd) == "ChatGPT":
            rows.append((hwnd, bool(win32gui.IsWindowVisible(hwnd)),
                         bool(win32gui.IsIconic(hwnd)), win32gui.GetWindowRect(hwnd)))

    win32gui.EnumWindows(cb, None)
    if not rows:
        return None

    def area(r):
        x1, y1, x2, y2 = r[3]
        return max(0, x2 - x1) * max(0, y2 - y1)

    normal = [r for r in rows if r[1] and not r[2]]
    if normal:
        return max(normal, key=area)[0]
    if restore:
        iconic = [r for r in rows if r[1] and r[2]] or rows
        hwnd = max(iconic, key=area)[0]
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(1.0)
        except Exception:
            pass
        return hwnd
    return None


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    return win32gui.GetWindowRect(hwnd)


def _rect_is_sane(rect: tuple[int, int, int, int]) -> bool:
    x1, y1, x2, y2 = rect
    return (x2 - x1) >= 800 and (y2 - y1) >= 600 and x1 > -10000 and y1 > -10000


def ensure_window(hwnd: int) -> tuple[int, int, int, int]:
    """确保窗口已恢复且尺寸合理，返回可用的 rect（否则抛错）。"""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(1.0)
    rect = window_rect(hwnd)
    if not _rect_is_sane(rect):
        # 再试一次最小化/恢复，仍不行就报错，避免用错坐标乱点
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            time.sleep(0.4)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(1.0)
        except Exception:
            pass
        rect = window_rect(hwnd)
    if not _rect_is_sane(rect):
        raise RuntimeError(f"ChatGPT 窗口尺寸异常，已拒绝操作：rect={rect}")
    return rect


def force_foreground(hwnd: int, tries: int = 5) -> bool:
    """**可靠地**把窗口抢到前台（Windows 默认禁止后台进程抢前台，单一调用会随机失败）。"""
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
        try:  # 最小化/恢复兜底
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            time.sleep(0.3)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        time.sleep(0.8)

    # **结尾必须确保窗口是恢复状态**，否则会留下一个最小化窗口，后续坐标全废
    if win32gui.IsIconic(hwnd):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.8)
        except Exception:
            pass
    return win32gui.GetForegroundWindow() == hwnd


def click_frac(hwnd: int, key: str) -> tuple[int, int]:
    x1, y1, x2, y2 = window_rect(hwnd)
    fx, fy = _LAYOUT[key]
    x, y = x1 + int((x2 - x1) * fx), y1 + int((y2 - y1) * fy)
    mouse.click(button="left", coords=(x, y))
    return x, y


def capture_window(hwnd: int) -> Image.Image:
    """PrintWindow 抓**窗口自身内容**（不受遮挡影响；ImageGrab 被盖住时会抓到别的窗口）。"""
    import win32ui

    x1, y1, x2, y2 = window_rect(hwnd)
    w, h = x2 - x1, y2 - y1
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
    info = bmp.GetInfo()
    img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                           bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    return img


# ---------------- 剪贴板 ----------------
def set_clipboard_image(path: str | Path) -> None:
    img = Image.open(path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "BMP")
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, buf.getvalue()[14:])
    finally:
        win32clipboard.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def send_message(hwnd: int) -> None:
    """发送：优先点右下角发送按钮（比 Enter 可靠），再补一个 Enter 兜底。"""
    click_frac(hwnd, "send_button")
    time.sleep(0.8)
    send_keys("{ENTER}")
    time.sleep(1.0)


# ---------------- 等生成完成 ----------------
def _mean_diff(a: Image.Image, b: Image.Image) -> float:
    """两张图的平均像素差（0~255）。"""
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a.convert("L"), b.convert("L"))
    hist = diff.histogram()
    total = sum(hist)
    if not total:
        return 0.0
    return sum(i * n for i, n in enumerate(hist)) / total


def wait_for_response(hwnd: int, *, timeout: float = 420.0,
                      settle: float = 8.0, threshold: float = 1.2,
                      poll: float = 3.0, min_wait: float = 12.0,
                      start_change: float = 0.8) -> bool:
    """等生成完成。

    不能只看「画面稳定」——刚发送时画面可能本来就没变，会被误判成已完成
    （实测踩过：46 秒就返回 None）。所以：
      1) 先等画面**出现过明显变化**（回复开始/生成中）；
      2) 之后再等画面稳定 settle 秒才算完成；
      3) 并有 min_wait 最短等待兜底。
    """
    start = time.time()
    last = None
    stable_since = None
    saw_change = False
    while time.time() - start < timeout:
        try:
            cur = capture_window(hwnd)
        except Exception:
            time.sleep(poll)
            continue
        if last is not None:
            d = _mean_diff(cur, last)
            if d >= start_change:
                saw_change = True
                stable_since = None
            elif saw_change and d < threshold:
                if stable_since is None:
                    stable_since = time.time()
                elif (time.time() - stable_since >= settle
                      and time.time() - start >= min_wait):
                    return True
        last = cur
        time.sleep(poll)
    return False


# ---------------- 保存生成图 ----------------
def _find_save_dialog(timeout: float = 15.0):
    """等「另存为」对话框出现（class #32770）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for w in Desktop(backend="uia").windows():
                try:
                    if w.class_name() == "#32770" and w.window_text() in ("另存为", "Save As"):
                        return w
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.5)
    return None


def _save_dialog_to(dlg, target: Path, timeout: float = 20.0) -> bool:
    """在「另存为」里保存到 target。

    实测要点（踩过的坑）：
      - 文件名框（auto_id=1001）直接 **set_edit_text 完整路径**即可，Windows 会自动切目录；
        不用去折腾顶部的地址栏，那条路更脆。
      - **对话框必须在前台**！它常常被别的窗口压在后面，这时用坐标点击「保存」会打到
        上层窗口上（表现为「点了但没保存」）。所以点之前先 force_foreground。
      - 点击用控件的**真实矩形中心**，不要猜比例坐标。
    """
    try:
        target = Path(target)
        edit = dlg.child_window(auto_id="1001", control_type="Edit")
        edit.set_edit_text(str(target))
        time.sleep(0.6)

        # 关键：把对话框抢到前台，否则物理点击会打到上层窗口
        force_foreground(dlg.handle)
        time.sleep(0.4)

        btn = dlg.child_window(auto_id="1", control_type="Button")
        r = btn.rectangle()
        mouse.click(button="left", coords=((r.left + r.right) // 2, (r.top + r.bottom) // 2))
        time.sleep(1.5)

        # 覆盖确认框（文件已存在时会弹）
        try:
            for w in Desktop(backend="uia").windows():
                try:
                    if w.class_name() == "#32770" and w.window_text() in ("确认另存为", "Confirm Save As"):
                        force_foreground(w.handle)
                        time.sleep(0.3)
                        send_keys("{ENTER}")
                        break
                except Exception:
                    continue
        except Exception:
            pass
    except Exception:
        logger.exception("填写另存为对话框失败")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if target.is_file() and target.stat().st_size > 0:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def _close_stale_save_dialogs() -> None:
    """关掉上一次残留的「另存为」对话框，避免和新的一次混淆。"""
    try:
        for w in Desktop(backend="uia").windows():
            try:
                if w.class_name() == "#32770" and w.window_text() in ("另存为", "Save As"):
                    force_foreground(w.handle)
                    time.sleep(0.3)
                    send_keys("{ESC}")
                    time.sleep(0.6)
            except Exception:
                continue
    except Exception:
        pass


def save_generated_image(hwnd: int, target: str | Path) -> bool:
    """左键点开生成图 → 查看器右上角下载 → 另存为到 target。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass

    _close_stale_save_dialogs()

    # 左键点开生成图（打开查看器）
    click_frac(hwnd, "image_center")
    time.sleep(2.5)

    # 查看器右上角下载
    click_frac(hwnd, "viewer_download")
    time.sleep(2.0)

    dlg = _find_save_dialog(timeout=15)
    if dlg is None:
        logger.warning("没有等到「另存为」对话框")
        return False
    ok = _save_dialog_to(dlg, target)
    if not ok:
        logger.warning("另存为未能落盘：%s", target)
    return ok


# ---------------- 主流程 ----------------
def generate_via_desktop(
    images: list[str | Path],
    prompt: str,
    *,
    save_to: str | Path | None = None,
    timeout: float = 420.0,
    new_chat: bool = True,
) -> Path | None:
    """完整流程：粘贴素材 → 发送 → 等生成 → 保存。成功返回落盘路径。

    save_to 省略时自动存到 `image_dir()`（默认 E:\\AI_Exports\\H3-MotionStudio\\ChatGPT生成图）。
    ⚠️ 调用方必须保证单链路（同一时刻只有一处调用），ChatGPT 桌面端只有一个登录态。
    """
    hwnd = find_main_window()
    if not hwnd:
        raise RuntimeError("找不到 ChatGPT 桌面端窗口")
    ensure_window(hwnd)  # 尺寸不合理直接报错，绝不用错坐标乱点
    if not force_foreground(hwnd):
        raise RuntimeError("无法把 ChatGPT 窗口置前（被前台锁拦住）")
    ensure_window(hwnd)

    if save_to is None:
        save_to = image_dir() / f"chatgpt_{time.strftime('%Y%m%d-%H%M%S')}.png"
    save_to = Path(save_to)

    click_frac(hwnd, "chat_tab")
    time.sleep(1.4)
    if new_chat:
        click_frac(hwnd, "new_chat")
        time.sleep(1.8)

    for img in images:
        set_clipboard_image(img)
        time.sleep(0.4)
        send_keys("^v")
        time.sleep(1.8)

    set_clipboard_text(prompt)
    time.sleep(0.4)
    send_keys("^v")
    time.sleep(1.2)
    send_message(hwnd)

    if not wait_for_response(hwnd, timeout=timeout):
        return None
    time.sleep(2.0)

    if save_generated_image(hwnd, save_to):
        return save_to
    return None
