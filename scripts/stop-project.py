"""延迟关闭验证过的项目进程树，使 HTTP 确认响应先返回；不触碰浏览器与外部服务。"""
import argparse
import os
from pathlib import Path
import sys
import time

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.project_shutdown import is_project_process


def stop_tree(pid, created, root):
    process = psutil.Process(pid)
    if process.create_time() != created or not is_project_process(process, root):
        raise RuntimeError("项目进程身份已变化，已取消关闭。")
    own_pid = os.getpid()
    targets = [process]
    try:
        # 先冻结当前树，避免服务在退出期间新启动生成或发布子进程。
        for target in targets:
            try:
                target.suspend()
                # 冻结父进程后再枚举子进程，防止快照后又出现未收尾的新任务。
                targets.extend(child for child in target.children() if child.pid != own_pid)
            except psutil.NoSuchProcess:
                pass
        for target in reversed(targets):
            try:
                target.kill()
            except psutil.NoSuchProcess:
                pass
    finally:
        # 如果权限等问题阻断关闭，不留下被挂起的存活进程。
        for target in targets:
            try:
                target.resume()
            except psutil.Error:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--created", type=float, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    time.sleep(2)
    stop_tree(args.pid, args.created, args.root)
