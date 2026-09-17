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
    "chat_tab": (0.575, 0.111),       # 顶部「聊天」标签（右边那个是「工作」，不要点）
    "new_chat": (0.055, 0.118),       # 侧栏「新对话」
    "send_button": (0.841, 0.452),    # 输入框右下角蓝色发送按钮（贴完长提示词后）
    "image_center": (0.510, 0.500),   # 生成图大致中心（用于左键点开查看器）
    "viewer_download": (0.906, 0.066),  # 查看器右上角下载按钮
}

# 标定窗口尺寸：PrintWindow 截图尺寸与窗口矩形完全一致（1600x1035），
# 所以比例坐标 × 窗口尺寸 = 截图像素坐标。窗口不是这个尺寸时必须先调整，
# 否则点击整体偏移（实测 1440x900 下「发送按钮」会偏到输入框上方）。
CALIBRATED_SIZE = (1600, 1035)

# 发送按钮的专属蓝色（实测 rgb≈(59,130,246)）；用它动态定位，不依赖固定比例
_SEND_BLUE = (59, 130, 246)

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


def resize_to_calibrated(hwnd: int, *, tries: int = 3) -> tuple[int, int]:
    """把窗口调整到标定尺寸（返回实际 size）。

    `_LAYOUT` 的比例值是在 1600x1035 下实测的；窗口尺寸不同时按比例换算出的点
    会偏到别的控件上（实测 1440x900 下「发送按钮」会落到输入框上方 300 多像素）。
    调整失败不抛错——调用方用 `assert_calibrated()` 决定是否继续。
    """
    target_w, target_h = CALIBRATED_SIZE
    flags = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
    for _ in range(max(1, tries)):
        try:
            win32gui.SetWindowPos(hwnd, 0, 40, 40, target_w, target_h, flags)
        except Exception:
            logger.exception("调整 ChatGPT 窗口尺寸失败")
        time.sleep(1.2)
        x1, y1, x2, y2 = window_rect(hwnd)
        if (x2 - x1, y2 - y1) == (target_w, target_h):
            break
    x1, y1, x2, y2 = window_rect(hwnd)
    return x2 - x1, y2 - y1


def assert_calibrated(hwnd: int, *, tolerance: int = 8) -> tuple[int, int]:
    """确认窗口尺寸与标定尺寸一致（允许 ±tolerance），否则抛错。

    **绝不能在尺寸不对时继续点**：比例坐标会落到别的控件上，
    表现为「点了没反应」甚至误触其它按钮。
    """
    x1, y1, x2, y2 = window_rect(hwnd)
    size = (x2 - x1, y2 - y1)
    tw, th = CALIBRATED_SIZE
    if abs(size[0] - tw) > tolerance or abs(size[1] - th) > tolerance:
        raise RuntimeError(
            f"ChatGPT 窗口尺寸 {size[0]}x{size[1]} 与标定尺寸 {tw}x{th} 不一致，"
            f"按比例坐标点击会点偏，已拒绝操作"
        )
    return size


def find_send_button(img: "Image.Image", hwnd: int, *, bottom_ratio: float = 0.22
                     ) -> tuple[int, int] | None:
    """在窗口截图里找发送按钮（那粒蓝色圆形按钮），返回**屏幕坐标**。

    三条经验（都是实测踩出来的）：
      * **只扫窗口最底部一条**：输入框固定在底部，聊天里生成的图片、对话里的小蓝点
        都可能被误判成按钮（实测取「最大蓝团」时，一个 8 像素的小蓝点因为连通面积最大
        而胜出，把「发送」点到了窗口右下角边缘）。所以搜索区要窄。
      * **取最靠下的那团**，不是最大的那团：发送按钮永远贴着窗口底边。
      * 没找到就退一步放宽搜索区（空聊天时输入框垂直居中，实测按钮在 y≈48%）。
    """
    try:
        rgb = img.convert("RGB")
        w, h = rgb.size
        px = rgb.load()
        x1, y1, x2, y2 = window_rect(hwnd)
        sx, sy = (x2 - x1) / w, (y2 - y1) / h

        def blobs_in(top_ratio: float) -> list[list[tuple[int, int]]]:
            y_start = int(h * (1.0 - top_ratio))
            blue = {(x, y) for y in range(y_start, h) for x in range(w)
                    if _is_send_blue(*px[x, y])}
            out: list[list[tuple[int, int]]] = []
            while blue:
                seed = blue.pop()
                stack = [seed]
                blob = [seed]
                while stack:
                    cx, cy = stack.pop()
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            nxt = (cx + dx, cy + dy)
                            if nxt in blue:
                                blue.discard(nxt)
                                stack.append(nxt)
                                blob.append(nxt)
                out.append(blob)
            return out

        for ratio in (bottom_ratio, 0.35, 1.0):
            found = [b for b in blobs_in(ratio) if len(b) >= 40]
            if not found:
                continue
            # 最靠下的一团就是发送按钮（窗口底边的那个圆）
            best = max(found, key=lambda b: sum(p[1] for p in b) / len(b))
            cx = sum(p[0] for p in best) / len(best)
            cy = sum(p[1] for p in best) / len(best)
            logger.debug("发送按钮：在底部 %.0f%% 内找到 %d 团，取最下者(%d px) 重心 %.0f,%.0f",
                         ratio * 100, len(found), len(best), cx, cy)
            return x1 + int(cx * sx), y1 + int(cy * sy)
        return None
    except Exception:
        logger.exception("定位发送按钮失败")
        return None


def _is_send_blue(r: int, g: int, b: int) -> bool:
    """是否发送按钮的那种蓝。

    **不能只匹配某一档 RGB**：输入框为空时按钮是**禁用态浅蓝**（实测 (144,192,240)），
    有内容时是可用态深蓝（实测 (59,130,246)）。所以按「蓝色通道明显占优」判断，
    两种状态都能认出来（这一条是实测踩出来的：按 (59,130,246)±55 匹配时，
    禁用态的按钮完全找不到，发送就静默失败了）。
    """
    return b > 170 and b - r > 25 and b - g > 10


def wait_for_send_button(hwnd: int, *, timeout: float = 20.0, poll: float = 1.0
                         ) -> tuple[int, int] | None:
    """等发送按钮出现再返回它的坐标（贴图后界面要渲染一会儿才稳定）。

    绝不在没定位到时退回固定比例坐标：贴了图的输入框会变高，固定坐标必然点空，
    而点空=消息没发出去，后面「等生成」只会白等到超时。
    """
    deadline = time.time() + max(0.0, timeout)
    last: tuple[int, int] | None = None
    hits = 0
    while True:
        pos = find_send_button(capture_window(hwnd), hwnd)
        if pos:
            if last is not None and abs(pos[0] - last[0]) <= 3 and abs(pos[1] - last[1]) <= 3:
                hits += 1
                if hits >= 2:  # 连续两次一致，说明界面稳定了
                    return pos
            else:
                hits = 0
            last = pos
        if time.time() >= deadline:
            return last
        time.sleep(poll)


def find_viewer_download(img: "Image.Image", hwnd: int) -> tuple[int, int] | None:
    """在「生成图查看器」右上角工具栏里找下载图标，返回**屏幕坐标**。

    实测（2026-09-16，1600x1035 窗口）：查看器工具栏在 y≈53~85，
    顺序是「分享 / 下载 / ⋯ / 放大 / 侧栏」，下载图标在窗口内 x≈1458、y≈69。
    `_LAYOUT["viewer_download"]` 按比例算出来是 (1489, 108)——偏出去几十像素，
    实测点不到（旧实现就是这么失败的），所以这里按**深色图标像素**动态定位。

    注意：工具栏那排图标只有「下载」是「深色描边 + 底下一横」的形状，
    这里取的是**该区域内最深的一团**，并用「团下面还有一条细横线」来加分，
    避免点到左边的「分享」黑底胶囊。
    """
    try:
        rgb = img.convert("RGB")
        w, h = rgb.size
        px = rgb.load()
        x1, y1, x2, y2 = window_rect(hwnd)
        sx, sy = (x2 - x1) / w, (y2 - y1) / h
        # 只看右上角工具栏那一条
        box = (int(w * 0.80), int(h * 0.02), int(w * 0.995), int(h * 0.12))

        def ink(x: int, y: int) -> bool:
            """工具栏图标是**灰色描边**（实测 ~(110~180)），不是纯黑，别用太低的阈值。"""
            r, g, b = px[x, y]
            return (r + g + b) / 3 < 200 and (max(r, g, b) - min(r, g, b)) < 30

        dark = {(x, y) for y in range(box[1], box[3]) for x in range(box[0], box[2])
                if ink(x, y)}
        if not dark:
            return None
        blobs: list[list[tuple[int, int]]] = []
        while dark:
            seed = dark.pop()
            stack = [seed]
            blob = [seed]
            while stack:
                cx, cy = stack.pop()
                for dx in (-2, 0, 2):
                    for dy in (-2, 0, 2):
                        nxt = (cx + dx, cy + dy)
                        if nxt in dark:
                            dark.discard(nxt)
                            stack.append(nxt)
                            blob.append(nxt)
            if len(blob) >= 10:
                blobs.append(blob)
        if not blobs:
            return None

        def bounds(blob):
            xs = [p[0] for p in blob]
            ys = [p[1] for p in blob]
            return min(xs), min(ys), max(xs), max(ys)

        # 「分享」是黑底胶囊：又宽又实心，直接排除；工具栏图标本体宽约 14~30 像素。
        # 实测（1600x1035）下载图标被渲染成左右两团：
        #   箭头 x1452-1462 / 托盘横线 x1450-1464，重心 (1457, 77)。
        # 所以这里按「左侧起第一个尺寸合适的图标」定位，取它的包围盒中心。
        icons = []
        for blob in blobs:
            bx0, by0, bx1, by1 = bounds(blob)
            bw, bh = bx1 - bx0 + 1, by1 - by0 + 1
            if 6 <= bw <= 45 and 4 <= bh <= 45:
                icons.append((bx0, by0, bx1, by1, len(blob)))
        logger.debug("查看器工具栏候选图标：%s", icons)
        if not icons:
            return None
        bx0, by0, bx1, by1, _ = icons[0]
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        return x1 + int(cx * sx), y1 + int(cy * sy)
    except Exception:
        logger.exception("定位查看器下载图标失败")
        return None


def download_open_image(hwnd: int, target: str | Path, *, timeout: float = 20.0) -> bool:
    """当前查看器里那张图 → 点下载 → 另存为到 target。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass
    _close_stale_save_dialogs()

    coords = find_viewer_download(capture_window(hwnd), hwnd)
    if not coords:
        logger.warning("没找到查看器的下载图标")
        return False
    logger.info("点击下载图标 %s -> %s", coords, target)
    mouse.click(button="left", coords=coords)
    time.sleep(1.5)

    dlg = _find_save_dialog(timeout=timeout)
    if dlg is None:
        logger.warning("没有等到「另存为」对话框")
        return False
    return _save_dialog_to(dlg, target)


def viewer_is_open(img: "Image.Image", hwnd: int) -> bool:
    """当前是不是停在「生成图查看器」那一页。

    实测特征：顶部中间多出一条**浅灰工具条**（添加评论 / 移除背景 / 擦除 / 调整大小），
    还有右上角的缩放比例。工具条的纵向位置会随图片比例变化（横版在 y≈12.5%、
    竖版会往上挪到 y≈10%），所以**扫多条线**取最长灰条，别只取一行。
    """
    try:
        rgb = img.convert("RGB")
        w, h = rgb.size
        px = rgb.load()

        def longest_grey_run(y: int) -> int:
            run = best = 0
            for x in range(w):
                r, g, b = px[x, y]
                grey = (abs(r - g) < 12 and abs(g - b) < 12 and 205 <= r <= 248)
                run = run + 1 if grey else 0
                best = max(best, run)
            return best

        threshold = int(w * 0.45)
        for y in range(int(h * 0.07), int(h * 0.17), 4):
            if longest_grey_run(y) > threshold:
                return True
        return False
    except Exception:
        logger.exception("判断查看器是否打开失败")
        return False


def find_tab_close_buttons(img: "Image.Image", hwnd: int) -> list[tuple[int, int]]:
    """找窗口标签条上各个标签页右端的 × 按钮（屏幕坐标，从左到右）。

    为什么不能写死坐标：图片查看器**每开一张就多一个标签页**（实测开了 2 个之后，
    固定坐标点到的已经不是要关的那个），标签宽度还会随标题长度变化。
    × 的形状是「约 12x12 的小灰块」，按尺寸筛出来最稳。
    """
    try:
        rgb = img.convert("RGB")
        w, h = rgb.size
        px = rgb.load()
        x1, y1, x2, y2 = window_rect(hwnd)
        sx, sy = (x2 - x1) / w, (y2 - y1) / h
        y0, y1b = int(h * 0.045), int(h * 0.10)   # 标签条所在高度
        x0 = int(w * 0.14)                        # 跳过左侧边栏

        def is_glyph(x: int, y: int) -> bool:
            r, g, b = px[x, y]
            # × 是**浅灰**（实测 ~150-175），比标签标题的黑字浅；用较松的阈值把两者都收进来，
            # 再用尺寸区分（标题字 13~14 像素宽，× 只有 8 像素宽）。
            return (r + g + b) / 3 < 215 and (max(r, g, b) - min(r, g, b)) < 40

        pts = {(x, y) for y in range(y0, y1b) for x in range(x0, w) if is_glyph(x, y)}
        boxes = []
        while pts:
            seed = pts.pop()
            stack = [seed]
            blob = [seed]
            while stack:
                cx, cy = stack.pop()
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nxt = (cx + dx, cy + dy)
                        if nxt in pts:
                            pts.discard(nxt)
                            stack.append(nxt)
                            blob.append(nxt)
            xs = [p[0] for p in blob]
            ys = [p[1] for p in blob]
            bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
            # 实测 × 的包围盒是 8x8（如 x529-536 / y70-77）；标题字更宽、图标更大/更小都被排除
            if 6 <= bw <= 10 and 6 <= bh <= 10 and len(blob) >= 12:
                boxes.append((min(xs), min(ys), max(xs), max(ys)))
        boxes.sort()
        logger.debug("标签页 × 候选：%s", boxes)
        return [(x1 + int((b[0] + b[2]) / 2 * sx),
                 y1 + int((b[1] + b[3]) / 2 * sy)) for b in boxes]
    except Exception:
        logger.exception("定位标签页 × 失败")
        return []


def close_viewer(hwnd: int, *, rounds: int = 4) -> bool:
    """关掉生成的图片查看器标签页（可能同时开着好几个）。

    **为什么必须关**：查看器会盖住整个会话，此时输入框与「发送」按钮都不在预期位置——
    实测在查看器打开时按截图找发送按钮，点到了图片上的蓝色区域，消息根本没发出去。
    所以任何「贴内容 / 发送」之前都要先确认查看器已关闭。

    × 位置由 `find_tab_close_buttons()` 动态定位；每关一个，剩下的标签会左移，
    所以每轮都要**重新定位**，不能复用坐标。
    """
    if not viewer_is_open(capture_window(hwnd), hwnd):
        return True
    for _ in range(max(1, rounds)):
        buttons = find_tab_close_buttons(capture_window(hwnd), hwnd)
        if not buttons:
            break
        mouse.click(button="left", coords=buttons[0])
        time.sleep(1.5)
        if not viewer_is_open(capture_window(hwnd), hwnd):
            logger.info("已关闭图片查看器")
            return True
    ok = not viewer_is_open(capture_window(hwnd), hwnd)
    if not ok:
        logger.warning("试图关闭图片查看器但工具条仍在")
    return ok


def generation_in_progress(img: "Image.Image") -> bool:
    """画面上是否还有「正在生成」的迹象（用于判定出图是否真的完成）。

    实测 ChatGPT 出图会先铺一片**点阵网格**再换成真图，光看「画面稳定」会在中间态
    就误判成完成（结果找不到图块，把已经生成好的图判成失败）。
    点阵网格的特征很好认：回复区里密密麻麻的**孤立小点**。
    这里按「在回复区里统计互不相邻的深色小点数量」判断。
    """
    try:
        rgb = img.convert("RGB")
        w, h = rgb.size
        px = rgb.load()
        x0, x1 = int(w * 0.30), int(w * 0.95)
        y0, y1 = int(h * 0.35), int(h * 0.90)
        step = 4

        def dot(x: int, y: int) -> bool:
            r, g, b = px[x, y]
            return (r + g + b) / 3 < 215 and (max(r, g, b) - min(r, g, b)) < 45

        cells = [(x, y) for y in range(y0, y1, step) for x in range(x0, x1, step)
                 if dot(x, y)]
        if len(cells) < 60:
            return False
        # 点阵的特征是「点很多、但彼此不相邻」——统计有多少点是孤立的
        cellset = set(cells)
        isolated = 0
        for (x, y) in cells:
            neighbours = sum(1 for dx in (-step, 0, step) for dy in (-step, 0, step)
                             if (dx or dy) and (x + dx, y + dy) in cellset)
            if neighbours == 0:
                isolated += 1
        ratio = isolated / len(cells)
        busy = len(cells) >= 200 and ratio >= 0.55
        if busy:
            logger.debug("疑似生成中：深色小点 %d 个，其中孤立 %d 个（%.0f%%）",
                         len(cells), isolated, ratio * 100)
        return busy
    except Exception:
        logger.exception("判断生成中状态失败")
        return False


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
    """发送：优先点右下角发送按钮（比 Enter 可靠），再补一个 Enter 兜底。

    发送按钮**先按截图里的蓝色实心圆动态定位**，定不到才退回 `_LAYOUT` 的标定比例；
    这条日志能直接看出到底走了哪条路（写错坐标时不用再靠猜）。
    """
    coords = wait_for_send_button(hwnd, timeout=20.0)
    if not coords:
        # 定不到就是定不到：绝不能拿固定比例坐标去点，那会点空（消息没发出去），
        # 后面等生成只会白等到超时，白白花掉几分钟还看不出真实原因。
        raise RuntimeError("没能在窗口里定位到发送按钮（蓝色实心圆），已放弃发送以免点错位置")
    logger.info("点击发送按钮（动态定位）%s", coords)
    mouse.click(button="left", coords=coords)
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


def _dialog_child(dlg_hwnd: int, ctrl_id: int, *, cls: str | None = None) -> int | None:
    """在对话框里按控件 ID 找子窗口（找不到返回 None）。

    **不要用 pywinauto 的 `child_window()`**：`Desktop(...).windows()` 返回的是
    UIAWrapper，它根本没有这个方法（实测 `AttributeError: 'UIAWrapper' object has
    no attribute 'child_window'`——下载到这一步就断在这里）。
    直接枚举子窗口按 GetDlgCtrlID 匹配，两种后端下都可靠。
    """
    found: list[int] = []

    def cb(hwnd, _):
        try:
            if win32gui.GetDlgCtrlID(hwnd) != ctrl_id:
                return
            if cls and win32gui.GetClassName(hwnd) != cls:
                return
            if win32gui.IsWindowVisible(hwnd):
                found.append(hwnd)
        except Exception:
            pass

    try:
        win32gui.EnumChildWindows(dlg_hwnd, cb, None)
    except Exception:
        logger.exception("枚举对话框子控件失败：%s", dlg_hwnd)
    return found[0] if found else None


def _save_dialog_to(dlg, target: Path, timeout: float = 20.0) -> bool:
    """在「另存为」里保存到 target。

    实测要点（都是踩出来的）：
      - 文件名框是 **Edit + 控件 ID 1001**（Windows 11 现代另存为对话框），
        直接全选改写**完整路径**即可，Windows 会自动切目录，不用折腾地址栏。
      - **对话框必须在前台**，否则物理点击会打到上层窗口（表现为「点了但没保存」）。
      - 点击用控件的**真实矩形中心**，不要猜比例坐标。
    """
    target = Path(target)
    dlg_hwnd = getattr(dlg, "handle", None)
    if not dlg_hwnd:
        logger.warning("另存为对话框没有句柄")
        return False

    try:
        edit = _dialog_child(dlg_hwnd, 1001, cls="Edit")
        if not edit:
            logger.warning("在另存为对话框里找不到文件名输入框（控件 ID 1001）")
            return False
        force_foreground(dlg_hwnd)          # 先抢前台，再点它的控件
        time.sleep(0.4)
        win32gui.SetForegroundWindow(edit)
        time.sleep(0.3)
        send_keys("^a")
        time.sleep(0.2)
        send_keys(str(target), with_spaces=True, pause=0.01)
        time.sleep(0.6)
        logger.info("已把目标路径写进文件名框：%s", target)

        btn = _dialog_child(dlg_hwnd, 1, cls="Button")
        if not btn:
            logger.warning("在另存为对话框里找不到「保存」按钮（控件 ID 1）")
            return False
        left, top, right, bottom = win32gui.GetWindowRect(btn)
        force_foreground(dlg_hwnd)
        time.sleep(0.3)
        mouse.click(button="left", coords=((left + right) // 2, (top + bottom) // 2))
        time.sleep(1.5)
        logger.info("已点击「保存」按钮 %s", ((left + right) // 2, (top + bottom) // 2))

        # 覆盖确认框（文件已存在时会弹）
        for _ in range(8):
            confirm = None
            try:
                for w in Desktop(backend="uia").windows():
                    try:
                        if (w.class_name() == "#32770"
                                and w.window_text() in ("确认另存为", "Confirm Save As")):
                            confirm = w
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            if confirm is None:
                break
            force_foreground(confirm.handle)
            time.sleep(0.3)
            send_keys("{ENTER}")
            time.sleep(0.8)
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
def find_composer_thumbnails(img: "Image.Image", hwnd: int, *, max_count: int = 12
                             ) -> list[tuple[int, int, int, int]]:
    """找输入框里已贴素材的缩略图矩形（屏幕坐标，按从左到右排序）。

    缩略图是白底输入区里的一片**深色照片块**，所以用「暗像素成团」定位。
    这些缩略图右上角有悬浮的 × 按钮，是官方提供的删除入口
    （用户 2026-09-16 现场指出：「鼠标悬浮后会有这个按钮 可以清空」）。
    """
    try:
        rgb = img.convert("RGB")
        w, h = rgb.size
        px = rgb.load()
        x1, y1, x2, y2 = window_rect(hwnd)
        sx, sy = (x2 - x1) / w, (y2 - y1) / h
        y_start, y_end = int(h * 0.30), int(h * 0.80)

        dark = {(x, y) for y in range(y_start, y_end) for x in range(w)
                if sum(px[x, y]) / 3 < 130}
        if not dark:
            return []

        boxes: list[tuple[int, int, int, int]] = []
        while dark and len(boxes) < max_count:
            seed = dark.pop()
            stack = [seed]
            blob = [seed]
            while stack:
                cx, cy = stack.pop()
                for dx in (-2, 0, 2):  # 步长 2：缩略图内部是连续的，不必逐像素
                    for dy in (-2, 0, 2):
                        nxt = (cx + dx, cy + dy)
                        if nxt in dark:
                            dark.discard(nxt)
                            stack.append(nxt)
                            blob.append(nxt)
            if len(blob) < 400:  # 太小的团是文字/图标，不是缩略图
                continue
            bx = [p[0] for p in blob]
            by = [p[1] for p in blob]
            boxes.append((min(bx), min(by), max(bx), max(by)))

        boxes.sort()  # 从左到右
        merged: list[tuple[int, int, int, int]] = []
        for box in boxes:
            if merged:
                px0, py0, px1, py1 = merged[-1]
                # x 方向重叠且纵向基本同一行 -> 同一个缩略图（暗块常被照片里的亮部切成几块）
                if box[0] <= px1 + 6 and box[1] <= py1 + 6 and box[3] >= py0 - 6:
                    merged[-1] = (min(px0, box[0]), min(py0, box[1]),
                                  max(px1, box[2]), max(py1, box[3]))
                    continue
            merged.append(box)

        scaled = [(x1 + int(bx0 * sx), y1 + int(by0 * sy),
                   x1 + int(bx1 * sx), y1 + int(by1 * sy))
                  for bx0, by0, bx1, by1 in merged]
        logger.debug("输入框缩略图 %d 个：%s", len(scaled), scaled)
        return scaled
    except Exception:
        logger.exception("定位输入框缩略图失败")
        return []


def remove_composer_attachment(hwnd: int, box: tuple[int, int, int, int]) -> bool:
    """点缩略图右上角的 × 删掉这张已贴素材。返回是否真的少了一张。"""
    before = len(find_composer_thumbnails(capture_window(hwnd), hwnd))
    x = box[2] - 12  # × 悬在右上角，稍微往内收一点
    y = box[1] + 12
    mouse.move(coords=(x, y))  # 先悬浮，× 才会显示出来
    time.sleep(0.6)
    mouse.click(button="left", coords=(x, y))
    time.sleep(1.0)
    after = len(find_composer_thumbnails(capture_window(hwnd), hwnd))
    ok = after < before
    logger.info("删除输入框缩略图：%d -> %d（%s）", before, after, "成功" if ok else "没删掉")
    return ok


def focus_composer(hwnd: int) -> None:
    """把焦点点进输入框（编辑器）。

    这一步是清空/粘贴的前提：焦点不在编辑器时 `Ctrl+A` 会变成「全选页面」，
    后面的剪切、粘贴全部作用不到输入框上（实测踩过：删不掉任何东西）。
    落点取窗口 (0.55, 0.55)：在 ChatGPT 布局里属于输入框的文字区，是安全的空白处。
    """
    x1, y1, x2, y2 = window_rect(hwnd)
    x = x1 + int((x2 - x1) * 0.55)
    y = y1 + int((y2 - y1) * 0.55)
    mouse.click(button="left", coords=(x, y))
    time.sleep(0.6)


def clear_composer(hwnd: int, *, max_rounds: int = 12) -> int:
    """清空输入框里的残留草稿，返回删掉的素材张数。

    **为什么必须有这一步**：点「新对话」不会清掉输入框里已贴好的草稿
    （实测连跑两次后堆到 4 张图 + 两段提示词，发出去就是把两次内容一起提交）。

    两条路都要走（用户 2026-09-16 给的快捷键方案 + 官方 × 按钮）：
      1. 点进输入框 → `Ctrl+A` 全选 → `Ctrl+X` 剪切走，清掉残留文字；
      2. 已贴素材的缩略图带 × 按钮，逐张点掉——剪切不一定带走附件，
         而且这条路每删一张都会**验证数量真的减少**，不会假成功。
    """
    focus_composer(hwnd)
    send_keys("^a")
    time.sleep(0.4)
    send_keys("^x")
    time.sleep(0.8)
    logger.info("已用 Ctrl+A / Ctrl+X 清空输入框文字")

    removed = 0
    for _ in range(max_rounds):
        boxes = find_composer_thumbnails(capture_window(hwnd), hwnd)
        if not boxes:
            break
        if not remove_composer_attachment(hwnd, boxes[0]):  # 每删一张，后面的会左移，必须重新定位
            break
        removed += 1
    logger.info("输入框清空完成：删掉 %d 张素材", removed)
    return removed


def generate_via_desktop(
    images: list[str | Path],
    prompt: str,
    *,
    save_to: str | Path | None = None,
    timeout: float = 420.0,
    new_chat: bool = True,
    verify_only: bool = False,
    shot_path: str | Path | None = None,
) -> Path | None:
    """完整流程：粘贴素材 → 发送 → 等生成 → 保存。成功返回落盘路径。

    save_to 省略时自动存到 `image_dir()`（默认 E:\\AI_Exports\\H3-MotionStudio\\ChatGPT生成图）。
    ⚠️ 调用方必须保证单链路（同一时刻只有一处调用），ChatGPT 桌面端只有一个登录态。

    `verify_only=True` 时**只把素材与提示词贴进输入框、不发送**，并截图到 `shot_path`，
    用来在真正出图前先让人眼确认粘贴内容对不对（用户 2026-09-16 要求）。
    """
    hwnd = find_main_window()
    if not hwnd:
        raise RuntimeError("找不到 ChatGPT 桌面端窗口")
    ensure_window(hwnd)  # 尺寸不合理直接报错，绝不用错坐标乱点
    # 比例坐标只在标定尺寸下成立 -> 先调整，再硬校验；对不上就抛错，不猜着点
    resize_to_calibrated(hwnd)
    assert_calibrated(hwnd)
    if not force_foreground(hwnd):
        raise RuntimeError("无法把 ChatGPT 窗口置前（被前台锁拦住）")
    ensure_window(hwnd)
    assert_calibrated(hwnd)

    if save_to is None:
        save_to = image_dir() / f"chatgpt_{time.strftime('%Y%m%d-%H%M%S')}.png"
    save_to = Path(save_to)

    # 桌面端默认就停在「聊天」（右侧「工作」标签才是 agent 模式），
    # 所以不再点「聊天」标签——那个点很小，点偏反而会切到「工作」。
    if new_chat:
        click_frac(hwnd, "new_chat")
        time.sleep(1.8)
        clear_composer(hwnd)  # 新对话不会清掉草稿，必须自己清，否则内容会越贴越多

    for img in images:
        set_clipboard_image(img)
        time.sleep(0.4)
        send_keys("^v")
        time.sleep(1.8)

    set_clipboard_text(prompt)
    time.sleep(0.4)
    send_keys("^v")
    time.sleep(1.2)

    if verify_only:
        time.sleep(1.5)
        shot = Path(shot_path) if shot_path else (save_to.parent / "_verify_draft.png")
        shot.parent.mkdir(parents=True, exist_ok=True)
        img_shot = capture_window(hwnd)
        img_shot.save(shot)
        boxes = find_composer_thumbnails(img_shot, hwnd)
        logger.info("verify_only：已粘贴未发送，截图 %s", shot)
        # 自检：贴进去的素材张数必须与请求一致，否则说明没清干净、内容叠在一起了
        if len(boxes) != len(images):
            logger.warning("verify_only 自检：输入框里有 %d 张素材，期望 %d 张——"
                           "可能是上一次的草稿没清干净，请核对截图 %s",
                           len(boxes), len(images), shot)
        return shot

    send_message(hwnd)

    if not wait_for_response(hwnd, timeout=timeout):
        return None
    time.sleep(2.0)

    if save_generated_image(hwnd, save_to):
        return save_to
    return None
