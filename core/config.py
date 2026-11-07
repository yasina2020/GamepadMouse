"""配置读写与默认值。"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

DEFAULT_CONFIG = {
    "enabled": True,
    "backend": "auto",          # auto | sdl2 | xinput
    "device": "auto",           # auto | "sdl2:0" | "xinput:0"
    "cursor_speed": 900,        # 满偏转时像素/秒
    "deadzone": 0.12,
    "curve": 1.5,               # 指数曲线，越大越精细
    "wheel_speed": 14,          # 满偏转时滚轮格/秒
    "step_wheel_speed": 9,      # 按住十字键时格/秒
    "nav_mode": "side_buttons", # side_buttons | undo_redo | alt_arrows
    # 游戏会话避让：游戏运行期间完全停止注入鼠标/按键
    "game_guard": True,
    "game_mode": "strict",      # strict(全程暂停) | foreground(仅游戏在前台) | off
    "game_whitelist": [],       # 手动指定游戏 exe 名或路径片段
    "game_grace": 2.0,          # 游戏退出后宽限秒数
    "swap_sticks": False,       # 用右摇杆移动光标
    "invert_move_y": False,
    "invert_wheel": False,
    "bindings": {
        "move_x": "axis:LX",
        "move_y": "axis:LY",
        "left_click": "btn:A",
        "right_click": "btn:B",
        "wheel_axis": "axis:RY",
        "wheel_up": "hat:0:up",
        "wheel_down": "hat:0:down",
        "prev": "btn:LEFTSHOULDER",
        "next": "btn:RIGHTSHOULDER",
        "toggle": "combo:BACK+START",
    },
    "bindings_customized": False,
}

# 设备只能以「原始手柄」被识别时（例如部分 NS2 Pro / 国产手柄），
# 用常见 HID 轴序给一套可直接上手的默认映射；用户可随时一键重新学习。
RAW_BINDINGS = {
    "move_x": "axis:LX",
    "move_y": "axis:LY",
    "left_click": "btn:0",
    "right_click": "btn:1",
    "wheel_axis": "axis:RY",
    "wheel_up": "hat:0:up",
    "wheel_down": "hat:0:down",
    "prev": "btn:4",
    "next": "btn:5",
    "toggle": "combo:8+9",
}

NUMERIC_RANGES = {
    "cursor_speed": (100, 3000),
    "deadzone": (0.0, 0.45),
    "curve": (1.0, 3.5),
    "wheel_speed": (1, 60),
    "step_wheel_speed": (1, 60),
    "game_grace": (0.0, 10.0),
}
BOOL_KEYS = {"enabled", "swap_sticks", "invert_move_y", "invert_wheel",
             "bindings_customized", "game_guard"}
BINDING_KEYS = set(DEFAULT_CONFIG["bindings"])


class Config:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            with self.lock:
                merged = copy.deepcopy(DEFAULT_CONFIG)
                merged.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
                if isinstance(raw.get("bindings"), dict):
                    merged["bindings"].update(
                        {k: v for k, v in raw["bindings"].items() if k in BINDING_KEYS}
                    )
                self.data = merged
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        with self.lock:
            data = copy.deepcopy(self.data)
        try:
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def get(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def update(self, patch: dict) -> dict:
        with self.lock:
            for k, v in patch.items():
                if k in NUMERIC_RANGES:
                    lo, hi = NUMERIC_RANGES[k]
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        continue
                    self.data[k] = round(max(lo, min(hi, v)), 3)
                elif k in BOOL_KEYS:
                    self.data[k] = bool(v)
                elif k == "nav_mode" and v in ("side_buttons", "undo_redo", "alt_arrows"):
                    self.data[k] = v
                elif k == "backend" and v in ("auto", "sdl2", "xinput", "ns2"):
                    self.data[k] = v
                elif k == "game_mode" and v in ("strict", "foreground", "off"):
                    self.data[k] = v
                elif k == "game_whitelist" and isinstance(v, list):
                    items = []
                    for item in v[:60]:
                        text = str(item).strip().replace("/", "\\")[:200]
                        if text and text.lower() not in {x.lower() for x in items}:
                            items.append(text)
                    self.data[k] = items
                elif k == "device":
                    self.data[k] = str(v)[:64]
                elif k == "bindings" and isinstance(v, dict):
                    for bk, bv in v.items():
                        if bk in BINDING_KEYS and isinstance(bv, str):
                            self.data["bindings"][bk] = bv[:64]
                            self.data["bindings_customized"] = True
        self.save()
        return self.get()

    def reset(self) -> dict:
        with self.lock:
            self.data = copy.deepcopy(DEFAULT_CONFIG)
        self.save()
        return self.get()
