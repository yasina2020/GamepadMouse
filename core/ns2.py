"""Nintendo Switch 2 Pro (VID 057E / PID 2069) 唤醒 + 原始 HID 报告读取。

Windows 上这只手柄上电后默认「沉默」：HID 接口只会吐出全 0 的报告，
必须先由主机通过厂商接口 (MI_01 / WinUSB) 发一段初始化命令，才会开始
输出 64 字节状态报告。本模块用纯 ctypes 调 WinUSB / HID API 完成这件事，
不依赖 SDL3、libusb 或任何第三方包。

初始化序列来自 SDL3 的 src/joystick/hidapi/SDL_hidapi_switch2.c。
报告解析（按钮位 / 12bit 摇杆）同样对齐该驱动。
"""
from __future__ import annotations

import ctypes
import json
import threading
import time
import winreg
from collections import deque
from ctypes import wintypes
from pathlib import Path

VID, PID = 0x057E, 0x2069
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_RW, OPEN_EXISTING = 3, 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
INVALID_HANDLE = ctypes.c_void_p(-1).value
PIPE_TRANSFER_TIMEOUT = 3

# ---- 摇杆中心自动校准 ----------------------------------------------------
# 12bit 摇杆，物理回中值通常在 2048 附近（实测 NS2 Pro 约 1970~2230）。
# 刚唤醒时前几帧报告里的摇杆字段还是过渡值，绝不能拿它当中心，
# 否则静止时会被算成「一直在推某个方向」，光标就会自己飘走。
BOOT_FRAMES = 24            # 跳过的启动过渡帧
CENTER_MIN, CENTER_MAX = 1650, 2450   # 合理的回中区间
JITTER_MAX = 8             # 相邻两帧最大抖动（静止判定）
STILL_SECONDS = 0.8        # 连续静止多久后重设中心
MIN_SAMPLES = 40           # 静止窗口最少样本数
HARD_ZERO = 18             # 归一化前的硬零区（原始计数）
CENTER_SAVE_MIN_GAP = 4.0  # 中心点落盘节流（秒）

# SDL3 HIDAPI_DriverSwitch2_InitUSB 的 init_sequence
INIT_SEQUENCE = [
    "07 91 00 01 00 00 00 00",
    "0c 91 00 02 00 04 00 00 27 00 00 00",
    "11 91 00 01 00 00 00 00",
    "0a 91 00 08 00 14 00 00 01 ff ff ff ff ff ff ff ff 35 00 46 00 00 00 00 00 00 00 00",
    "0c 91 00 04 00 04 00 00 27 00 00 00",
    "01 91 00 0c 00 00 00 00",
    "01 91 00 01 00 00 00 00",
    "08 91 00 02 00 04 00 00 01 00 00 00",
    "03 91 00 0a 00 04 00 00 05 00 00 00",  # 选择报告 0x05
    "03 91 00 0d 00 08 00 00 01 00 ff ff ff ff ff ff",  # 开始输出
]

# (显示名, 字节下标, 位掩码)  —— 下标是完整 64 字节 USB 包的下标
BUTTONS = [
    ("Y", 5, 0x01), ("X", 5, 0x02), ("B", 5, 0x04), ("A", 5, 0x08),
    ("R", 5, 0x40), ("ZR", 5, 0x80),
    ("− 减号", 6, 0x01), ("+ 加号", 6, 0x02),
    ("右摇杆按下", 6, 0x04), ("左摇杆按下", 6, 0x08),
    ("HOME", 6, 0x10), ("截图", 6, 0x20), ("C", 6, 0x40),
    ("十字键 ↓", 7, 0x01), ("十字键 ↑", 7, 0x02),
    ("十字键 →", 7, 0x04), ("十字键 ←", 7, 0x08),
    ("L", 7, 0x40), ("ZL", 7, 0x80),
    ("GL 背键", 8, 0x01), ("GR 背键", 8, 0x02),
]

# 印刷字母 → 按键「位置」语义（与 SDL 标准手柄映射一致）。
# Switch 与 Xbox 的 A/B、X/Y 印刷字母正好相反，这里按位置统一：
# 下方键一律是 A（左键），右方键一律是 B（右键）。
SEMANTIC = {
    "Y": "X", "X": "Y", "B": "A", "A": "B",
    "R": "RIGHTSHOULDER", "ZR": "RIGHTTRIGGER",
    "− 减号": "BACK", "+ 加号": "START",
    "右摇杆按下": "RIGHTSTICK", "左摇杆按下": "LEFTSTICK",
    "HOME": "GUIDE", "截图": "CAPTURE", "C": "C",
    "十字键 ↓": "DPAD_DOWN", "十字键 ↑": "DPAD_UP",
    "十字键 →": "DPAD_RIGHT", "十字键 ←": "DPAD_LEFT",
    "L": "LEFTSHOULDER", "ZL": "LEFTTRIGGER",
    "GL 背键": "PADDLE_LEFT", "GR 背键": "PADDLE_RIGHT",
}


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]


class USB_INTERFACE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ubyte) for n in (
        "bLength", "bDescriptorType", "bInterfaceNumber", "bAlternateSetting",
        "bNumEndpoints", "bInterfaceClass", "bInterfaceSubClass",
        "bInterfaceProtocol", "iInterface")]


class WINUSB_PIPE_INFORMATION(ctypes.Structure):
    _fields_ = [("PipeType", ctypes.c_int), ("PipeId", ctypes.c_ubyte),
                ("MaximumPacketSize", wintypes.USHORT), ("Interval", ctypes.c_ubyte)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


def _load_api():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wu = ctypes.WinDLL("winusb", use_last_error=True)
    cfg = ctypes.WinDLL("cfgmgr32", use_last_error=True)

    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
    k32.ReadFile.restype = wintypes.BOOL
    k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
    k32.WriteFile.restype = wintypes.BOOL
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    k32.ResetEvent.argtypes = [wintypes.HANDLE]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                        ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    k32.GetOverlappedResult.restype = wintypes.BOOL
    k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    k32.CancelIoEx.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    wu.WinUsb_Initialize.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    wu.WinUsb_Initialize.restype = wintypes.BOOL
    wu.WinUsb_QueryInterfaceSettings.argtypes = [ctypes.c_void_p, ctypes.c_ubyte,
                                                  ctypes.POINTER(USB_INTERFACE_DESCRIPTOR)]
    wu.WinUsb_QueryInterfaceSettings.restype = wintypes.BOOL
    wu.WinUsb_QueryPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte,
                                    ctypes.POINTER(WINUSB_PIPE_INFORMATION)]
    wu.WinUsb_QueryPipe.restype = wintypes.BOOL
    wu.WinUsb_SetPipePolicy.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, wintypes.ULONG,
                                        wintypes.ULONG, ctypes.c_void_p]
    wu.WinUsb_SetPipePolicy.restype = wintypes.BOOL
    wu.WinUsb_WritePipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p,
                                    wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
                                    ctypes.c_void_p]
    wu.WinUsb_WritePipe.restype = wintypes.BOOL
    wu.WinUsb_ReadPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p,
                                   wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
                                   ctypes.c_void_p]
    wu.WinUsb_ReadPipe.restype = wintypes.BOOL
    wu.WinUsb_Free.argtypes = [ctypes.c_void_p]
    wu.WinUsb_Free.restype = wintypes.BOOL

    cfg.CM_Get_Device_Interface_List_SizeW.argtypes = [
        ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(GUID), wintypes.LPCWSTR,
        wintypes.ULONG]
    cfg.CM_Get_Device_Interface_List_SizeW.restype = wintypes.DWORD
    cfg.CM_Get_Device_Interface_ListW.argtypes = [
        ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.LPWSTR,
        wintypes.ULONG, wintypes.ULONG]
    cfg.CM_Get_Device_Interface_ListW.restype = wintypes.DWORD
    return k32, wu, cfg


_K32 = _WU = _CFG = None


def api():
    global _K32, _WU, _CFG
    if _K32 is None:
        _K32, _WU, _CFG = _load_api()
    return _K32, _WU, _CFG


def _guid(data1, data2, data3, tail):
    return GUID(data1, data2, data3, (ctypes.c_ubyte * 8)(*tail))


HID_GUID = _guid(0x4D1E55B2, 0xF16F, 0x11CF,
                 (0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30))
DEFAULT_WINUSB_GUID = _guid(0x6F13725E, 0xEF0E, 0x4FD3,
                            (0xAE, 0x5F, 0xB2, 0xDE, 0x98, 0x9E, 0xC8, 0x25))


def _interface_list(guid) -> list[str]:
    """列出某设备接口类的所有已存在接口路径。"""
    _, _, cfg = api()
    size = wintypes.ULONG()
    ret = cfg.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(guid),
                                                 None, 0)
    if ret != 0 or not size.value:
        return []
    buf = ctypes.create_unicode_buffer(size.value + 2)
    ret = cfg.CM_Get_Device_Interface_ListW(ctypes.byref(guid), None, buf,
                                            size.value + 2, 0)
    if ret != 0:
        return []
    text = bytes(buf).decode("utf-16-le", errors="ignore")
    return [p for p in text.split("\x00") if p]


def _winusb_guids() -> list[GUID]:
    """从注册表读 MI_01 的 DeviceInterfaceGUID，附一个已知默认值兜底。"""
    out, seen = [], set()
    try:
        base = (r"SYSTEM\CurrentControlSet\Enum\USB"
                r"\VID_057E&PID_2069&MI_01")
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            try:
                dk = winreg.OpenKey(key, sub + r"\Device Parameters")
                val, _ = winreg.QueryValueEx(dk, "DeviceInterfaceGUID")
                val = val.strip("{}")
                parts = val.split("-")
                if len(parts) == 5 and val not in seen:
                    tail = bytes.fromhex(parts[3] + parts[4])
                    out.append(_guid(int(parts[0], 16), int(parts[1], 16),
                                     int(parts[2], 16), tuple(tail)))
                    seen.add(val)
            except (OSError, ValueError):
                pass
        winreg.CloseKey(key)
    except OSError:
        pass
    if not seen:
        out.append(DEFAULT_WINUSB_GUID)
    return out


def find_paths() -> dict:
    hid = [p for p in _interface_list(HID_GUID)
           if "vid_057e&pid_2069" in p.lower() and "mi_00" in p.lower()]
    winusb = []
    for g in _winusb_guids():
        winusb += [p for p in _interface_list(g)
                   if "vid_057e&pid_2069" in p.lower() and "mi_01" in p.lower()]
    return {"hid": hid[0] if hid else None,
            "winusb": winusb[0] if winusb else None,
            "hid_all": hid, "winusb_all": winusb}


class WinUSB:
    """NS2 Pro 的厂商 bulk 命令通道。"""

    def __init__(self, path: str):
        k32, wu, _ = api()
        self.k32, self.wu = k32, wu
        self.h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW,
                                 None, OPEN_EXISTING, 0, None)
        if not self.h or self.h == INVALID_HANDLE:
            raise OSError(ctypes.get_last_error(), "打开 WinUSB 接口失败")
        self.wh = ctypes.c_void_p()
        if not wu.WinUsb_Initialize(self.h, ctypes.byref(self.wh)):
            err = ctypes.get_last_error()
            k32.CloseHandle(self.h)
            raise OSError(err, "WinUsb_Initialize 失败")
        desc = USB_INTERFACE_DESCRIPTOR()
        if not wu.WinUsb_QueryInterfaceSettings(self.wh, 0, ctypes.byref(desc)):
            raise OSError(ctypes.get_last_error(), "读取接口描述符失败")
        self.out_ep = self.in_ep = None
        for i in range(desc.bNumEndpoints):
            p = WINUSB_PIPE_INFORMATION()
            if wu.WinUsb_QueryPipe(self.wh, 0, i, ctypes.byref(p)) and p.PipeType == 2:
                if p.PipeId & 0x80:
                    self.in_ep = p.PipeId
                else:
                    self.out_ep = p.PipeId
        if self.out_ep is None or self.in_ep is None:
            raise OSError(0, "未找到 bulk IN/OUT 端点")
        timeout = wintypes.ULONG(350)
        for ep in (self.out_ep, self.in_ep):
            wu.WinUsb_SetPipePolicy(self.wh, ep, PIPE_TRANSFER_TIMEOUT, 4,
                                    ctypes.byref(timeout))

    def command(self, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data)
        sent = wintypes.ULONG()
        if not self.wu.WinUsb_WritePipe(self.wh, self.out_ep, buf, len(data),
                                         ctypes.byref(sent), None):
            raise OSError(ctypes.get_last_error(), "bulk 写失败")
        reply = ctypes.create_string_buffer(64)
        got = wintypes.ULONG()
        self.wu.WinUsb_ReadPipe(self.wh, self.in_ep, reply, 64,
                                ctypes.byref(got), None)
        return reply.raw[:got.value]

    def initialize(self) -> list[dict]:
        log = []
        for i, item in enumerate(INIT_SEQUENCE, 1):
            cmd = bytes.fromhex(item)
            try:
                ack = self.command(cmd)
                log.append({"step": i, "ok": True, "cmd": item,
                            "ack": ack[:16].hex(" ")})
            except OSError as exc:
                log.append({"step": i, "ok": False, "cmd": item, "error": str(exc)})
            time.sleep(0.02)
        return log

    def close(self):
        try:
            self.wu.WinUsb_Free(self.wh)
        finally:
            self.k32.CloseHandle(self.h)


def parse_report(data: bytes) -> dict:
    """解析 64 字节状态包 → 按钮 / 12bit 摇杆。"""
    if len(data) < 17:
        return {"buttons": [], "digital": {}, "axes": {}}
    pressed = [name for name, idx, mask in BUTTONS if data[idx] & mask]
    lx = data[11] | ((data[12] & 0x0F) << 8)
    ly = (data[12] >> 4) | (data[13] << 4)
    rx = data[14] | ((data[15] & 0x0F) << 8)
    ry = (data[15] >> 4) | (data[16] << 4)
    return {
        "buttons": pressed,
        "digital": {f"btn:{name}": 1 for name in pressed},
        "axes": {"LX": lx, "LY": ly, "RX": rx, "RY": ry},
        "counter": int.from_bytes(data[1:5], "little", signed=False),
    }


class NS2Probe:
    """后台读 HID 报告，记录按键事件，供测试页与主应用使用。"""

    def __init__(self, center_file: Path | None = None):
        self.lock = threading.RLock()
        self.events: deque = deque(maxlen=400)
        self.logs: deque = deque(maxlen=60)
        self.state = {
            "found": False, "initialized": False, "initializing": False,
            "streaming": False, "reports": 0, "hz": 0.0, "last_report": "",
            "changed": [], "buttons": [], "axes": {}, "digital": {},
            "hex": "", "error": "",
        }
        self.paths = {}
        self.winusb: WinUSB | None = None
        self._reader_thread = None
        self._stop = threading.Event()
        self._prev_buttons: set[str] = set()
        self._prev_report: bytes | None = None
        self._last_stick_evt = 0.0
        self._window = deque(maxlen=120)

        # 摇杆中心：启动过渡帧保护 + 静止时自动纠偏 + 落盘复用
        self._neutral: dict[str, float] = {}
        self._calibrated = False
        self._boot_frames = 0
        self._boot_samples: list[dict] = []
        self._calib_last: dict | None = None
        self._calib_stable_since = 0.0
        self._calib_window: deque = deque(maxlen=240)
        self._last_calib_log = 0.0
        self._last_center_save = 0.0
        self.center_file = Path(center_file) if center_file else (
            Path(__file__).resolve().parent.parent / "stick_center.json")
        self._load_center()

    # ------------------------------------------------------------ 日志
    def _log(self, text: str):
        with self.lock:
            self.logs.append({"t": time.strftime("%H:%M:%S"), "text": text})
            self.events.append({"t": round(time.time() * 1000), "kind": "log",
                                "text": text})

    # -------------------------------------------------------- 摇杆中心校准
    def _load_center(self):
        """复用上次的校准值；值不合理就丢弃，重新自动校准。"""
        try:
            if not self.center_file.exists():
                return
            d = json.loads(self.center_file.read_text(encoding="utf-8"))
            vals = {k: float(d[k]) for k in ("LX", "LY", "RX", "RY") if k in d}
            if len(vals) == 4 and all(CENTER_MIN <= v <= CENTER_MAX for v in vals.values()):
                self._neutral = vals
                self._calibrated = True
                self._boot_frames = BOOT_FRAMES
                self._log("已载入上次保存的摇杆中心：" + self._center_text(vals))
        except Exception as exc:
            self._log(f"摇杆中心缓存读取失败（将自动校准）：{exc}")

    def _center_text(self, center: dict) -> str:
        return "  ".join(f"{k}={v:.0f}" for k, v in center.items())

    def _save_center(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_center_save < CENTER_SAVE_MIN_GAP:
            return
        self._last_center_save = now
        try:
            self.center_file.write_text(
                json.dumps({k: round(v, 1) for k, v in self._neutral.items()},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self._log(f"摇杆中心保存失败：{exc}")

    def _calibrate(self, axes: dict, now: float):
        """每帧调用（调用方持有 self.lock）。

        1) 跳过唤醒后的过渡帧，用后半段稳定样本取中位数做初始中心；
        2) 之后只要连续静止 ~0.8 秒，且读数落在物理回中区间内，
           就用窗口均值重设中心 —— 这样即使初始中心采错也能自愈，
           同时不会把「推着摇杆停住」误当成回中。
        """
        if not axes or not all(k in axes for k in ("LX", "LY", "RX", "RY")):
            return

        if self._boot_frames < BOOT_FRAMES:
            self._boot_frames += 1
            self._boot_samples.append(dict(axes))
            if self._boot_frames == BOOT_FRAMES:
                tail = self._boot_samples[BOOT_FRAMES // 2:]
                spread = max(max(w[k] for w in tail) - min(w[k] for w in tail)
                             for k in axes)
                if spread <= 40:
                    self._neutral = {k: sum(w[k] for w in tail) / len(tail)
                                     for k in axes}
                    self._calibrated = True
                    self._save_center(force=True)
                    self._log("摇杆中心自动校准完成：" + self._center_text(self._neutral))
                else:
                    self._log("启动时摇杆未回中，等待静止后自动纠偏…")
            return

        jitter = (max(abs(axes[k] - self._calib_last.get(k, axes[k])) for k in axes)
                  if self._calib_last is not None else 999.0)
        self._calib_last = dict(axes)
        if jitter <= JITTER_MAX:
            if not self._calib_stable_since:
                self._calib_stable_since = now
            self._calib_window.append(dict(axes))
        else:
            self._calib_stable_since = 0.0
            self._calib_window.clear()

        if (self._calib_stable_since and now - self._calib_stable_since >= STILL_SECONDS
                and len(self._calib_window) >= MIN_SAMPLES):
            avg = {k: sum(w[k] for w in self._calib_window) / len(self._calib_window)
                   for k in axes}
            plausible = all(CENTER_MIN <= v <= CENTER_MAX for v in avg.values())
            drift = max(abs(avg[k] - self._neutral.get(k, avg[k])) for k in avg)
            if plausible and drift > 10:
                self._neutral = avg
                self._calibrated = True
                self._save_center()
                if now - self._last_calib_log > 6:
                    self._last_calib_log = now
                    self._log(f"摇杆中心已纠偏（偏差 {drift:.0f}）：" +
                              self._center_text(avg))
            self._calib_stable_since = 0.0
            self._calib_window.clear()

    def recenter(self) -> dict:
        """手动校准：把当前静止读数设为摇杆中心。"""
        with self.lock:
            axes = dict(self.state.get("axes") or {})
            if not all(k in axes for k in ("LX", "LY", "RX", "RY")):
                return {"ok": False, "message": "还没读到手柄数据"}
            if not all(CENTER_MIN <= v <= CENTER_MAX for v in axes.values()):
                return {"ok": False, "message": "请先松开两个摇杆，让它们回中，再点校准"}
            self._neutral = {k: float(v) for k, v in axes.items()}
            self._calibrated = True
            self._boot_frames = BOOT_FRAMES
            self._calib_stable_since = 0.0
            self._calib_window.clear()
            self._save_center(force=True)
            text = self._center_text(self._neutral)
        self._log("已手动校准摇杆中心：" + text)
        return {"ok": True, "neutral": dict(self._neutral),
                "message": "摇杆中心已更新，光标漂移应该立即停止"}

    # ------------------------------------------------------------ 设备
    def refresh_paths(self):
        try:
            self.paths = find_paths()
        except Exception as exc:
            self.paths = {"hid": None, "winusb": None, "hid_all": [], "winusb_all": []}
            self.state["error"] = f"枚举失败：{exc}"
        with self.lock:
            self.state["found"] = bool(self.paths.get("hid"))
        return self.paths

    def probe_once(self, timeout_ms: int = 500) -> bool:
        """独立读一帧 HID 报告，判断手柄是否已经在正常输出。"""
        k32, _, _ = api()
        self.refresh_paths()
        h = self._open_hid()
        if not h:
            return False
        event = k32.CreateEventW(None, True, False, None)
        buf = ctypes.create_string_buffer(64)
        alive = False
        try:
            for _ in range(2):
                ov = OVERLAPPED()
                ov.hEvent = event
                k32.ResetEvent(event)
                got = wintypes.DWORD()
                ok = k32.ReadFile(h, buf, 64, ctypes.byref(got), ctypes.byref(ov))
                if not ok and ctypes.get_last_error() == ERROR_IO_PENDING:
                    if k32.WaitForSingleObject(event, timeout_ms) != WAIT_OBJECT_0:
                        break
                    if not k32.GetOverlappedResult(h, ctypes.byref(ov),
                                                   ctypes.byref(got), True):
                        break
                elif not ok:
                    break
                if got.value >= 17:
                    data = buf.raw[:got.value]
                    axes = parse_report(data)["axes"]
                    if any(axes.values()):
                        alive = True
                        break
        finally:
            k32.CloseHandle(event)
            k32.CloseHandle(h)
        return alive

    def initialize(self, force: bool = False) -> dict:
        """确保手柄进入报告模式；若已经在输出则无需再发命令。"""
        with self.lock:
            if self.state["initializing"]:
                return {"ok": False, "message": "正在初始化中"}
            self.state["initializing"] = True
        try:
            already = (self.state["streaming"] and self.state["reports"] > 3)
            if not already and not force:
                already = self.probe_once()
            if already:
                self._log("手柄已经在正常输出报告，无需重复唤醒")
                with self.lock:
                    self.state["initialized"] = True
                    self.state["error"] = ""
                return {"ok": True, "steps": [], "already": True,
                        "message": "手柄已在输出数据，无需唤醒"}
            self.refresh_paths()
            p = self.paths.get("winusb")
            if not p:
                self._log("未找到 WinUSB 厂商接口，无法唤醒（手柄可能没插 USB 线）")
                self.state["error"] = "未找到 WinUSB 厂商接口（确认用 USB 线连接）"
                return {"ok": False, "message": self.state["error"]}
            if self.winusb is None:
                self.winusb = WinUSB(p)
            self._log("已打开 WinUSB 通道，bulk OUT=0x%02x IN=0x%02x" %
                      (self.winusb.out_ep, self.winusb.in_ep))
            steps = self.winusb.initialize()
            good = sum(1 for s in steps if s["ok"])
            ok = good == len(steps) or (good == 0 and self.probe_once())
            self._log(f"初始化命令发送完毕：{good}/{len(steps)} 成功，"
                      f"报告流{'正常' if ok else '仍未就绪'}")
            with self.lock:
                self.state["initialized"] = ok
                self.state["error"] = "" if ok else "初始化后仍未收到有效报告"
            return {"ok": ok, "steps": steps, "message":
                    f"{good}/{len(steps)} 条命令已发送" + ("（设备已有其他程序唤醒）" if ok and good == 0 else "")}
        except Exception as exc:
            # 通道被别的程序独占是常见情况；只要报告流正常就视为成功
            if self.probe_once(250):
                self._log("命令通道不可用，但报告流已由其他程序唤醒")
                self.state["initialized"] = True
                self.state["error"] = ""
                return {"ok": True, "steps": [], "already": True,
                        "message": "命令通道被占用，但手柄已由其他程序唤醒"}
            self._log(f"初始化失败：{type(exc).__name__}: {exc}")
            self.state["error"] = str(exc)
            return {"ok": False, "message": str(exc)}
        finally:
            with self.lock:
                self.state["initializing"] = False

    def initialize_async(self):
        t = threading.Thread(target=self.initialize, name="ns2-init", daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------ 读线程
    def start(self):
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._stop.clear()
        self.refresh_paths()
        self._reader_thread = threading.Thread(target=self._reader_loop,
                                               name="ns2-hid", daemon=True)
        self._reader_thread.start()

    def stop(self):
        self._stop.set()
        if self.winusb:
            try:
                self.winusb.close()
            except Exception:
                pass
            self.winusb = None

    def _open_hid(self):
        k32, _, _ = api()
        path = self.paths.get("hid")
        if not path:
            return None
        h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW, None,
                            OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if not h or h == INVALID_HANDLE:
            return None
        return h

    def _reader_loop(self):
        k32, _, _ = api()
        event = k32.CreateEventW(None, True, False, None)
        handle = None
        last_path_check = 0.0
        idle_logged = False
        while not self._stop.is_set():
            if not handle:
                if time.monotonic() - last_path_check > 1.0:
                    last_path_check = time.monotonic()
                    self.refresh_paths()
                    handle = self._open_hid()
                    if handle:
                        self._log("HID 报告通道已打开")
                        idle_logged = False
                if not handle:
                    self._stop.wait(0.5)
                    continue
            buf = ctypes.create_string_buffer(64)
            ov = OVERLAPPED()
            ov.hEvent = event
            k32.ResetEvent(event)
            got = wintypes.DWORD()
            ok = k32.ReadFile(handle, buf, 64, ctypes.byref(got), ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err == ERROR_IO_PENDING:
                    if k32.WaitForSingleObject(event, 400) != WAIT_OBJECT_0:
                        k32.CancelIoEx(handle, ctypes.byref(ov))
                        k32.WaitForSingleObject(event, 100)
                        if not idle_logged and self.state["reports"] == 0:
                            idle_logged = True
                        continue
                    if not k32.GetOverlappedResult(handle, ctypes.byref(ov),
                                                   ctypes.byref(got), True):
                        continue
                else:
                    k32.CloseHandle(handle)
                    handle = None
                    self._log("HID 通道断开，等待手柄重新连接…")
                    self._stop.wait(0.5)
                    continue
            if got.value < 17:
                continue
            data = buf.raw[:got.value]
            self._accept(data)
        if handle:
            k32.CloseHandle(handle)
        k32.CloseHandle(event)

    def _accept(self, data: bytes):
        now = time.time()
        parsed = parse_report(data)
        with self.lock:
            st = self.state
            st["reports"] += 1
            st["hex"] = data.hex(" ")
            st["last_report"] = "%02X" % data[0]
            st["buttons"] = parsed["buttons"]
            st["digital"] = parsed["digital"]
            st["axes"] = parsed["axes"]
            st["streaming"] = any(parsed["axes"].values()) or bool(parsed["buttons"])
            self._calibrate(parsed["axes"], now)
            # 与上一帧比较「有效载荷」字节（跳过前面的计数器）
            if self._prev_report is not None:
                st["changed"] = [i for i in range(5, min(17, len(data)))
                                 if data[i] != self._prev_report[i]]
            self._prev_report = data
            self._window.append(now)
            if len(self._window) > 2:
                span = self._window[-1] - self._window[0]
                st["hz"] = round((len(self._window) - 1) / span, 1) if span > 0 else 0

            # 按键边沿
            cur = set(parsed["buttons"])
            for name in sorted(cur - self._prev_buttons):
                self.events.append({"t": int(now * 1000), "kind": "down", "text": name})
            for name in sorted(self._prev_buttons - cur):
                self.events.append({"t": int(now * 1000), "kind": "up", "text": name})
            self._prev_buttons = cur
            # 摇杆移动（限频）
            if now - self._last_stick_evt > 0.25 and self._neutral:
                axes = parsed["axes"]
                moved = [f"{k}={v}" for k, v in axes.items()
                         if abs(v - self._neutral.get(k, v)) > 90]
                if moved:
                    self._last_stick_evt = now
                    self.events.append({"t": int(now * 1000), "kind": "axis",
                                        "text": "  ".join(moved)})

    # ------------------------------------------------------------ 状态
    def clear(self):
        with self.lock:
            self.events.clear()
            self.logs.clear()
            self.state["reports"] = 0
            self._prev_buttons.clear()
            self._window.clear()

    def snapshot(self) -> dict:
        with self.lock:
            st = dict(self.state)
            st["paths"] = {k: (v if isinstance(v, list) else v)
                           for k, v in self.paths.items()}
            st["events"] = list(self.events)[-120:]
            st["logs"] = list(self.logs)[-30:]
            st["neutral"] = {k: round(v) for k, v in self._neutral.items()}
            st["calibrated"] = self._calibrated
            st["center_drift"] = round(max(
                (abs(self.state["axes"].get(k, v) - v)
                 for k, v in self._neutral.items()), default=0))
        return st

    # 主引擎使用：兼容 SDL2 后端 open/close/sample 三个方法
    def open(self, index: int = 0) -> bool:
        return bool(self.paths.get("hid")) or self.state["reports"] > 0

    def close(self):
        pass

    def sample(self) -> dict | None:
        """给主引擎用的标准手柄采样（按位置语义，与 SDL 标准映射一致）。"""
        with self.lock:
            if not self.state["axes"] or self.state["reports"] < 2:
                return None
            raw = dict(self.state["axes"])
            pressed = list(self.state["buttons"])
            neutral = dict(self._neutral)
            # 中心尚未校准时一律输出 0，绝不让过渡帧或错采的中心把光标带跑
            booting = not self._calibrated
        norm = {}
        for k, v in raw.items():
            c = neutral.get(k, 2048)
            if booting or abs(v - c) < HARD_ZERO:
                norm[k] = 0.0
                continue
            span = c if v < c else max(1, 4095 - c)
            norm[k] = max(-1.0, min(1.0, (v - c) / span))
        # SDL 对 Y 轴取反：原始值越大 = 摇杆越向上；引擎约定正值 = 向下
        norm["LY"] = -norm.get("LY", 0.0)
        norm["RY"] = -norm.get("RY", 0.0)
        norm["LT"] = 1.0 if "ZL" in pressed else 0.0
        norm["RT"] = 1.0 if "ZR" in pressed else 0.0

        digital = {}
        for label in pressed:
            sem = SEMANTIC.get(label)
            if sem:
                digital["btn:" + sem] = 1.0
                if sem.startswith("DPAD_"):
                    direction = sem[5:].lower()
                    digital[f"hat:0:{direction}"] = 1.0
        return {
            "name": "Nintendo Switch 2 Pro", "backend": "ns2", "layout": "Switch",
            "mode": "gamecontroller", "axes": norm, "digital": digital,
            "raw_buttons": {}, "hats": [0], "labels": {},
            "axis_ids": ["LX", "LY", "RX", "RY"],
        }


if __name__ == "__main__":
    import json
    p = NS2Probe()
    print("paths:", json.dumps(p.refresh_paths(), ensure_ascii=False, indent=2))
    print("init:", json.dumps(p.initialize(), ensure_ascii=False)[:400])
    p.start()
    time.sleep(2)
    s = p.snapshot()
    print("state:", json.dumps({k: v for k, v in s.items()
                               if k not in ("events", "logs", "paths")},
                              ensure_ascii=False, indent=2))
    print("events:", s["events"][-8:])
    p.stop()
