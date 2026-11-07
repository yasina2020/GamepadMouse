"""SDL2 手柄后端（ctypes 直连 SDL2.dll）。

优先使用 GameController 标准映射（Xbox / Switch Pro 自动按位置统一），
设备不被 SDL 认识为手柄时回退到原始 Joystick（轴 / 按钮 / 十字键），
保证 NS2 Pro 这类新设备也能被读取和手动绑定。
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

SDL_INIT_JOYSTICK = 0x00000200
SDL_INIT_GAMECONTROLLER = 0x00002000
SDL_INIT_EVENTS = 0x00004000

AXIS_NAMES = ["LEFTX", "LEFTY", "RIGHTX", "RIGHTY", "TRIGGERLEFT", "TRIGGERRIGHT"]
BUTTON_NAMES = [
    "A", "B", "X", "Y", "BACK", "GUIDE", "START",
    "LEFTSTICK", "RIGHTSTICK", "LEFTSHOULDER", "RIGHTSHOULDER",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
    "MISC1", "PADDLE1", "PADDLE2", "PADDLE3", "PADDLE4", "TOUCHPAD",
]

# 位置化命名：SDL 默认按「按键所处位置」映射，Switch Pro 的 A/B 与 Xbox 相反
BUTTON_LABELS = {
    "A": "A · 下方键", "B": "B · 右方键", "X": "X · 左方键", "Y": "Y · 上方键",
    "BACK": "View / −", "GUIDE": "Guide / HOME", "START": "Menu / +",
    "LEFTSTICK": "左摇杆按压", "RIGHTSTICK": "右摇杆按压",
    "LEFTSHOULDER": "LB / L", "RIGHTSHOULDER": "RB / R",
    "DPAD_UP": "十字键 ↑", "DPAD_DOWN": "十字键 ↓",
    "DPAD_LEFT": "十字键 ←", "DPAD_RIGHT": "十字键 →",
    "MISC1": "分享 / 截图", "PADDLE1": "背键 P1", "PADDLE2": "背键 P2",
    "PADDLE3": "背键 P3", "PADDLE4": "背键 P4", "TOUCHPAD": "触控板",
}

AXIS_LABELS = {
    "LX": "左摇杆 ←→", "LY": "左摇杆 ↑↓",
    "RX": "右摇杆 ←→", "RY": "右摇杆 ↑↓",
    "LT": "LT / ZL", "RT": "RT / ZR",
}

HAT_DIRS = {1: "up", 2: "right", 4: "down", 8: "left"}


class SDLUnavailable(Exception):
    pass


class SDL2Backend:
    """封装单个 SDL2 手柄设备。"""

    def __init__(self, dll_path: str | Path | None = None):
        candidates = []
        if dll_path:
            candidates.append(Path(dll_path))
        here = Path(__file__).resolve().parent.parent / "lib" / "SDL2.dll"
        candidates.append(here)
        candidates.append(Path("SDL2.dll"))

        self.lib = None
        self.dll_file = None
        for path in candidates:
            if path.is_file():
                try:
                    self.lib = ctypes.CDLL(str(path))
                    self.dll_file = str(path)
                    break
                except OSError:
                    continue
        if self.lib is None:
            try:
                self.lib = ctypes.CDLL("SDL2.dll")
                self.dll_file = "SDL2.dll (系统 PATH)"
            except OSError as exc:
                raise SDLUnavailable(f"找不到可用的 SDL2.dll：{exc}") from exc

        self._bind_api()
        self._init_sdl()

    # ------------------------------------------------------------------ API
    def _bind_api(self):
        L = self.lib
        c_int = ctypes.c_int
        c_uint = ctypes.c_uint
        c_void = ctypes.c_void_p
        c_char = ctypes.c_char_p

        def fn(name, restype, argtypes):
            f = getattr(L, name, None)
            if f is None:
                return None
            f.restype = restype
            f.argtypes = argtypes
            return f

        self.SDL_SetHint = fn("SDL_SetHint", c_int, [c_char, c_char])
        self.SDL_Init = fn("SDL_Init", c_int, [c_uint])
        self.SDL_Quit = fn("SDL_Quit", None, [])
        self.SDL_GetError = fn("SDL_GetError", c_char, [])
        self.SDL_PumpEvents = fn("SDL_PumpEvents", None, [])
        self.SDL_NumJoysticks = fn("SDL_NumJoysticks", c_int, [])
        self.SDL_JoystickOpen = fn("SDL_JoystickOpen", c_void, [c_int])
        self.SDL_JoystickClose = fn("SDL_JoystickClose", None, [c_void])
        self.SDL_JoystickName = fn("SDL_JoystickName", c_char, [c_void])
        self.SDL_JoystickNameForIndex = fn("SDL_JoystickNameForIndex", c_char, [c_int])
        self.SDL_JoystickNumAxes = fn("SDL_JoystickNumAxes", c_int, [c_void])
        self.SDL_JoystickNumButtons = fn("SDL_JoystickNumButtons", c_int, [c_void])
        self.SDL_JoystickNumHats = fn("SDL_JoystickNumHats", c_int, [c_void])
        self.SDL_JoystickGetAxis = fn("SDL_JoystickGetAxis", c_int, [c_void, c_int])
        self.SDL_JoystickGetButton = fn("SDL_JoystickGetButton", ctypes.c_uint8, [c_void, c_int])
        self.SDL_JoystickGetHat = fn("SDL_JoystickGetHat", ctypes.c_uint8, [c_void, c_int])
        self.SDL_JoystickUpdate = fn("SDL_JoystickUpdate", None, [])
        self.SDL_JoystickGetAttached = fn("SDL_JoystickGetAttached", c_int, [c_void])
        self.SDL_JoystickInstanceID = fn("SDL_JoystickInstanceID", ctypes.c_int32, [c_void])
        self.SDL_IsGameController = fn("SDL_IsGameController", c_int, [c_int])
        self.SDL_GameControllerOpen = fn("SDL_GameControllerOpen", c_void, [c_int])
        self.SDL_GameControllerClose = fn("SDL_GameControllerClose", None, [c_void])
        self.SDL_GameControllerName = fn("SDL_GameControllerName", c_char, [c_void])
        self.SDL_GameControllerGetAxis = fn("SDL_GameControllerGetAxis", c_int, [c_void, c_int])
        self.SDL_GameControllerGetButton = fn("SDL_GameControllerGetButton", ctypes.c_uint8, [c_void, c_int])
        self.SDL_GameControllerUpdate = fn("SDL_GameControllerUpdate", None, [])
        self.SDL_GameControllerGetJoystick = fn("SDL_GameControllerGetJoystick", c_void, [c_void])
        self.SDL_GameControllerMapping = fn("SDL_GameControllerMapping", c_void, [c_void])
        self.SDL_free = fn("SDL_free", None, [c_void])

        required = ["SDL_Init", "SDL_NumJoysticks", "SDL_JoystickOpen",
                    "SDL_JoystickGetAxis", "SDL_JoystickGetButton"]
        missing = [n for n in required if getattr(self, n, None) is None]
        if missing:
            raise SDLUnavailable("SDL2.dll 缺少必要导出：" + ", ".join(missing))

    def _init_sdl(self):
        hints = {
            b"SDL_JOYSTICK_HIDAPI": b"1",
            b"SDL_JOYSTICK_HIDAPI_SWITCH": b"1",
            b"SDL_JOYSTICK_RAWINPUT": b"1",
            b"SDL_GAMECONTROLLER_USE_BUTTON_LABELS": b"0",
            b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS": b"1",
            b"SDL_HINT_JOYSTICK_THREAD": b"1",
        }
        if self.SDL_SetHint:
            for k, v in hints.items():
                self.SDL_SetHint(k, v)
        rc = self.SDL_Init(SDL_INIT_JOYSTICK | SDL_INIT_GAMECONTROLLER | SDL_INIT_EVENTS)
        if rc != 0:
            raise SDLUnavailable("SDL_Init 失败：" + self._err())
        self.joy = None
        self.gc = None
        self.index = -1
        self.mode = "none"
        self.name = "未连接"
        self.layout = "未知"
        self.labels = {}
        self._axis_ids: list[str] = []
        self._button_ids: list[str] = []
        self._hat_count = 0

    def _err(self) -> str:
        try:
            return (self.SDL_GetError() or b"").decode("utf-8", "replace")
        except Exception:
            return "未知错误"

    @staticmethod
    def _text(ptr) -> str:
        if not ptr:
            return ""
        if isinstance(ptr, bytes):
            return ptr.decode("utf-8", "replace")
        return ctypes.string_at(ptr).decode("utf-8", "replace")

    # -------------------------------------------------------------- 设备枚举
    def list_devices(self) -> list[dict]:
        self.SDL_PumpEvents()
        out = []
        for i in range(self.SDL_NumJoysticks()):
            name = self._text(self.SDL_JoystickNameForIndex(i)) or f"手柄 #{i + 1}"
            is_gc = bool(self.SDL_IsGameController(i)) if self.SDL_IsGameController else False
            out.append({"index": i, "name": name, "kind": "GameController" if is_gc else "Joystick"})
        return out

    def close(self):
        if self.gc:
            self.SDL_GameControllerClose(self.gc)
            self.gc = None
        if self.joy:
            self.SDL_JoystickClose(self.joy)
            self.joy = None
        self.mode = "none"
        self.index = -1
        self.name = "未连接"

    def open(self, index: int) -> bool:
        self.close()
        if index < 0 or index >= self.SDL_NumJoysticks():
            return False
        self.index = index
        is_gc = bool(self.SDL_IsGameController(index)) if self.SDL_IsGameController else False
        if is_gc and self.SDL_GameControllerOpen:
            self.gc = self.SDL_GameControllerOpen(index)
        if self.gc:
            self.mode = "gamecontroller"
            self.name = self._text(self.SDL_GameControllerName(self.gc)) or "手柄"
            self.joy = self.SDL_GameControllerGetJoystick(self.gc)
            mapping = ""
            if self.SDL_GameControllerMapping:
                ptr = self.SDL_GameControllerMapping(self.gc)
                if ptr:
                    try:
                        mapping = ctypes.string_at(ptr).decode("utf-8", "replace")
                    finally:
                        if self.SDL_free:
                            self.SDL_free(ptr)
            self._axis_ids = ["LX", "LY", "RX", "RY", "LT", "RT"]
            self._button_ids = BUTTON_NAMES[:]
            self.labels = {f"btn:{n}": BUTTON_LABELS.get(n, n) for n in BUTTON_NAMES}
        else:
            self.joy = self.SDL_JoystickOpen(index)
            if not self.joy:
                return False
            self.mode = "joystick"
            self.name = self._text(self.SDL_JoystickName(self.joy)) or "手柄"
            mapping = ""
            na = max(0, self.SDL_JoystickNumAxes(self.joy))
            nb = max(0, self.SDL_JoystickNumButtons(self.joy))
            # 常见 HID 轴序：0=LX 1=LY 2=RX(或 LT) 3=RY 4=RT 5=...
            preset = ["LX", "LY", "RX", "RY", "LT", "RT"]
            self._axis_ids = [preset[i] if i < len(preset) else f"A{i}" for i in range(na)]
            self._button_ids = [str(i) for i in range(nb)]
            self.labels = {f"btn:{i}": f"按钮 {i}" for i in range(nb)}

        low = (self.name + " " + mapping).lower()
        if "switch" in low or "nintendo" in low or "pro controller" in low or "joy-con" in low or "nsw" in low:
            self.layout = "Switch / NS Pro"
        elif "xbox" in low or "xinput" in low:
            self.layout = "Xbox"
        elif "playstation" in low or "dualsense" in low or "dualshock" in low:
            self.layout = "PlayStation"
        else:
            self.layout = "通用手柄"
        self._hat_count = max(0, self.SDL_JoystickNumHats(self.joy)) if self.joy else 0
        for a in self._axis_ids:
            self.labels.setdefault(f"axis:{a}", AXIS_LABELS.get(a, f"轴 {a}"))
        return True

    @property
    def connected(self) -> bool:
        if not self.joy:
            return False
        try:
            return bool(self.SDL_JoystickGetAttached(self.joy))
        except Exception:
            return False

    # ------------------------------------------------------------------ 采样
    def sample(self) -> dict | None:
        if not self.joy:
            return None
        self.SDL_PumpEvents()
        if self.mode == "gamecontroller":
            self.SDL_GameControllerUpdate()
        else:
            self.SDL_JoystickUpdate()
        if not self.connected:
            return None

        axes: dict[str, float] = {}
        if self.mode == "gamecontroller":
            for i, aid in enumerate(self._axis_ids):
                raw = self.SDL_GameControllerGetAxis(self.gc, i)
                if i >= 4:  # 扳机：SDL 为 0..32767
                    axes[aid] = max(-1.0, min(1.0, raw / 32767.0))
                else:
                    axes[aid] = max(-1.0, min(1.0, raw / 32767.0))
        else:
            for i, aid in enumerate(self._axis_ids):
                raw = self.SDL_JoystickGetAxis(self.joy, i)
                axes[aid] = max(-1.0, min(1.0, raw / 32767.0))

        digital: dict[str, float] = {}
        raw_buttons: dict[str, float] = {}
        if self.mode == "gamecontroller":
            for i, bid in enumerate(self._button_ids):
                try:
                    v = 1.0 if self.SDL_GameControllerGetButton(self.gc, i) else 0.0
                except Exception:
                    v = 0.0
                digital[f"btn:{bid}"] = v
                raw_buttons[bid] = v
        else:
            for i in range(len(self._button_ids)):
                v = 1.0 if self.SDL_JoystickGetButton(self.joy, i) else 0.0
                digital[f"btn:{self._button_ids[i]}"] = v
                raw_buttons[self._button_ids[i]] = v
            # 原始模式下，用前两个轴 + 第 3/4 轴补出十字键/扳机的数字量
        # 扳机也提供数字量，方便绑定到点击
        for t in ("LT", "RT"):
            if t in axes:
                digital[f"axis:{t}+"] = 1.0 if axes[t] > 0.55 else 0.0
        # 摇杆半轴也可作为数字量绑定
        for aid, val in axes.items():
            if aid in ("LT", "RT"):
                continue
            digital[f"axis:{aid}+"] = 1.0 if val > 0.7 else 0.0
            digital[f"axis:{aid}-"] = 1.0 if val < -0.7 else 0.0

        hats = []
        for h in range(self._hat_count):
            v = int(self.SDL_JoystickGetHat(self.joy, h))
            hats.append(v)
            for bit, d in HAT_DIRS.items():
                digital[f"hat:{h}:{d}"] = 1.0 if v & bit else 0.0

        return {
            "name": self.name,
            "backend": "SDL2 · 标准映射" if self.mode == "gamecontroller" else "SDL2 · 原始手柄",
            "layout": self.layout,
            "axes": axes,
            "digital": digital,
            "raw_buttons": raw_buttons,
            "hats": hats,
            "labels": self.labels,
            "axis_ids": self._axis_ids,
            "mode": self.mode,
        }

    def quit(self):
        self.close()
        try:
            self.SDL_Quit()
        except Exception:
            pass
