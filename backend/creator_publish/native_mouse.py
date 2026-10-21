"""通过Windows SendInput发送连续移动、按钮和滚轮事件，不直接设置光标位置。"""

import ctypes
from ctypes import wintypes
import math


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class InputData(ctypes.Union):
    _fields_ = [("mouse", MouseInput)]


class Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("data", InputData)]


def pointer_path(start, target):
    """每步最多12像素；长距离增加步数，避免固定步数造成大跨度跳动。"""
    distance = math.hypot(target[0] - start[0], target[1] - start[1])
    steps = math.ceil(distance / 12)
    return [
        (round(start[0] + (target[0] - start[0]) * step / steps),
         round(start[1] + (target[1] - start[1]) * step / steps))
        for step in range(1, steps + 1)
    ]


def mouse_event(flags, position=None, wheel=0):
    """只发送系统鼠标事件；绝对坐标支持多显示器及负坐标。"""
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    user.SendInput.restype = wintypes.UINT
    dx = dy = 0
    if position is not None:
        left, top = user.GetSystemMetrics(76), user.GetSystemMetrics(77)
        width, height = user.GetSystemMetrics(78), user.GetSystemMetrics(79)
        if width <= 1 or height <= 1:
            raise RuntimeError("无法读取虚拟桌面范围。")
        x, y = position
        if not left <= x < left + width or not top <= y < top + height:
            raise ValueError("鼠标目标位于显示器范围外。")
        dx = round((x - left) * 65535 / (width - 1))
        dy = round((y - top) * 65535 / (height - 1))
        flags |= 0x8000 | 0x4000  # ABSOLUTE和VIRTUALDESK，避免主屏坐标映射错误。
    event = Input(type=0, data=InputData(mouse=MouseInput(
        dx=dx, dy=dy, mouseData=wheel & 0xFFFFFFFF, dwFlags=flags,
    )))
    if user.SendInput(1, ctypes.byref(event), ctypes.sizeof(Input)) != 1:
        raise ctypes.WinError(ctypes.get_last_error())
