"""本机键鼠操作的自检与最小示例（Windows）。

用途：验证/演示 DSH 侧能不能替你操作这台电脑的鼠标与键盘。
不参与 H3 业务链路，只是一个可手动运行的探针 + 现成工具。

运行（务必用项目 venv，里面的 pywinauto 已装好）：

    .venv\\Scripts\\python.exe scripts\\pc_input_demo.py --info
    .venv\\Scripts\\python.exe scripts\\pc_input_demo.py --list
    .venv\\Scripts\\python.exe scripts\\pc_input_demo.py --move 600 400
    .venv\\Scripts\\python.exe scripts\\pc_input_demo.py --countdown 3 --type "hello"
    .venv\\Scripts\\python.exe scripts\\pc_input_demo.py --countdown 3 --hotkey "^s"
    .venv\\Scripts\\python.exe scripts\\pc_input_demo.py --click 800 500
    .venv\\Scripts\\python.exe scripts\\pc_input_demo.py --self-test

通道说明（项目里已有实测结论）：
  * send_keys / click（SetCursorPos + mouse_event）是**真实输入**，能驱动剪映、QML、
    tkinter 等一切窗口，代价是**会占用你的光标与前台焦点**。
  * 投递窗口消息（WM_LBUTTONDOWN 等，不碰光标）只对经典 Win32 控件有效；
    tkinter / 剪映首页 QML 这类不认（项目里剪辑自动化因此只在编辑器内用 post_click）。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time

try:
    import win32gui
    import win32process
except Exception:  # pragma: no cover - 缺依赖时给出人话
    print("缺少 pywin32，请先: .venv\\Scripts\\python.exe -m pip install pywin32")
    raise SystemExit(2)

try:
    from pywinauto import Desktop, mouse
    from pywinauto.keyboard import send_keys
except Exception:  # pragma: no cover
    Desktop = None
    mouse = None
    send_keys = None

user32 = ctypes.windll.user32


def screen_size() -> tuple[int, int]:
    """物理分辨率（本进程按 DPI 感知读取，避免被缩放骗）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
    except Exception:
        pass
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def cursor_pos() -> tuple[int, int]:
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def foreground_title() -> str:
    hwnd = user32.GetForegroundWindow()
    return win32gui.GetWindowText(hwnd) or f"<无标题 hwnd={hwnd}>"


def require_pywinauto() -> None:
    if send_keys is None:
        print("缺少 pywinauto，请先: .venv\\Scripts\\python.exe -m pip install pywinauto")
        raise SystemExit(2)


def cmd_info() -> int:
    w, h = screen_size()
    x, y = cursor_pos()
    print(f"屏幕物理分辨率 : {w} x {h}")
    print(f"当前光标位置   : {x}, {y}")
    print(f"当前前台窗口   : {foreground_title()}")
    if Desktop is None:
        print("pywinauto       : 未安装（只能读状态，不能注入输入）")
        return 1
    wins = [w_ for w_ in Desktop(backend="win32").windows() if w_.is_visible()]
    print(f"可见顶层窗口   : {len(wins)} 个（用 --list 看明细）")
    return 0


def iter_windows():
    for w in Desktop(backend="win32").windows():
        try:
            if not w.is_visible():
                continue
            hwnd = w.handle
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            yield hwnd, pid, win32gui.GetWindowText(hwnd), win32gui.GetClassName(hwnd)
        except Exception:
            continue


def cmd_list(keyword: str | None) -> int:
    require_pywinauto()
    rows = list(iter_windows())
    if keyword:
        rows = [r for r in rows if keyword.lower() in r[2].lower()]
    print(f"{'hwnd':>10}  {'pid':>7}  {'class':<24}  title")
    for hwnd, pid, title, cls in sorted(rows, key=lambda r: r[1]):
        print(f"{hwnd:>10}  {pid:>7}  {cls[:24]:<24}  {title}")
    print(f"共 {len(rows)} 个窗口")
    return 0


def cmd_move(x: int, y: int) -> int:
    require_pywinauto()
    print(f"移动前: {cursor_pos()}  前台: {foreground_title()}")
    mouse.move(coords=(x, y))
    time.sleep(0.2)
    print(f"移动后: {cursor_pos()}（目标 {x},{y}）")
    return 0


def cmd_click(x: int, y: int, double: bool) -> int:
    require_pywinauto()
    before = cursor_pos()
    mouse.click(button="left", coords=(x, y), double=double)
    time.sleep(0.2)
    print(f"已在 ({x}, {y}) 点击{'双击' if double else ''}；光标 {before} -> {cursor_pos()}")
    return 0


def cmd_type(text: str, countdown: int) -> int:
    require_pywinauto()
    if countdown:
        for left in range(countdown, 0, -1):
            print(f"{left} 秒后开始输入，请把目标窗口切到前台……", flush=True)
            time.sleep(1)
    print(f"前台窗口: {foreground_title()}")
    send_keys(text, pause=0.03)
    print(f"已输入 {len(text)} 个字符")
    return 0


def cmd_hotkey(combo: str, countdown: int) -> int:
    require_pywinauto()
    if countdown:
        for left in range(countdown, 0, -1):
            print(f"{left} 秒后发送 {combo}……", flush=True)
            time.sleep(1)
    print(f"前台窗口: {foreground_title()}")
    send_keys(combo, pause=0.03)
    print(f"已发送 {combo}")
    return 0


def cmd_self_test() -> int:
    """在自建窗口里验证两条通道，不碰用户正在用的窗口。"""
    require_pywinauto()
    import tkinter as tk

    hits: list[tuple[int, int]] = []
    root = tk.Tk()
    root.title("H3 Input Self-Test")
    root.geometry("460x300+160+160")
    label = tk.Label(root, text="waiting", font=("Segoe UI", 12))
    label.pack(expand=True)

    def on_click(event):
        hits.append((event.x, event.y))
        label.config(text=f"clicked {event.x},{event.y}")
        root.update()

    root.bind("<Button-1>", on_click)
    root.update()
    time.sleep(1.0)

    hwnd = win32gui.FindWindow(None, "H3 Input Self-Test")
    rect = win32gui.GetWindowRect(hwnd)
    print(f"测试窗口 hwnd={hwnd} rect={rect}")

    # 通道 1：真实硬件级输入（SetCursorPos + mouse_event / 键盘事件）
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    mouse.click(button="left", coords=(rect[0] + 120, rect[1] + 150))
    time.sleep(0.6)
    root.update()
    print(f"① 真实点击 -> {hits}")

    # 通道 2：投递窗口消息（不碰光标）；tkinter 不认，仅作对照
    WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
    lparam = ((150 & 0xFFFF) << 16) | (120 & 0xFFFF)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lparam)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
    time.sleep(0.6)
    root.update()
    print(f"② 消息点击 -> {hits}（tkinter 不响应，属预期）")
    root.destroy()

    ok = len(hits) >= 1
    print("结论：真实键鼠输入通道可用" if ok else "结论：输入通道不可用")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机键鼠操作自检 / 最小示例")
    parser.add_argument("--info", action="store_true", help="打印屏幕、光标、前台窗口")
    parser.add_argument("--list", nargs="?", const="", metavar="KEYWORD", help="列出可见窗口（可按标题过滤）")
    parser.add_argument("--move", nargs=2, type=int, metavar=("X", "Y"), help="移动光标")
    parser.add_argument("--click", nargs=2, type=int, metavar=("X", "Y"), help="在屏幕坐标单击")
    parser.add_argument("--double", action="store_true", help="配合 --click 使用双击")
    parser.add_argument("--type", metavar="TEXT", help="向前台窗口输入文本")
    parser.add_argument("--hotkey", metavar="COMBO", help="向前台窗口发送组合键，如 ^s / %{F4}")
    parser.add_argument("--countdown", type=int, default=0, help="输入前等待秒数，便于切换窗口")
    parser.add_argument("--self-test", action="store_true", help="在自建窗口里验证输入通道")
    args = parser.parse_args(argv)

    if args.self_test:
        return cmd_self_test()
    if args.info:
        return cmd_info()
    if args.list is not None:
        return cmd_list(args.list or None)
    if args.move:
        return cmd_move(*args.move)
    if args.click:
        return cmd_click(*args.click, args.double)
    if args.type is not None:
        return cmd_type(args.type, args.countdown)
    if args.hotkey is not None:
        return cmd_hotkey(args.hotkey, args.countdown)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
