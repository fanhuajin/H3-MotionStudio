"""跨进程桌面占用闸门；不与视频/GPU 锁绑定。"""
from contextlib import contextmanager
import ctypes


@contextmanager
def desktop_session():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, "Local\\H3MotionStudioDesktopInput")
    occupied = ctypes.get_last_error() == 183
    try:
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if occupied:
            raise RuntimeError("三端发布或剪映字幕正在使用桌面，请等其结束后重试。")
        yield
    finally:
        if handle:
            kernel.CloseHandle(handle)
