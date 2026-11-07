"""手柄 → 鼠标 的核心引擎：后台轮询、按键映射、输入执行、状态快照。"""
from __future__ import annotations

import ctypes
import math
import threading
import time
from collections import deque
from datetime import datetime

from . import wininput
from .config import Config, RAW_BINDINGS, DEFAULT_CONFIG
from .sdl2 import SDL2Backend, SDLUnavailable, BUTTON_LABELS, AXIS_LABELS
from .xinput import XInputBackend, XInputUnavailable

ANALOG_ACTIONS = {"move_x", "move_y", "wheel_axis"}
DIGITAL_ACTIONS = {"left_click", "right_click", "wheel_up", "wheel_down", "prev", "next", "toggle"}
ACTION_TITLES = {
    "move_x": "光标水平移动", "move_y": "光标垂直移动",
    "left_click": "鼠标左键", "right_click": "鼠标右键",
    "wheel_axis": "滚轮（摇杆）", "wheel_up": "向上滚", "wheel_down": "向下滚",
    "prev": "上一步", "next": "下一步", "toggle": "暂停 / 继续",
}

SEMANTIC_TO_RAW = {
    "A": "0", "B": "1", "X": "2", "Y": "3",
    "LEFTSHOULDER": "4", "RIGHTSHOULDER": "5",
    "BACK": "8", "START": "9", "LEFTSTICK": "10", "RIGHTSTICK": "11", "GUIDE": "12",
    "DPAD_UP": "hat:0:up", "DPAD_DOWN": "hat:0:down",
    "DPAD_LEFT": "hat:0:left", "DPAD_RIGHT": "hat:0:right",
}
RAW_TO_SEMANTIC = {v: k for k, v in SEMANTIC_TO_RAW.items() if not v.startswith("hat:")}
HAT_TO_SEMANTIC = {
    "hat:0:up": "DPAD_UP", "hat:0:down": "DPAD_DOWN",
    "hat:0:left": "DPAD_LEFT", "hat:0:right": "DPAD_RIGHT",
}
AXIS_ALIASES = {
    "LX": "A0", "LY": "A1", "RX": "A2", "RY": "A3", "LT": "A4", "RT": "A5",
    "A0": "LX", "A1": "LY", "A2": "RX", "A3": "RY", "A4": "LT", "A5": "RT",
}


def shape(v: float, deadzone: float, curve: float) -> float:
    a = abs(v)
    if a <= deadzone or deadzone >= 1:
        return 0.0
    n = (a - deadzone) / (1.0 - deadzone)
    return math.copysign(min(1.0, n) ** curve, v)


def _variants(binding: str):
    """为一个绑定生成等价写法，兼容『标准映射』与『原始手柄』两种设备。"""
    yield binding
    if binding.startswith("btn:"):
        name = binding[4:]
        if name in SEMANTIC_TO_RAW:
            alias = SEMANTIC_TO_RAW[name]
            yield alias if alias.startswith("hat:") else "btn:" + alias
        if name in RAW_TO_SEMANTIC:
            yield "btn:" + RAW_TO_SEMANTIC[name]
    elif binding in HAT_TO_SEMANTIC:
        yield "btn:" + HAT_TO_SEMANTIC[binding]
    elif binding.startswith("axis:"):
        alias = AXIS_ALIASES.get(binding[5:])
        if alias:
            yield "axis:" + alias


class Engine:
    def __init__(self, config_path):
        self.cfg = Config(config_path)
        self.lock = threading.RLock()
        self.sdl = None
        self.xinput = None
        self.ns2 = None              # NS2 Pro 原始 HID 直读（由 app.py 注入）
        self.gameguard = None        # 游戏会话避让（由 app.py 注入）
        self._game_pause_applied = False
        self.backend_errors = []
        self.current = None            # (backend, index, device_id)
        self.last_sample = None
        self.devices = []
        self.log = deque(maxlen=60)
        self.enabled_note = ""
        self.bind = {"pending": False, "action": None, "result": None, "deadline": 0.0}
        self._bind_baseline = None
        self._action_prev: dict[str, bool] = {}
        self._held: set[str] = set()
        self._move_x_frac = 0.0
        self._move_y_frac = 0.0
        self._wheel_frac = 0.0
        self._step_frac = 0.0
        self._toggle_since = None
        self._toggle_latched = False
        self._force_rescan = True
        self._last_scan = 0.0
        self._hz = 0.0
        self._running = False
        self._thread = None
        self._init_backends()

    # ------------------------------------------------------------------ 后端
    def _init_backends(self):
        wanted = self.cfg.get()["backend"]
        if wanted in ("auto", "sdl2"):
            try:
                self.sdl = SDL2Backend()
                self._log(f"SDL2 已加载：{self.sdl.dll_file}")
            except (SDLUnavailable, OSError) as exc:
                self.sdl = None
                self.backend_errors.append(f"SDL2：{exc}")
                self._log(f"SDL2 不可用：{exc}")
        if wanted in ("auto", "xinput"):
            try:
                self.xinput = XInputBackend()
                self._log(f"XInput 已加载：{self.xinput.dll_name}")
            except (XInputUnavailable, OSError) as exc:
                self.xinput = None
                self.backend_errors.append(f"XInput：{exc}")
        if (self.sdl is None and self.xinput is None
                and wanted not in ("ns2",)
                and not (wanted == "auto" and self.ns2 is not None)):
            self._log("没有任何可用的手柄后端，请确认 SDL2.dll 与系统 XInput 运行库。")

    def _log(self, msg: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.log.append(f"[{stamp}] {msg}")

    @staticmethod
    def _score(d: dict) -> int:
        """自动选择时给设备打分，避免把鼠标/键盘类 HID 误当成手柄。"""
        name = d["name"].lower()
        score = 0
        if d["kind"] == "GameController":
            score += 100
        if d["backend"] == "xinput":
            score += 90
        if d["backend"] == "ns2":
            score += 200
        keywords = ("xbox", "switch", "nintendo", "pro controller", "joy-con", "ns2",
                    "gamepad", "controller", "wireless", "dualsense", "dualshock",
                    "playstation", "8bitdo", "gamesir", "flydigi", "beitong")
        if any(k in name for k in keywords):
            score += 60
        if "if_hid" in name or "virtual" in name:
            score -= 40
        return score

    def _collect_devices(self) -> list[dict]:
        out = []
        if self.ns2 and self.ns2.state.get("reports", 0) > 2:
            # NS2 Pro 需要厂商握手后才能上报数据，SDL2/XInput 都读不到它
            out.append({"id": "ns2:0", "backend": "ns2", "index": 0,
                        "name": "Nintendo Switch 2 Pro", "kind": "GameController"})
        if self.sdl:
            try:
                for d in self.sdl.list_devices():
                    out.append({**d, "id": f"sdl2:{d['index']}", "backend": "sdl2"})
            except Exception as exc:
                self._log(f"SDL2 枚举失败：{exc}")
        if self.xinput:
            try:
                for d in self.xinput.list_devices():
                    out.append({**d, "id": f"xinput:{d['index']}", "backend": "xinput"})
            except Exception as exc:
                self._log(f"XInput 枚举失败：{exc}")
        for d in out:
            d["score"] = self._score(d)
        out.sort(key=lambda d: (-d["score"], d["id"]))
        return out

    def _select(self, devices: list[dict]) -> dict | None:
        desired = self.cfg.get()["device"]
        if desired != "auto":
            return next((d for d in devices if d["id"] == desired), None)
        return devices[0] if devices else None

    def _ensure_device(self, force=False):
        now = time.monotonic()
        if not force and not self._force_rescan and now - self._last_scan < 0.9:
            return
        self._last_scan = now
        self._force_rescan = False
        devices = self._collect_devices()
        with self.lock:
            self.devices = devices
        target = self._select(devices)

        if self.current and target and self.current[2] == target["id"]:
            return
        # 关闭上一个 SDL 设备
        if self.current and self.current[0] == "sdl2" and self.sdl:
            self.sdl.close()
        if self.current and self.current[0] == "xinput" and self.xinput:
            self.xinput.close()
        self.current = None
        self.last_sample = None
        self._release_all()
        if not target:
            return

        backend = {"sdl2": self.sdl, "xinput": self.xinput,
                   "ns2": self.ns2}.get(target["backend"])
        if backend is None:
            return
        try:
            if backend.open(target["index"]):
                self.current = (target["backend"], target["index"], target["id"])
                self._log(f"已连接：{target['name']}（{target['kind']}）")
                if target["kind"] == "Joystick":
                    self._log("该设备按原始手柄读取；若按键不对，请用「重新绑定」逐项校准或点「套用通用手柄映射」。")
            else:
                self._log(f"打开设备失败：{target['name']}")
        except Exception as exc:
            self._log(f"打开设备异常：{exc}")

    # ------------------------------------------------------------------ 解析
    @staticmethod
    def _axis_value(binding: str, sample: dict) -> float:
        if not binding.startswith("axis:"):
            return 0.0
        axes = sample["axes"]
        for cand in _variants(binding):
            if cand.startswith("axis:") and cand[5:] in axes:
                return axes[cand[5:]]
        return 0.0

    @staticmethod
    def _digital_value(binding: str, sample: dict) -> float:
        d = sample["digital"]
        for cand in _variants(binding):
            if cand in d:
                return d[cand]
        # 标准映射设备用 btn:DPAD_UP，反之亦然
        if binding.startswith("axis:") and binding.endswith("+") and binding[5:-1] in ("LT", "RT"):
            return 1.0 if sample["axes"].get(binding[5:-1], 0) > 0.55 else 0.0
        return 0.0

    @staticmethod
    def _combo_parts(binding: str) -> list[str] | None:
        if not binding.startswith("combo:"):
            return None
        return [p if p.startswith("btn:") else "btn:" + p
                for p in binding[6:].split("+") if p.strip()]

    def _combo_pressed(self, binding: str, sample: dict) -> bool:
        parts = self._combo_parts(binding)
        if parts is None:
            return self._digital_value(binding, sample) > 0.5
        return all(self._digital_value(p, sample) > 0.5 for p in parts)

    # ------------------------------------------------------------------ 主循环
    def start(self):
        if self._running:
            return
        try:                       # 提升 Windows 定时器精度，让轮询更稳
            ctypes.WinDLL("winmm").timeBeginPeriod(1)
        except Exception:
            pass
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="gamepad-loop", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._release_all()
        if self.sdl:
            self.sdl.quit()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _loop(self):
        last = time.perf_counter()
        while self._running:
            t0 = time.perf_counter()
            dt = min(0.05, max(0.001, t0 - last))
            last = t0
            self._hz = self._hz * 0.92 + (1.0 / dt) * 0.08 if self._hz else 1.0 / dt
            try:
                self._ensure_device()
                backend = None
                if self.current:
                    backend = {"sdl2": self.sdl, "xinput": self.xinput,
                               "ns2": self.ns2}.get(self.current[0])
                sample = backend.sample() if backend else None
                if sample is None:
                    if self.current:
                        self._log("手柄已断开，等待重新连接…")
                        self._release_all()
                        self.current = None
                        self._force_rescan = True
                    self.last_sample = None
                else:
                    self.last_sample = sample
                    self._process(sample, dt)
            except Exception as exc:
                self._log(f"轮询异常：{exc}")
                time.sleep(0.2)
            elapsed = time.perf_counter() - t0
            time.sleep(max(0.001, 0.008 - elapsed))

    def _release_all(self):
        for name in list(self._held):
            try:
                wininput.button_up(name)
            except Exception:
                pass
        self._held.clear()
        self._action_prev.clear()

    def _process(self, sample: dict, dt: float):
        cfg = self.cfg.get()
        bind = cfg["bindings"]

        if self.bind["pending"]:
            self._learn(sample)
            return

        # 游戏会话：完全不注入任何鼠标/键盘事件（包括组合键），只继续读手柄做状态显示。
        # 判定基于「游戏进程是否还活着」，切到浏览器查攻略时同样保持暂停。
        if self.gameguard and self.gameguard.is_paused():
            if not self._game_pause_applied:
                self._release_all()
                self._move_x_frac = self._move_y_frac = 0.0
                self._wheel_frac = self._step_frac = 0.0
                self._toggle_since = None
                self._toggle_latched = False
                self._game_pause_applied = True
            return
        if self._game_pause_applied:
            # 恢复的这一帧：把当前已按住的键记为基线，避免误触发一次点击或导航
            self._game_pause_applied = False
            self._action_prev.clear()
            for action in ("prev", "next"):
                self._action_prev[action] = self._digital_value(bind[action], sample) > 0.5
            if self._digital_value(bind["left_click"], sample) > 0.5:
                self._held.add("left")
            if self._digital_value(bind["right_click"], sample) > 0.5:
                self._held.add("right")
            self._toggle_since = None
            self._toggle_latched = False
            self._log("游戏会话结束，恢复鼠标接管")
            return  # 恢复帧只重建基线，不注入，避免摇杆仍偏转时光标突然飘一下

        # 暂停/继续：即使已停用也要能重新启用
        toggle_now = self._combo_pressed(bind.get("toggle", "combo:BACK+START"), sample)
        need_hold = 0.4 if bind.get("toggle", "").startswith("combo:") else 0.18
        if toggle_now:
            if self._toggle_since is None:
                self._toggle_since = time.monotonic()
            elif not self._toggle_latched and time.monotonic() - self._toggle_since >= need_hold:
                self._toggle_latched = True
                self.cfg.update({"enabled": not cfg["enabled"]})
                state = "已启动" if not cfg["enabled"] else "已暂停"
                self._log(f"{state}鼠标控制（手柄 {ACTION_TITLES['toggle']}）")
                self._release_all()
                return
        else:
            self._toggle_since = None
            self._toggle_latched = False

        enabled = cfg["enabled"]
        dz = cfg["deadzone"]
        curve = cfg["curve"]
        swap = cfg["swap_sticks"]

        def swapped(b: str) -> str:
            if not b.startswith("axis:"):
                return b
            pair = {"LX": "RX", "LY": "RY", "RX": "LX", "RY": "LY"}
            return "axis:" + pair.get(b[5:], b[5:])

        values = {
            "move_x": self._axis_value(swapped(bind["move_x"]) if swap else bind["move_x"], sample),
            "move_y": self._axis_value(swapped(bind["move_y"]) if swap else bind["move_y"], sample),
            "wheel_axis": self._axis_value(swapped(bind["wheel_axis"]) if swap else bind["wheel_axis"], sample),
        }

        digital_now = {}
        for action in ("left_click", "right_click", "wheel_up", "wheel_down", "prev", "next"):
            digital_now[action] = self._digital_value(bind[action], sample) > 0.5

        if enabled:
            # 光标移动：浮点累加，避免小速度被整数截断丢失
            vx = shape(values["move_x"], dz, curve)
            vy = shape(values["move_y"], dz, curve)
            if cfg["invert_move_y"]:
                vy = -vy
            self._move_x_frac += vx * cfg["cursor_speed"] * dt
            self._move_y_frac += vy * cfg["cursor_speed"] * dt
            ix, iy = int(self._move_x_frac), int(self._move_y_frac)
            if ix or iy:
                self._move_x_frac -= ix
                self._move_y_frac -= iy
                wininput.move(ix, iy)

            # 摇杆滚轮
            wv = shape(values["wheel_axis"], dz, curve)
            if cfg["invert_wheel"]:
                wv = -wv
            self._wheel_frac += -wv * cfg["wheel_speed"] * dt
            # 十字键步进滚轮
            step = (1.0 if digital_now["wheel_up"] else 0.0) - (1.0 if digital_now["wheel_down"] else 0.0)
            self._step_frac += step * cfg["step_wheel_speed"] * dt
            total = self._wheel_frac + self._step_frac
            if abs(total) * 120 >= 1:
                wininput.wheel(total)
                self._wheel_frac = 0.0
                self._step_frac = 0.0

            # 左右键（支持按住拖拽）
            for action, btn in (("left_click", "left"), ("right_click", "right")):
                cur = digital_now[action]
                if cur and btn not in self._held:
                    wininput.button_down(btn)
                    self._held.add(btn)
                elif not cur and btn in self._held:
                    wininput.button_up(btn)
                    self._held.discard(btn)

            # 上一步 / 下一步（按下沿触发一次）
            nav = cfg["nav_mode"]
            for action, fn in (("prev", wininput.prev), ("next", wininput.next_action)):
                cur = digital_now[action]
                if cur and not self._action_prev.get(action, False):
                    fn(nav)
                self._action_prev[action] = cur
        else:
            if self._held:
                self._release_all()
            self._move_x_frac = self._move_y_frac = self._wheel_frac = self._step_frac = 0.0
            for action in ("prev", "next"):
                self._action_prev[action] = digital_now[action]

    # ------------------------------------------------------------------ 按键学习
    def start_bind(self, action: str):
        if action not in ACTION_TITLES:
            return False
        sample = self.last_sample
        self._bind_baseline = (
            dict(sample["axes"]) if sample else {},
            dict(sample["digital"]) if sample else {},
        )
        with self.lock:
            self.bind = {"pending": True, "action": action, "result": None,
                         "deadline": time.monotonic() + 12.0}
        self._log(f"请按下要绑定到「{ACTION_TITLES[action]}」的按键 / 拨动摇杆…")
        return True

    def cancel_bind(self):
        with self.lock:
            self.bind = {"pending": False, "action": None, "result": None, "deadline": 0.0}
        self._bind_baseline = None
        self._log("已取消按键学习")

    def _learn(self, sample: dict):
        with self.lock:
            action = self.bind["action"]
            deadline = self.bind["deadline"]
            pending = self.bind["pending"]
        if not pending:
            return
        if time.monotonic() > deadline:
            self.cancel_bind()
            self._log("按键学习超时，已取消")
            return

        base_axes, base_dig = self._bind_baseline or ({}, {})
        found = None
        if action in ANALOG_ACTIONS:
            best = 0.0
            for aid, v in sample["axes"].items():
                delta = abs(v - base_axes.get(aid, 0.0))
                if delta > 0.42 and abs(v) > 0.55 and delta > best:
                    best, found = delta, "axis:" + aid
        else:
            candidates = []
            for sid, v in sample["digital"].items():
                if v > 0.7 and base_dig.get(sid, 0.0) < 0.3:
                    prio = 0 if sid.startswith("btn:") else (1 if sid.startswith("hat:") else 2)
                    candidates.append((prio, sid))
            if candidates:
                candidates.sort()
                found = candidates[0][1]
            # 组合键学习：BACK + START 同时按下
            if action == "toggle" and not found:
                pass

        if not found:
            return
        self.cfg.update({"bindings": {action: found}})
        with self.lock:
            self.bind = {"pending": False, "action": None, "result": found, "deadline": 0.0}
        self._bind_baseline = None
        self._log(f"「{ACTION_TITLES[action]}」已绑定 → {self.label_of(found, sample)}")

    # ------------------------------------------------------------------ 状态
    @staticmethod
    def label_of(source: str, sample: dict | None = None) -> str:
        labels = (sample or {}).get("labels", {})
        if source in labels:
            return labels[source]
        # 标准映射 / 原始编号互为别名，显示时补成易懂的名称
        for cand in _variants(source):
            if cand in labels:
                return labels[cand]
        if source.startswith("combo:"):
            parts = source[6:].split("+")
            return " + ".join(Engine.label_of("btn:" + p if not p.startswith("btn:") else p, sample)
                              for p in parts)
        if source.startswith("btn:"):
            name = source[4:]
            return BUTTON_LABELS.get(name, labels.get("btn:" + name, f"按钮 {name}"))
        if source.startswith("axis:"):
            aid = source[5:].rstrip("+-")
            label = AXIS_LABELS.get(aid, labels.get("axis:" + aid, f"轴 {aid}"))
            if source.endswith("+"):
                label += " 正方向"
            elif source.endswith("-"):
                label += " 负方向"
            return label
        if source.startswith("hat:"):
            _, h, d = source.split(":")
            return {"up": "十字键 ↑", "down": "十字键 ↓", "left": "十字键 ←",
                    "right": "十字键 →"}.get(d, f"十字键 {h}:{d}")
        return source

    def state(self) -> dict:
        cfg = self.cfg.get()
        with self.lock:
            sample = self.last_sample
            devices = list(self.devices)
            logs = list(self.log)[-12:]
            bind = dict(self.bind)
        snap = {
            "ok": True,
            "enabled": cfg["enabled"],
            "config": cfg,
            "device": None,
            "devices": [{"id": d["id"], "name": d["name"], "kind": d["kind"]} for d in devices],
            "selected": self.current[2] if self.current else cfg["device"],
            "inputs": {"axes": {}, "digital": {}, "active": [], "buttons": {}, "hats": []},
            "binding_labels": {},
            "bind": {"pending": bind["pending"], "action": bind["action"], "result": bind["result"]},
            "log": logs,
            "stats": {"hz": round(self._hz, 1), "errors": self.backend_errors},
            "game": self.gameguard.brief() if self.gameguard else None,
            "titles": ACTION_TITLES,
        }
        if sample:
            snap["device"] = {
                "name": sample["name"], "backend": sample["backend"],
                "layout": sample["layout"], "connected": True, "mode": sample["mode"],
                "axis_ids": sample["axis_ids"],
            }
            snap["inputs"] = {
                "axes": {k: round(v, 3) for k, v in sample["axes"].items()},
                "digital": {k: round(v, 2) for k, v in sample["digital"].items()},
                "active": [k for k, v in sample["digital"].items() if v > 0.5],
                "buttons": sample["raw_buttons"],
                "hats": sample["hats"],
            }
            snap["labels"] = sample["labels"]
        for key, src in cfg["bindings"].items():
            snap["binding_labels"][key] = self.label_of(src, sample)
        snap["bind"]["pending"] = bool(bind["pending"] and time.monotonic() < bind["deadline"])
        return snap

    # ------------------------------------------------------------------ 命令
    def command(self, cmd: str, payload: dict) -> dict:
        if cmd == "toggle":
            cfg = self.cfg.update({"enabled": not self.cfg.get()["enabled"]})
            self._log("鼠标控制已" + ("启动" if cfg["enabled"] else "暂停"))
            if not cfg["enabled"]:
                self._release_all()
        elif cmd == "set_enabled":
            cfg = self.cfg.update({"enabled": bool(payload.get("value"))})
            self._log("鼠标控制已" + ("启动" if cfg["enabled"] else "暂停"))
            if not cfg["enabled"]:
                self._release_all()
        elif cmd == "config":
            patch = payload.get("patch") or {}
            old_backend = self.cfg.get()["backend"]
            cfg = self.cfg.update(patch)
            if cfg["backend"] != old_backend:
                self._log("后端已切换，重新初始化…")
                self._reinit_backends()
            if any(str(k).startswith("game_") for k in patch) and self.gameguard:
                self.gameguard.rescan()
            self._force_rescan = True
        elif cmd == "bind_start":
            self.start_bind(payload.get("action", ""))
        elif cmd == "bind_cancel":
            self.cancel_bind()
        elif cmd == "rescan":
            self._force_rescan = True
            self._log("已重新扫描手柄")
        elif cmd == "defaults":
            self.cfg.reset()
            self._log("已恢复默认设置")
            self._force_rescan = True
        elif cmd == "raw_defaults":
            self.cfg.update({"bindings": RAW_BINDINGS})
            self._log("已套用通用手柄映射")
        elif cmd == "test":
            self._test(payload.get("action", ""), self.cfg.get()["nav_mode"])
        return {"ok": True}

    def _reinit_backends(self):
        try:
            if self.sdl:
                self.sdl.quit()
        except Exception:
            pass
        self.sdl = None
        self.xinput = None
        self.current = None
        self.last_sample = None
        self.backend_errors = []
        self._init_backends()

    def _test(self, action: str, nav: str):
        if self.gameguard and self.gameguard.is_paused():
            self._log("游戏会话进行中，已跳过测试动作")
            return
        try:
            if action == "left_click":
                wininput.button_down("left")
                wininput.button_up("left")
            elif action == "right_click":
                wininput.button_down("right")
                wininput.button_up("right")
            elif action == "wheel_up":
                wininput.wheel(3)
            elif action == "wheel_down":
                wininput.wheel(-3)
            elif action == "prev":
                wininput.prev(nav)
            elif action == "next":
                wininput.next_action(nav)
            elif action == "move":
                wininput.move(0, -60)
            self._log(f"已发送测试动作：{ACTION_TITLES.get(action, action)}")
        except Exception as exc:
            self._log(f"测试动作失败：{exc}")
