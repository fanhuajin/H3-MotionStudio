"""独立 STA 进程显示原生作品文件夹选择框，结果通过 stdout 返回。"""
import argparse
import ctypes
from ctypes import wintypes
import json
import sys

from native_folder_picker import choose_folders


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        user = ctypes.WinDLL("user32")
        user.GetForegroundWindow.restype = wintypes.HWND
        selected = choose_folders(user.GetForegroundWindow(), args.directory)
        print(json.dumps({"folders": selected}, ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
