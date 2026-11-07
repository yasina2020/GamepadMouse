"""XInput 兜底后端：即使 SDL2.dll 缺失或设备未被 SDL 识别，Xbox 手柄仍可用。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

BTN_BITS = {
    "DPAD_UP": 0x0001, "DPAD_DOWN": 0x0002, "DPAD_LEFT": 0x0004, "DPAD_RIGHT": 0x0008,
    "START": 0x0010, "BACK": 0x0020,
    "LEFTSTICK": 0x0040, "RIGHTSTICK": 0x0080,
    "LEFTSHOULDER": 0x0100, "RIGHTSHOULDER": 0x0200,
    "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
}
from .sdl2 import BUTTON_LABELS, AXIS_LABELS  # noqa: E402


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", wintypes.WORD),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


class XInputUnavailable(Exception):
    pass


class XInputBackend:
    def __init__(self):
        self.lib = None
        for name in ("XInput1_4.dll", "XInput9_1_0.dll", "XInput1_3.dll"):
            try:
                self.lib = ctypes.WinDLL(name)
                self.lib.XInputGetState.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_STATE)]
                self.lib.XInputGetState.restype = wintypes.DWORD
                self.dll_name = name
                break
            except OSError:
                continue
        if self.lib is None:
            raise XInputUnavailable("系统缺少 XInput 运行库")
        self.joy = None
        self.index = -1
        self.name = "未连接"
        self.layout = "Xbox"
        self.mode = "xinput"
        self.labels = {f"btn:{k}": BUTTON_LABELS.get(k, k) for k in BTN_BITS}
        self.labels.update({f"axis:{k}": v for k, v in AXIS_LABELS.items()})
        self._axis_ids = ["LX", "LY", "RX", "RY", "LT", "RT"]
        self._button_ids = list(BTN_BITS)

    def list_devices(self) -> list[dict]:
        out = []
        state = XINPUT_STATE()
        for i in range(4):
            if self.lib.XInputGetState(i, ctypes.byref(state)) == 0:
                out.append({"index": i, "name": f"Xbox 手柄 #{i + 1} (XInput)", "kind": "XInput"})
        return out

    def open(self, index: int) -> bool:
        devices = self.list_devices()
        if not any(d["index"] == index for d in devices):
            self.joy = None
            self.index = -1
            return False
        self.index = index
        self.joy = True
        self.name = f"Xbox 手柄 #{index + 1}"
        return True

    @property
    def connected(self) -> bool:
        if self.index < 0:
            return False
        state = XINPUT_STATE()
        return self.lib.XInputGetState(self.index, ctypes.byref(state)) == 0

    def sample(self) -> dict | None:
        if not self.joy:
            return None
        state = XINPUT_STATE()
        if self.lib.XInputGetState(self.index, ctypes.byref(state)) != 0:
            return None
        g = state.Gamepad

        def norm(v):
            return max(-1.0, min(1.0, v / 32767.0))

        # XInput 的 Y 轴向上为正，统一成 SDL 约定（向上为负）
        axes = {
            "LX": norm(g.sThumbLX), "LY": -norm(g.sThumbLY),
            "RX": norm(g.sThumbRX), "RY": -norm(g.sThumbRY),
            "LT": g.bLeftTrigger / 255.0, "RT": g.bRightTrigger / 255.0,
        }
        digital = {}
        raw_buttons = {}
        for key, bit in BTN_BITS.items():
            v = 1.0 if g.wButtons & bit else 0.0
            digital[f"btn:{key}"] = v
            raw_buttons[key] = v
        for t in ("LT", "RT"):
            digital[f"axis:{t}+"] = 1.0 if axes[t] > 0.55 else 0.0
        for aid in ("LX", "LY", "RX", "RY"):
            digital[f"axis:{aid}+"] = 1.0 if axes[aid] > 0.7 else 0.0
            digital[f"axis:{aid}-"] = 1.0 if axes[aid] < -0.7 else 0.0
        return {
            "name": self.name,
            "backend": f"XInput · {self.dll_name}",
            "layout": "Xbox",
            "axes": axes,
            "digital": digital,
            "raw_buttons": raw_buttons,
            "hats": [],
            "labels": self.labels,
            "axis_ids": self._axis_ids,
            "mode": "xinput",
        }

    def close(self):
        self.joy = None
        self.index = -1
        self.name = "未连接"

    def quit(self):
        self.close()
