"""Windows 原生多选文件夹对话框（IFileOpenDialog）。"""
import ctypes
from ctypes import wintypes


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def choose_folders(owner, initial_directory):
    ole = ctypes.OleDLL("ole32")
    shell = ctypes.OleDLL("shell32")
    ole.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    shell.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                                ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    def guid(value):
        result = GUID()
        ole.CLSIDFromString(value, ctypes.byref(result))
        return result
    def call(pointer, index, result_type, argument_types, *arguments):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *argument_types)(table[index])
        return method(pointer, *arguments)
    def release(pointer):
        if pointer:
            call(pointer, 2, wintypes.ULONG, [])
    def checked(result):
        if result < 0:
            raise OSError(f"文件夹选择失败（0x{result & 0xffffffff:08X}）")
    # Tk may already have initialized COM on this UI thread.
    initialized = ctypes.WinDLL("ole32").CoInitializeEx(None, 2)
    dialog = ctypes.c_void_p()
    array = ctypes.c_void_p()
    try:
        clsid = guid("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")
        iid = guid("{D57C7288-D4AD-4768-BE02-9D969532D960}")
        ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(dialog))
        flags = wintypes.DWORD()
        checked(call(dialog, 10, ctypes.c_long, [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(flags)))
        checked(call(dialog, 9, ctypes.c_long, [wintypes.DWORD], flags.value | 0x20 | 0x40 | 0x200))
        checked(call(dialog, 17, ctypes.c_long, [wintypes.LPCWSTR], "批量选择作品文件夹"))
        checked(call(dialog, 18, ctypes.c_long, [wintypes.LPCWSTR], "加入选中作品"))
        item_iid = guid("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")
        initial = ctypes.c_void_p()
        try:
            shell.SHCreateItemFromParsingName(str(initial_directory), None, ctypes.byref(item_iid), ctypes.byref(initial))
            checked(call(dialog, 12, ctypes.c_long, [ctypes.c_void_p], initial))
        except OSError:
            pass
        finally:
            release(initial)
        result = call(dialog, 3, ctypes.c_long, [wintypes.HWND], owner)
        if result & 0xffffffff == 0x800704C7:
            return []
        checked(result)
        checked(call(dialog, 27, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(array)))
        count = wintypes.DWORD()
        checked(call(array, 7, ctypes.c_long, [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(count)))
        paths = []
        for index in range(count.value):
            item = ctypes.c_void_p()
            name = ctypes.c_void_p()
            try:
                checked(call(array, 8, ctypes.c_long, [wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)], index, ctypes.byref(item)))
                checked(call(item, 5, ctypes.c_long, [wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)], 0x80058000, ctypes.byref(name)))
                paths.append(ctypes.wstring_at(name))
            finally:
                if name:
                    ole.CoTaskMemFree(name)
                release(item)
        return paths
    finally:
        release(array)
        release(dialog)
        if initialized >= 0:
            ole.CoUninitialize()
