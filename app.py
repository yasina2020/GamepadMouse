"""手柄当鼠标用 · 应用入口。

启动本地服务 + 后台手柄轮询，并以「独立应用窗口」模式打开控制面板。
"""
from __future__ import annotations

import ctypes
import json
import os
import struct
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

RUNTIME = BASE / "runtime.json"
ERROR_LOG = BASE / "error.log"
MUTEX_NAME = "Local\\GamepadMouse_Singleton_8f31c"


def message_box(text: str, title: str = "手柄当鼠标用"):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 | 0x1000)
    except Exception:
        print(text)


def find_browser() -> str | None:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def open_panel(url: str):
    exe = find_browser()
    if exe:
        try:
            subprocess.Popen(
                [exe, f"--app={url}", "--window-size=1220,940",
                 "--window-position=120,60", "--no-first-run",
                 "--disable-features=msEdgeWelcomePage,msWelcomePage"],
                close_fds=True,
            )
            return
        except OSError:
            pass
    webbrowser.open(url)


def already_running() -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    err = ctypes.get_last_error()
    return bool(handle) and err == 183  # ERROR_ALREADY_EXISTS


def main():
    if struct.calcsize("P") != 8:
        message_box("需要 64 位 Python 才能加载随附的 SDL2.dll。")
        return 1

    if already_running():
        try:
            info = json.loads(RUNTIME.read_text(encoding="utf-8"))
            page = "test" if "--test" in sys.argv else ""
            open_panel(f"http://127.0.0.1:{info['port']}/{page}")
        except Exception:
            message_box("应用已经在运行了。若找不到窗口，请查看任务栏或重新打开控制面板。")
        return 0

    no_browser = "--no-browser" in sys.argv
    port = 8765
    for arg in sys.argv:
        if arg.startswith("--port="):
            try:
                port = int(arg.split("=", 1)[1])
            except ValueError:
                pass

    from core.engine import Engine
    from core.ns2 import NS2Probe
    from core.gameguard import GameGuard
    from server import make_server

    engine = Engine(BASE / "config.json")
    probe = NS2Probe()
    engine.ns2 = probe
    guard = GameGuard(engine.cfg, log=engine._log)
    engine.gameguard = guard
    stop_event = threading.Event()

    httpd, actual_port = make_server(engine, port, on_quit=stop_event.set,
                                     probe=probe, guard=guard)
    url = f"http://127.0.0.1:{actual_port}/"
    try:
        RUNTIME.write_text(json.dumps({"port": actual_port, "pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass

    engine.start()
    probe.start()
    guard.start()
    # NS2 Pro 上电后默认沉默，尝试唤醒；若已在输出则会自动跳过
    threading.Timer(0.6, probe.initialize_async).start()
    thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    thread.start()
    page = url + ("test" if "--test" in sys.argv else "")
    print(f"控制面板：{url}")
    print(f"手柄测试页：{url}test")
    print("按 Ctrl+C 或点击界面中的「退出应用」结束。")

    if not no_browser:
        open_panel(page)

    try:
        while not stop_event.wait(0.25):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        print("正在退出…")
        httpd.shutdown()
        engine.stop()
        probe.stop()
        guard.stop()
        try:
            RUNTIME.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        tb = traceback.format_exc()
        try:
            ERROR_LOG.write_text(tb, encoding="utf-8")
        except OSError:
            pass
        print(tb)
        message_box(f"启动失败，详细信息已写入：\n{ERROR_LOG}\n\n{tb[-600:]}")
        sys.exit(1)
