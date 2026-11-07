"""Windows 输入注入：相对移动、鼠标按键、滚轮、侧键、键盘组合键。

只使用标准库 ctypes + 系统自带 user32.dll，不依赖任何第三方包。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

_PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)
ULONG_PTR = ctypes.c_ulonglong if _PTR_SIZE == 8 else ctypes.c_ulong

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000

XBUTTON1 = 0x0001  # 后退 / 上一步
XBUTTON2 = 0x0002  # 前进 / 下一步

KEYEVENTF_KEYUP = 0x0002

VK_CONTROL = 0x11
VK_MENU = 0x12     # Alt
VK_SHIFT = 0x10
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_Z = 0x5A
VK_Y = 0x59

WHEEL_DELTA = 120


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int


def _send(*items: INPUT) -> int:
    arr = (INPUT * len(items))(*items)
    return user32.SendInput(len(arr), arr, ctypes.sizeof(INPUT))


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF,
                      dwFlags=flags, time=0, dwExtraInfo=0),
    )


def _key(vk: int, up: bool = False) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0,
                      time=0, dwExtraInfo=0),
    )


def move(dx: int, dy: int) -> None:
    """相对移动光标。dx/dy 为整数像素。"""
    if dx or dy:
        _send(_mouse(MOUSEEVENTF_MOVE, int(dx), int(dy)))


_BTN_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
    "x1": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    "x2": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}


def button_down(name: str) -> None:
    flags = _BTN_FLAGS.get(name)
    if flags:
        _send(_mouse(flags[0], data=flags[2]))


def button_up(name: str) -> None:
    flags = _BTN_FLAGS.get(name)
    if flags:
        _send(_mouse(flags[1], data=flags[2]))


def wheel(notches: float, horizontal: bool = False) -> None:
    """正值向上/向右滚，负值向下/向左滚。"""
    delta = int(round(notches * WHEEL_DELTA))
    if delta:
        _send(_mouse(MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL,
                     data=delta & 0xFFFFFFFF))


def key_combo(vks: list[int]) -> None:
    """按下并释放一个组合键，例如 [VK_CONTROL, VK_Z]。"""
    items = [_key(vk) for vk in vks]
    items += [_key(vk, up=True) for vk in reversed(vks)]
    _send(*items)


def screen_size() -> tuple[int, int]:
    return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


def prev(mode: str) -> None:
    """上一步。"""
    if mode == "undo_redo":
        key_combo([VK_CONTROL, VK_Z])
    elif mode == "alt_arrows":
        key_combo([VK_MENU, VK_LEFT])
    else:
        _send(_mouse(MOUSEEVENTF_XDOWN, data=XBUTTON1),
              _mouse(MOUSEEVENTF_XUP, data=XBUTTON1))


def next_action(mode: str) -> None:
    """下一步。"""
    if mode == "undo_redo":
        key_combo([VK_CONTROL, VK_Y])
    elif mode == "alt_arrows":
        key_combo([VK_MENU, VK_RIGHT])
    else:
        _send(_mouse(MOUSEEVENTF_XDOWN, data=XBUTTON2),
              _mouse(MOUSEEVENTF_XUP, data=XBUTTON2))
