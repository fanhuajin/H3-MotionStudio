"""automation.publish_watcher — 监听发布成品目录，检测「人物图.png + 发布文案.txt」齐了。

发布成品目录 {publish_root}/{编号}_{标题}_{作品号}/：
  - 人物图.png + 发布文案.txt 在审核点就会提前落盘（deliver_review_materials）
  - 最终成片.mp4 在出片完成后补上
监听「人物图+发布文案都出现」→ 触发封面生成（cover_flow）。
"""
from __future__ import annotations

import time
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG = True
except ImportError:
    WATCHDOG = False


def find_ready_folders(cfg: dict, publish_root: str | Path | None = None) -> list[Path]:
    """扫描发布目录，返回已同时具备 人物图 + 发布文案 的条目子目录。"""
    root = Path(publish_root or cfg["publish_root"])
    if not root.is_dir():
        return []
    ready: list[Path] = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        has_image = bool(list(folder.glob("人物图.*")))
        has_copy = (folder / "发布文案.txt").is_file()
        if has_image and has_copy:
            ready.append(folder)
    return ready


class _Handler:
    """同时兼容 watchdog 与轮询：on_ready(folder) 在「人物图+发布文案齐了」时触发。"""

    def __init__(self, cfg, on_ready):
        self.cfg = cfg
        self.on_ready = on_ready
        self._seen = set()

    def _check(self):
        for folder in find_ready_folders(self.cfg):
            key = str(folder)
            if key not in self._seen:
                self._seen.add(key)
                try:
                    self.on_ready(folder)
                except Exception as e:
                    print(f"[watcher] 处理 {folder} 出错：{e}")

    def on_any_event(self, event):
        self._check()


if WATCHDOG:
    class _WatchdogHandler(_Handler, FileSystemEventHandler):
        pass
else:
    class _WatchdogHandler:  # 占位
        pass


def watch(cfg: dict, on_ready, publish_root: str | Path | None = None, poll: float = 3.0):
    """阻塞式监听；每 poll 秒扫描一次（即使无 watchdog 也能工作）。"""
    root = Path(publish_root or cfg["publish_root"])
    root.mkdir(parents=True, exist_ok=True)
    if WATCHDOG:
        handler = _WatchdogHandler(cfg, on_ready)
        observer = Observer()
        observer.schedule(handler, str(root), recursive=True)
        observer.start()
        try:
            while True:
                handler._check()
                time.sleep(poll)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()
    else:
        handler = _Handler(cfg, on_ready)
        print("[watcher] 未装 watchdog，改用轮询模式")
        try:
            while True:
                handler._check()
                time.sleep(poll)
        except KeyboardInterrupt:
            pass
