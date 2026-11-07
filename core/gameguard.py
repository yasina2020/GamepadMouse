"""游戏会话检测：玩游戏期间完全停止接管鼠标，避免与游戏抢输入。

设计要点
--------
用户有两个场景：

1. 游戏正在运行            -> 不接管
2. 游戏开着但切出来查攻略  -> 仍然不接管

所以判定单位不是「前台窗口」，而是「游戏会话」：一旦某个进程被识别为游戏，
就锁住它，只要该进程还活着（不管当前焦点在浏览器还是桌面）就一直暂停；
进程退出后再宽限几秒才恢复，避免切关卡 / 重启游戏时抖动。

识别信号（多路径，自动 + 手动，互相兜底）
  * 手动白名单：exe 名或路径片段，最可靠
  * 游戏平台目录：steamapps/common、Epic、XboxGames、GOG、EA、Riot 等
  * 全屏窗口：前台窗口铺满整块显示器且进程不在「非游戏」名单里
  * 系统信号：SHQueryUserNotificationState 返回 QUNS_RUNNING_D3D_FULL_SCREEN

所有 API 只用 ctypes + Windows 标准库，不需要额外依赖或管理员权限。
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

# ---------------------------------------------------------------- 常量
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
MONITOR_DEFAULTTONEAREST = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4

# 游戏平台安装目录特征（小写，路径里出现即认为该进程是游戏）
PLATFORM_MARKERS = (
    "\\steamapps\\common\\",
    "\\epic games\\",
    "\\xboxgames\\",
    "\\gog galaxy\\games\\",
    "\\gog games\\",
    "\\ea games\\",
    "\\ubisoft connect\\games\\",
    "\\riot games\\",
    "\\battle.net\\",
    "\\xbox games\\",
    "\\amazon games\\library\\",
)

# 平台目录下的启动器 / 反作弊辅助进程，不该被当成游戏本体
PLATFORM_EXCLUDES = (
    "\\launcher\\", "\\riot client\\", "\\battle.net.exe",
    "\\epicgameslauncher", "\\galaxyclient", "\\eadesktop",
    "\\origin.exe", "\\upc.exe", "\\easyanticheat", "\\battleye",
    "\\crashhandler", "\\crashreportclient", "\\installer\\",
)

# 游戏目录里的辅助进程（崩溃上报、反作弊、安装器等），不能当游戏本体锁定
HELPER_KEYWORDS = (
    "crashpad", "crashhandler", "crashreport", "easyanticheat", "battleye",
    "beservice", "be_service", "vcredist", "redist", "unins", "setup",
    "launcher", "updater", "update.exe", "report", "helper", "handler",
    "dotnet", "dxsetup", "directx", "install",
)

# 明确不算游戏的常见程序：防止全屏看视频 / F11 网页 / 直播软件被误判
NON_GAME_EXES = {
    "explorer.exe", "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe",
    "opera.exe", "vivaldi.exe", "msedgewebview2.exe", "360chrome.exe",
    "python.exe", "pythonw.exe", "cmd.exe", "conhost.exe", "powershell.exe",
    "pwsh.exe", "steam.exe", "steamwebhelper.exe", "steamservice.exe",
    "discord.exe", "obs64.exe", "obs32.exe", "streamlabs obs.exe",
    "vlc.exe", "mpv.exe", "potplayer.exe", "potplayermini64.exe",
    "code.exe", "notepad.exe", "notepad++.exe", "mspaint.exe",
    "applicationframehost.exe", "shellexperiencehost.exe", "searchhost.exe",
    "taskmgr.exe", "dwm.exe", "winlogon.exe", "csrss.exe", "svchost.exe",
    "runtimebroker.exe", "textinputhost.exe", "widgetservice.exe",
    "workbuddy.exe", "wechat.exe", "qq.exe", "dingtalk.exe", "feishu.exe",
    "photoshop.exe", "illustrator.exe", "premiere pro.exe",
    "windowsterminal.exe", "openconsole.exe", "regedit.exe", "mmc.exe",
}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class Win32:
    """集中绑定需要的 Win32 函数；任何一项缺失都只降级，不让程序崩。"""

    def __init__(self):
        self.ok = True
        try:
            self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.u32 = ctypes.WinDLL("user32", use_last_error=True)
            self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        except OSError:
            self.ok = False
            return
        k, u, s = self.k32, self.u32, self.shell32

        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32FirstW.restype = wintypes.BOOL
        k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.restype = wintypes.BOOL
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.MonitorFromWindow.restype = wintypes.HANDLE
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        u.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL

        s.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
        s.SHQueryUserNotificationState.restype = ctypes.c_long

    # ------------------------------------------------------------ 进程
    def processes(self) -> list[dict]:
        if not self.ok:
            return []
        snap = self.k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return []
        out = []
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            more = self.k32.Process32FirstW(snap, ctypes.byref(entry))
            while more:
                pid = int(entry.th32ProcessID)
                out.append({"pid": pid, "name": entry.szExeFile})
                entry.dwSize = ctypes.sizeof(entry)
                more = self.k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            self.k32.CloseHandle(snap)
        return out

    def process_path(self, pid: int) -> str:
        if not self.ok or pid <= 0:
            return ""
        h = self.k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h or h == ctypes.c_void_p(-1).value:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if self.k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            self.k32.CloseHandle(h)
        return ""

    # ------------------------------------------------------------ 窗口
    def foreground(self) -> dict | None:
        if not self.ok:
            return None
        hwnd = self.u32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        self.u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(512)
        self.u32.GetWindowTextW(hwnd, buf, 512)
        cls = ctypes.create_unicode_buffer(128)
        self.u32.GetClassNameW(hwnd, cls, 128)
        fullscreen = False
        rect = RECT()
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        mon = self.u32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if (self.u32.GetWindowRect(hwnd, ctypes.byref(rect)) and mon
                and self.u32.GetMonitorInfoW(mon, ctypes.byref(mi))):
            m = mi.rcMonitor
            fullscreen = (rect.left <= m.left + 2 and rect.top <= m.top + 2
                          and rect.right >= m.right - 2 and rect.bottom >= m.bottom - 2)
        return {"pid": int(pid.value), "title": buf.value, "class": cls.value,
                "fullscreen": fullscreen,
                "desktop": cls.value in ("Progman", "WorkerW", "Shell_TrayWnd")}

    def windows(self) -> list[dict]:
        """有可见标题的顶层窗口，供 UI 让用户手动把某个程序标记为游戏。"""
        if not self.ok:
            return []
        rows = []

        def cb(hwnd, _):
            try:
                if not self.u32.IsWindowVisible(hwnd):
                    return True
                n = self.u32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(min(n + 2, 512))
                self.u32.GetWindowTextW(hwnd, buf, len(buf))
                pid = wintypes.DWORD()
                self.u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                rows.append({"pid": int(pid.value), "title": buf.value})
            except Exception:
                pass
            return True

        try:
            self.u32.EnumWindows(WNDENUMPROC(cb), 0)
        except Exception:
            pass
        return rows

    def shell_state(self) -> int:
        if not self.ok:
            return 0
        val = ctypes.c_int()
        try:
            self.shell32.SHQueryUserNotificationState(ctypes.byref(val))
            return val.value
        except Exception:
            return 0


class GameGuard:
    """独立线程每 ~350ms 扫描一次；引擎每帧只读 is_paused()，不拖慢轮询。"""

    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or (lambda msg: None)
        self.win = Win32()
        self.lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._path_cache: dict[int, str] = {}
        self._wake = threading.Event()

        self._paused = False
        self._lock_pid: int | None = None
        self._lock_info: dict | None = None
        self._reason = ""
        self._since = 0.0
        self._grace_until = 0.0
        self._override_pid: int | None = None
        self._override_until = 0.0
        self._last_scan = 0.0
        self._last_error = ""
        self._games_now: list[dict] = []
        self._foreground: dict | None = None
        self._windows: list[dict] = []
        self._scans = 0

    # ------------------------------------------------------------ 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="game-guard", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def rescan(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
            self._wake.wait(0.35)
            self._wake.clear()

    # ------------------------------------------------------------ 判定
    @staticmethod
    def _whitelist_hit(name: str, path: str, entries: list[str]) -> str:
        low_name, low_path = name.lower(), path.lower()
        for raw in entries:
            e = str(raw).strip().lower().replace("/", "\\")
            if not e:
                continue
            if e == low_name or (len(e) > 3 and e in low_path):
                return raw
        return ""

    @staticmethod
    def _is_game_exe(name: str, path: str) -> bool:
        """平台目录里的进程是否像「游戏本体」，过滤 .dll / 崩溃上报 / 反作弊辅助进程。"""
        n = name.lower()
        if not n.endswith(".exe") or n in NON_GAME_EXES:
            return False
        if any(k in n for k in HELPER_KEYWORDS):
            return False
        low = path.lower().replace("/", "\\")
        return not any(x in low for x in PLATFORM_EXCLUDES)

    @staticmethod
    def _platform_hit(path: str) -> str:
        low = path.lower().replace("/", "\\")
        if any(x in low for x in PLATFORM_EXCLUDES):
            return ""
        for marker in PLATFORM_MARKERS:
            if marker in low:
                return marker.strip("\\")
        return ""

    def scan(self):
        cfg = self.cfg.get()
        enabled = bool(cfg.get("game_guard", True))
        mode = cfg.get("game_mode", "strict")
        whitelist = list(cfg.get("game_whitelist", []) or [])
        grace = float(cfg.get("game_grace", 2.0) or 0)
        now = time.monotonic()

        procs = self.win.processes()
        # 路径按 pid 缓存，避免每轮对每个进程反复 OpenProcess
        alive = {p["pid"] for p in procs}
        for pid in list(self._path_cache):
            if pid not in alive:
                self._path_cache.pop(pid, None)
        for p in procs:
            if p["pid"] not in self._path_cache:
                self._path_cache[p["pid"]] = self.win.process_path(p["pid"])

        fg = self.win.foreground()
        quns = self.win.shell_state()
        by_pid = {p["pid"]: p for p in procs}
        win_rows = self.win.windows()
        window_pids = {w["pid"] for w in win_rows}

        games = []
        for p in procs:
            name, path = p["name"], self._path_cache.get(p["pid"], "")
            signals = []
            hit = self._whitelist_hit(name, path, whitelist)
            if hit:
                signals.append("白名单")
            plat = self._platform_hit(path)
            if plat and self._is_game_exe(name, path):
                signals.append("游戏目录")
            if signals:
                games.append({
                    "pid": p["pid"], "name": name, "path": path,
                    "signals": signals,
                    "foreground": bool(fg and fg["pid"] == p["pid"]),
                    "has_window": p["pid"] in window_pids,
                    # 白名单是用户明确指定，永远可锁定；平台目录必须像游戏本体
                    "lockable": bool(hit) or (bool(plat) and self._is_game_exe(name, path)),
                })

        # 全屏前台窗口：覆盖「独立安装、不在平台目录」的游戏
        if (fg and fg["fullscreen"] and not fg["desktop"]
                and fg["pid"] in by_pid
                and by_pid[fg["pid"]]["name"].lower() not in NON_GAME_EXES):
            p = by_pid[fg["pid"]]
            existing = next((g for g in games if g["pid"] == p["pid"]), None)
            sig = ["全屏窗口"]
            if quns == QUNS_RUNNING_D3D_FULL_SCREEN:
                sig.append("全屏 3D")
            if quns == QUNS_PRESENTATION_MODE:
                sig.append("演示模式")
            if existing:
                for s in sig:
                    if s not in existing["signals"]:
                        existing["signals"].append(s)
                existing["foreground"] = True
                existing["has_window"] = True
                existing["lockable"] = True
            else:
                games.append({"pid": p["pid"], "name": p["name"],
                              "path": self._path_cache.get(p["pid"], ""),
                              "signals": sig, "foreground": True,
                              "has_window": True, "lockable": True})

        # 候选窗口列表（UI 用），附上识别结果
        seen = {}
        for w in win_rows:
            p = by_pid.get(w["pid"])
            if not p or w["pid"] in seen:
                continue
            path = self._path_cache.get(w["pid"], "")
            hit = self._whitelist_hit(p["name"], path, whitelist)
            match = next((g for g in games if g["pid"] == w["pid"]), None)
            seen[w["pid"]] = {
                "pid": w["pid"], "name": p["name"], "path": path,
                "title": w["title"][:80], "is_game": bool(match),
                "whitelisted": bool(hit),
                "signal": ", ".join(match["signals"]) if match else "",
            }
        windows = sorted(seen.values(), key=lambda r: (not r["is_game"], r["name"].lower()))[:40]

        with self.lock:
            self._games_now = games
            self._foreground = fg
            self._windows = windows
            self._scans += 1
            self._last_scan = time.time()

            # --- 状态机 -------------------------------------------------
            if not enabled or mode == "off":
                if self._paused or self._lock_pid:
                    self.log("游戏检测已关闭，恢复鼠标接管")
                self._paused = False
                self._lock_pid = None
                self._lock_info = None
                self._reason = "游戏检测已关闭"
                self._grace_until = 0.0
                return

            # 锁定目标还活着 -> 一路保持暂停（这就是「切出来查攻略」的答案）
            if self._lock_pid is not None and self._lock_pid in alive:
                target = next((g for g in games if g["pid"] == self._lock_pid), self._lock_info)
                if target:
                    self._lock_info = target
                self._paused = True
                self._grace_until = 0.0
                return

            if self._lock_pid is not None:
                # 游戏进程刚消失，进入宽限期
                if not self._grace_until:
                    self._grace_until = now + max(0.0, grace)
                    gone = (self._lock_info or {}).get("name", "游戏")
                    self.log(f"游戏已退出（{gone}），宽限 {grace:.0f} 秒后恢复接管")
                if now < self._grace_until:
                    self._paused = True
                    return
                self._lock_pid = None
                self._lock_info = None
                self._override_pid = None
                self._paused = False
                self._reason = ""
                self.log("已恢复手柄鼠标控制")

            lockable = [g for g in games if g.get("lockable")]
            if lockable:
                # 优先锁前台全屏、有窗口的游戏，其次白名单，最后其它游戏本体
                lockable.sort(key=lambda g: (not g["foreground"],
                                             not g.get("has_window"),
                                             "白名单" not in g["signals"], g["pid"]))
                pick = lockable[0]
                self._lock_pid = pick["pid"]
                self._lock_info = pick
                self._paused = True
                self._since = now
                self._grace_until = 0.0
                self._reason = "、".join(pick["signals"])
                self.log(f"检测到游戏 {pick['name']}（{self._reason}），已暂停鼠标接管")
                return

            self._paused = False
            self._reason = ""

    # ------------------------------------------------------------ 对外
    def is_paused(self) -> bool:
        cfg = self.cfg.get()
        if not cfg.get("game_guard", True) or cfg.get("game_mode", "strict") == "off":
            return False
        with self.lock:
            if self._override_until and time.monotonic() < self._override_until:
                return False
            if self._override_pid is not None and self._override_pid == self._lock_pid:
                return False
            if not self._paused:
                return False
            if cfg.get("game_mode", "strict") == "foreground":
                # 宽松模式：只有游戏自己还在前台时才暂停
                return bool(self._foreground and self._lock_pid
                            and self._foreground["pid"] == self._lock_pid)
            return True

    def brief(self) -> dict:
        """给 /api/state 用的轻量摘要（每 70ms 调用，不做窗口枚举）。"""
        cfg = self.cfg.get()
        with self.lock:
            info = self._lock_info or {}
            return {
                "enabled": bool(cfg.get("game_guard", True)),
                "mode": cfg.get("game_mode", "strict"),
                "locked": self._lock_pid is not None,
                "paused": self.is_paused(),
                "game": info.get("name", ""),
                "reason": self._reason,
                "override": bool(self._override_until > time.monotonic()
                                 or (self._override_pid is not None
                                     and self._override_pid == self._lock_pid)),
            }

    def snapshot(self) -> dict:
        cfg = self.cfg.get()
        with self.lock:
            paused = self._paused
            lock_pid = self._lock_pid
            info = dict(self._lock_info) if self._lock_info else None
            reason = self._reason
            since = self._since
            grace_left = max(0.0, self._grace_until - time.monotonic())
            override_left = max(0.0, self._override_until - time.monotonic())
            override_pid = self._override_pid
            games = [dict(g) for g in self._games_now]
            windows = [dict(w) for w in self._windows]
            fg = dict(self._foreground) if self._foreground else None
            scans = self._scans
            last_scan = self._last_scan
            err = self._last_error
        name = (info or {}).get("name", "")
        return {
            "available": self.win.ok,
            "enabled": bool(cfg.get("game_guard", True)),
            "mode": cfg.get("game_mode", "strict"),
            "whitelist": list(cfg.get("game_whitelist", []) or []),
            "grace": float(cfg.get("game_grace", 2.0) or 0),
            "locked": lock_pid is not None,
            "paused": self.is_paused(),
            "raw_paused": paused,
            "game": info,
            "game_name": name,
            "reason": reason,
            "duration": round(max(0.0, time.monotonic() - since), 1) if lock_pid else 0,
            "grace_left": round(grace_left, 1),
            "override": bool(override_left > 0 or (override_pid and override_pid == lock_pid)),
            "override_left": round(override_left, 1),
            "games": games,
            "windows": windows,
            "foreground": fg,
            "shell_state": self.win.shell_state() if self.win.ok else 0,
            "scans": scans,
            "last_scan": last_scan,
            "error": err,
        }

    def add_whitelist(self, entry: str) -> dict:
        entry = str(entry or "").strip().replace("/", "\\")
        if not entry:
            return {"ok": False, "message": "名称为空"}
        cfg = self.cfg.get()
        items = list(cfg.get("game_whitelist", []) or [])
        if any(str(x).lower() == entry.lower() for x in items):
            return {"ok": True, "message": "已在名单中"}
        items.append(entry[:200])
        self.cfg.update({"game_whitelist": items})
        self.log(f"已把 {entry} 加入游戏名单")
        self.rescan()
        return {"ok": True, "message": f"已添加 {entry}"}

    def remove_whitelist(self, entry: str) -> dict:
        cfg = self.cfg.get()
        items = [x for x in (cfg.get("game_whitelist", []) or [])
                 if str(x).lower() != str(entry).lower()]
        self.cfg.update({"game_whitelist": items})
        self.log(f"已把 {entry} 移出游戏名单")
        self.rescan()
        return {"ok": True, "message": f"已移除 {entry}"}

    def set_override(self, minutes: float = 0) -> dict:
        """临时恢复接管。minutes=0 表示本次游戏会话内一直恢复。"""
        with self.lock:
            if minutes and minutes > 0:
                self._override_until = time.monotonic() + minutes * 60
                self._override_pid = None
            else:
                self._override_until = 0.0
                self._override_pid = self._lock_pid
        self.log("已临时恢复手柄鼠标控制（本次游戏）" if not minutes
                 else f"已临时恢复 {minutes:g} 分钟")
        return {"ok": True, "message": "已临时恢复"}

    def clear_override(self) -> dict:
        with self.lock:
            self._override_until = 0.0
            self._override_pid = None
        self.log("已撤销临时恢复，恢复自动避让")
        return {"ok": True, "message": "已撤销"}

    def clear_lock(self) -> dict:
        """立刻解除当前游戏会话锁定（不改动白名单，下一轮扫描可能重新识别）。"""
        with self.lock:
            self._lock_pid = None
            self._lock_info = None
            self._paused = False
            self._grace_until = 0.0
            self._override_pid = None
            self._override_until = 0.0
            self._reason = ""
        self.log("已手动解除游戏会话锁定")
        return {"ok": True, "message": "已解除锁定"}
