import ctypes
import os
from ctypes import wintypes


FLASHW_ALL = 0x00000003
FLASHW_TIMERNOFG = 0x0000000C
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _get_parent_pid_chain(kernel32: ctypes.WinDLL, pid: int) -> list[int]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return [pid]

    parents: dict[int, int] = {}
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    try:
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    pids = [pid]
    seen = {pid}
    current = pid
    while current in parents:
        parent = parents[current]
        if not parent or parent in seen:
            break
        pids.append(parent)
        seen.add(parent)
        current = parent
    return pids


def _find_visible_window_for_pids(user32: ctypes.WinDLL, pids: list[int]) -> int:
    windows_by_pid: dict[int, list[int]] = {}

    @ENUM_WINDOWS_PROC
    def enum_window(hwnd: int, lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        pid = int(window_pid.value)
        if pid in pids:
            windows_by_pid.setdefault(pid, []).append(hwnd)
        return True

    user32.EnumWindows(enum_window, 0)
    for pid in pids:
        windows = windows_by_pid.get(pid)
        if windows:
            return windows[-1]
    return 0


def request_window_attention_win32() -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    kernel32.GetConsoleWindow.argtypes = []
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [ENUM_WINDOWS_PROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
    user32.FlashWindowEx.restype = wintypes.BOOL

    pids = _get_parent_pid_chain(kernel32, os.getpid())
    hwnd = _find_visible_window_for_pids(user32, pids) or kernel32.GetConsoleWindow()
    if not hwnd:
        return

    info = FLASHWINFO(
        ctypes.sizeof(FLASHWINFO),
        hwnd,
        FLASHW_ALL | FLASHW_TIMERNOFG,
        5,
        0,
    )
    user32.FlashWindowEx(ctypes.byref(info))
