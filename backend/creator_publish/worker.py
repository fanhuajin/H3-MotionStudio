"""仅由发布服务启动；导入引擎不会产生桌面操作，run 才会运行队列。"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import time
import psutil

from multi_publish import MultiQueue, SubmissionTracking, PLATFORMS, pending_platforms
from bilibili_auto import (
    BilibiliDraft, prepare_files, cover_from_folder, category_from_folder, select_hour, CHINA_TIME,
)
from douyin_auto import DouyinDraft, prepare_douyin, douyin_covers, select_douyin_hour
from xiaohongshu_auto import (
    XiaohongshuDraft, prepare_xiaohongshu, xiaohongshu_cover, select_xiaohongshu_hour,
)
from desktop_session import desktop_session
from platform_pages import PlatformPages


def adapters():
    return {
        "bilibili": (type("TrackedBili", (SubmissionTracking, BilibiliDraft), {"tab_hint": "创作中心"}),
                     prepare_files, cover_from_folder, category_from_folder, select_hour),
        "douyin": (type("TrackedDouyin", (SubmissionTracking, DouyinDraft), {"tab_hint": "抖音创作者中心"}),
                   prepare_douyin, douyin_covers, lambda f: None, select_douyin_hour),
        "xiaohongshu": (type("TrackedXhs", (SubmissionTracking, XiaohongshuDraft), {"tab_hint": "小红书创作服务平台"}),
                        prepare_xiaohongshu, xiaohongshu_cover, lambda f: None, select_xiaohongshu_hour),
    }


def atomic_write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.05)


class Notice:
    def __init__(self, queue):
        self.queue = queue

    def set(self, value):
        self.queue.state["notice"] = value
        self.queue.save_queue()


class Rows:
    def update_states(self, *args):
        pass


class WebQueue(MultiQueue):
    """保留原 run_platform 的检查点与提交保护，仅替换 Tk 窗口回调。"""
    def __init__(self, state_path):
        self.queue_file = state_path
        self.control_file = state_path.with_name("control.json")
        self.state = json.loads(state_path.read_text(encoding="utf-8"))
        self.items = {i["id"]: dict(i, folder=Path(i["folder"])) for i in self.state["items"]}
        self.adapters = adapters()
        self.timezone = CHINA_TIME
        self.tree = Rows()
        self.notice = Notice(self)
        self.paused = False
        self.state["phase"] = "running"

    def save_queue(self):
        self.state["items"] = [dict(i, folder=str(i["folder"])) for i in self.items.values()]
        atomic_write(self.queue_file, self.state)

    def command(self):
        try:
            return json.loads(self.control_file.read_text(encoding="utf-8")).get("command")
        except FileNotFoundError:
            return None

    def tick(self):
        # 后端进程退出后停止继续操作；提交意图已在鼠标按下之前持久化。
        if not self.owner_alive() or self.command() == "stop":
            raise RuntimeError("发布已停止，可核对记录后继续。")
        if self.command() == "pause":
            self.web_pause = True
            try:
                self.pause()
            finally:
                self.web_pause = False

    def owner_alive(self):
        # Windows venv python.exe 会经转发进程启动实际解释器，后端可能是祖父进程。
        # 仍要求后端位于存活的父进程链中，避免后端退出后继续操作桌面。
        try:
            return any(parent.pid == self.state["ownerPid"] for parent in psutil.Process().parents())
        except psutil.Error:
            return False

    def pause(self):
        started = time.monotonic()
        self.state["phase"] = "paused"
        self.notice.set("已暂停，点击继续恢复当前操作。")
        # Esc 也进入同一暂停状态；旧 resume 指令不能让新的 Esc 暂停立即结束。
        if not getattr(self, "web_pause", False) and self.command() == "resume":
            self.control_file.unlink(missing_ok=True)
        while self.command() != "resume":
            if self.command() == "stop" or not self.owner_alive():
                raise RuntimeError("发布已停止，可核对记录后继续。")
            time.sleep(.1)
        self.control_file.unlink(missing_ok=True)
        self.state["phase"] = "running"
        self.notice.set("正在继续发布。")
        # 从网页点击继续会切换前台，按原 Esc 恢复语义还原目标窗口。
        draft = getattr(self, "active_draft", None)
        if draft and draft.window:
            draft.window.SetFocus()
            time.sleep(.2)
        # 原 check() 会为 Esc 计算暂停时长；网页暂停由 tick() 发起，需要在这里补记。
        if getattr(self, "web_pause", False) and draft:
            draft.paused_seconds += time.monotonic() - started


@desktop_session()
def run_queue(state_path, platforms):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, "Local\\CreatorPublishHelperMulti")
    occupied = ctypes.get_last_error() == 183
    queue = WebQueue(state_path)
    queue.state["workerPid"] = os.getpid()
    queue.save_queue()
    try:
        if not mutex or occupied:
            raise RuntimeError("另一个三端发布窗口正在使用桌面，请先关闭它。")
        import windows_mcp.uia  # noqa: F401
        import keyboard_session
        # 初始化 draft 后由回调获取当前窗口，以支持网页暂停恢复。
        for platform, adapter in list(queue.adapters.items()):
            base, *rest = adapter
            def init(draft, base=base):
                base.__init__(draft)
                queue.active_draft = draft
            queue.adapters[platform] = (type(base.__name__ + "Web", (base,), {"__init__": init}), *rest)
        for item in queue.items.values():
            for p in platforms:
                if item["states"][p] == "待核对":
                    raise ValueError("请先核对所选平台的待核对记录。")
                if p not in pending_platforms(item):
                    continue
                _, prepare, cover, category, select = queue.adapters[p]
                prepare(item["folder"])
                cover(item["folder"])
                category(item["folder"])
                if item["scheduled"]:
                    select(item["date"], item["hour"])
        # 先通过原生标签页核对已有页面，只补开缺失项；全部就绪才进入任何上传/发布步骤。
        browser = BilibiliDraft()
        queue.active_draft = browser
        browser.on_tick = queue.tick
        browser.on_pause = queue.pause
        PlatformPages(browser, queue.notice.set).ensure()
        for p in platforms:
            for key, item in queue.items.items():
                if p in pending_platforms(item):
                    queue.tick()
                    queue.run_platform(key, item, p)
        queue.notice.set("所选平台全部处理完成，记录已保留。")
    except Exception as error:
        queue.notice.set(f"发布已停止：{error}")
    finally:
        if "keyboard_session" in locals():
            keyboard_session.stop()
        queue.state["phase"] = "idle"
        queue.save_queue()
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("--platform", choices=PLATFORMS)
    args = parser.parse_args()
    try:
        run_queue(args.state, (args.platform,) if args.platform else PLATFORMS)
    except Exception as error:
        state = json.loads(args.state.read_text(encoding="utf-8"))
        state.update(phase="idle", notice=f"发布无法启动：{error}")
        atomic_write(args.state, state)
