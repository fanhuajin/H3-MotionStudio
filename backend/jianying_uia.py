"""backend.jianying_uia — 用 UIA（FullDescription）驱动剪映，**不移动鼠标、不抢前台**。

## 为什么之前"读不到元素"

剪映是 Qt6 自绘 UI：
  · `Name` / `Value` / `LegacyIAccessible` 全是空的 → pywinauto 按名字找不到控件；
  · 但每个控件都带 **UIA FullDescription（属性 id 30159）**，剪映用它标好了语义，例如：
        HomePageStartProjectDesp              首页「开始创作」
        HomePageDraftTitle:9月16日 (4)         某条草稿（带名字）
        MainWindowTitleBarExportBtn           编辑页右上「导出」
        ExportPath                            导出对话框的路径框
  · 点击用 `Click(simulateMove=False)`：**不产生真实鼠标事件**，不需要窗口在前台。

参考实现：`pyJianYingDraft` 的 `jianying_controller.py`（其注释写明「仅支持剪映6及以下版本」，
本机为 5.9.0.11632，在支持范围内）。

## 前提

剪映**必须以 `QT_ACCESSIBILITY=1` 启动**，否则 Qt 不暴露控件树（实测 descendants 从 159 变 0）。
`ensure_app_accessibility()` 负责带该环境变量拉起剪映。

## 用法（CLI）

    python -m backend.jianying_uia map                 # 打印元素地图（FullDescription 清单）
    python -m backend.jianying_uia map --filter 导出     # 只看含某关键词的
    python -m backend.jianying_uia click --fd HomePageStartProjectDesp
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import jianying_desktop as jy  # noqa: E402

try:
    import uiautomation as auto
except ImportError as exc:  # pragma: no cover
    raise SystemExit("需要 pip install uiautomation") from exc

FD = 30159  # UIA_FullDescriptionPropertyId

auto.SetGlobalSearchTimeout(5)


@contextmanager
def uia_in_thread():
    """在**工作线程**里安全使用 uiautomation。

    实测坑：把 uiautomation 放进 `asyncio.to_thread` 会直接抛
    `[WinError -2147221008] 尚未调用 CoInitialize / Can not load UIAutomationCore.dll`
    —— uiautomation 要求每个线程自己初始化 COM，必须用官方的
    `UIAutomationInitializerInThread`（它的 `__init__` 里就调了
    `InitializeUIAutomationInCurrentThread()`）。
    """
    initializer = auto.UIAutomationInitializerInThread()
    try:
        yield initializer
    finally:
        initializer.Uninitialize()

JY_EXE = r"D:\soft\剪映以激活版本免安装\剪映以激活版本免安装\剪映解锁版\JianyingPro\解锁版.exe"


# ---------------------------------------------------------------------------
# 启动 / 取窗口
# ---------------------------------------------------------------------------
def ensure_app_accessibility(timeout: float = 120.0, *, restart: bool = False) -> int:
    """确保剪映在跑，且**带 QT_ACCESSIBILITY=1**（没有就重启它），返回窗口句柄。

    `restart=True`：先杀掉再启动 —— 剪映启动必定停在首页，这是"回到首页"最稳的办法
    （编辑器里没有可靠的返回首页控件，而自动草稿不需要保留现场）。
    """
    def tree_size(hwnd: int) -> int:
        try:
            w = auto.ControlFromHandle(hwnd)
            return len(w.GetChildren())
        except Exception:
            return 0

    hwnd = jy.find_window()
    if hwnd and not restart and tree_size(hwnd) > 0:
        return hwnd

    hwnd = jy.find_window()
    if hwnd and restart:
        print("重启剪映（保证回到首页）")
    if hwnd:
        print("剪映在跑但需要重启或控件树为空 → 带 QT_ACCESSIBILITY=1 重启")
        subprocess.run(["taskkill", "/F", "/IM", "JianyingPro.exe"], capture_output=True)
        time.sleep(3)

    env = dict(os.environ)
    env["QT_ACCESSIBILITY"] = "1"
    subprocess.Popen([JY_EXE], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        hwnd = jy.find_window()
        if hwnd and tree_size(hwnd) > 0:
            time.sleep(4)
            return hwnd
    raise RuntimeError("剪映未能在带 QT_ACCESSIBILITY=1 的情况下就绪")


def app_window():
    """当前剪映主窗口控件（首页或编辑页都行）。"""
    hwnd = jy.find_window()
    if not hwnd:
        raise RuntimeError("剪映没在运行")
    return auto.ControlFromHandle(hwnd)


# ---------------------------------------------------------------------------
# 查找 / 操作
# ---------------------------------------------------------------------------
def walk(ctrl=None, max_depth: int = 12):
    """遍历控件树，产出 (depth, control, full_description, name, class_name)。"""
    ctrl = ctrl or app_window()
    stack = [(ctrl, 0)]
    while stack:
        node, d = stack.pop()
        if d > max_depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            children = []
        for ch in children:
            try:
                fd = ch.GetPropertyValue(FD) or ""
                yield d, ch, fd, (ch.Name or ""), ch.ClassName
            except Exception:
                fd = ""
            stack.append((ch, d + 1))


def find(fd_substr: str, *, exact: bool = False, timeout: float = 10.0, root=None):
    """按 FullDescription 找控件（exact=False 时子串匹配）。"""
    deadline = time.time() + timeout
    while True:
        for _d, ctrl, fd, _n, _c in walk(root):
            if not fd:
                continue
            if (fd == fd_substr) if exact else (fd_substr in fd):
                return ctrl
        if time.time() >= deadline:
            return None
        time.sleep(1.0)


def find_all(fd_substr: str, *, exact: bool = False):
    return [(fd, ctrl) for _d, ctrl, fd, _n, _c in walk() if fd and
            ((fd == fd_substr) if exact else (fd_substr in fd))]


def click(ctrl) -> None:
    """**不移动真实鼠标**地点击控件（UIA Click(simulateMove=False)）。"""
    ctrl.Click(simulateMove=False)


def click_fd(fd_substr: str, *, exact: bool = False, timeout: float = 10.0) -> bool:
    ctrl = find(fd_substr, exact=exact, timeout=timeout)
    if not ctrl:
        return False
    click(ctrl)
    return True


def get_text(ctrl) -> str:
    for getter in ("GetValuePattern", "GetLegacyIAccessiblePattern"):
        try:
            pat = getattr(ctrl, getter)()
            if getter == "GetValuePattern":
                return pat.Value or ""
            return pat.Name or ""
        except Exception:
            continue
    return ""


def set_text(ctrl, text: str) -> bool:
    try:
        ctrl.GetValuePattern().SetValue(text)
        return True
    except Exception:
        pass
    try:
        ctrl.GetLegacyIAccessiblePattern().SetValue(text)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="剪映 UIA 驱动（不移动鼠标）")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("map", help="打印元素地图")
    m.add_argument("--filter", default="", help="只看 FullDescription 含该关键词的")
    m.add_argument("--max", type=int, default=200)

    c = sub.add_parser("click", help="按 FullDescription 点击")
    c.add_argument("--fd", required=True)
    c.add_argument("--exact", action="store_true")

    a = sub.add_parser("ensure", help="确保剪映带辅助功能启动")

    args = p.parse_args(argv)
    if args.cmd == "ensure":
        print("hwnd =", ensure_app_accessibility())
        return 0

    if args.cmd == "map":
        rows = [(d, fd, cls) for d, _c, fd, _n, cls in walk() if fd]
        if args.filter:
            rows = [r for r in rows if args.filter in r[1]]
        print(f"带 FullDescription 的控件 {len(rows)} 个：")
        for d, fd, cls in rows[: args.max]:
            print(f"  {'  ' * d}{fd[:60]:<62} cls={cls[:24]}")
        return 0

    if args.cmd == "click":
        ok = click_fd(args.fd, exact=args.exact)
        print("点击成功" if ok else "没找到控件")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
