"""本地控制面板 HTTP 服务（只监听 127.0.0.1）。"""
from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

WEB_DIR = Path(__file__).resolve().parent / "web"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "GamepadMouse/1.0"
    engine = None
    probe = None
    guard = None
    on_quit = None

    def log_message(self, fmt, *args):
        pass  # 静音，避免刷屏

    # ---------------------------------------------------------------- 安全
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1_000_000:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, OSError):
            return {}

    # ---------------------------------------------------------------- 路由
    def do_GET(self):
        if not self._host_ok():
            self._json({"error": "forbidden host"}, 403)
            return
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(self.engine.state())
            return
        if path == "/api/health":
            self._json({"ok": True})
            return
        if path == "/api/probe":
            self._json(self.probe.snapshot() if self.probe else {"error": "probe unavailable"})
            return
        if path == "/api/games":
            self._json(self.guard.snapshot() if self.guard else {"error": "guard unavailable"})
            return
        if path == "/test":
            path = "/test.html"
        if path == "/":
            path = "/index.html"
        # 静态文件防目录穿越
        rel = path.lstrip("/").replace("\\", "/")
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(target.suffix.lower(),
                         mimetypes.guess_type(target.name)[0] or "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self._host_ok():
            self._json({"error": "forbidden host"}, 403)
            return
        path = urlparse(self.path).path
        payload = self._read_json()
        if path == "/api/config":
            self._json(self.engine.command("config", {"patch": payload.get("patch") or payload}))
            return
        if path == "/api/cmd":
            cmd = payload.pop("cmd", "")
            if cmd == "quit":
                self._json({"ok": True})
                if self.on_quit:
                    self.on_quit()
                return
            self._json(self.engine.command(cmd, payload))
            return
        if path == "/api/probe/init":
            if not self.probe:
                self._json({"error": "probe unavailable"}, 503)
                return
            self.probe.initialize_async()
            self._json({"ok": True, "message": "初始化命令已开始发送"})
            return
        if path == "/api/probe/clear":
            if self.probe:
                self.probe.clear()
            self._json({"ok": True})
            return
        if path == "/api/probe/recenter":
            if not self.probe:
                self._json({"error": "probe unavailable"}, 503)
                return
            self._json(self.probe.recenter())
            return
        if path == "/api/games":
            if not self.guard:
                self._json({"error": "guard unavailable"}, 503)
                return
            action = payload.get("action", "")
            value = payload.get("value")
            table = {
                "add": lambda: self.guard.add_whitelist(value or ""),
                "remove": lambda: self.guard.remove_whitelist(value or ""),
                "override": lambda: self.guard.set_override(float(value or 0)),
                "clear_override": lambda: self.guard.clear_override(),
                "clear_lock": lambda: self.guard.clear_lock(),
                "rescan": lambda: (self.guard.rescan(), {"ok": True})[1],
            }
            func = table.get(action)
            if not func:
                self._json({"error": "unknown action", "action": action}, 400)
                return
            try:
                result = func()
                result["snapshot"] = self.guard.snapshot()
                self._json(result)
            except Exception as exc:
                self._json({"ok": False, "message": str(exc)}, 500)
            return
        self._json({"error": "not found"}, 404)


def make_server(engine, port: int = 0, on_quit=None, probe=None,
                guard=None) -> tuple[ThreadingHTTPServer, int]:
    Handler.engine = engine
    Handler.probe = probe
    Handler.guard = guard
    Handler.on_quit = on_quit
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd, httpd.server_address[1]
