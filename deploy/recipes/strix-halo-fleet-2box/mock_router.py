"""Local mock of the router's per-node config API (stdlib only).

This stands in for a real semantic-router on a dev machine so the CCP + agent
fan-out logic can be verified end-to-end WITHOUT AMD/ROCm hardware. It models the
two primitives the agent relies on:

- ``GET /config/hash``   -> SHA256 of the active config file (the drift signal),
- a config FILE that the agent overwrites; the mock detects the change and bumps
  a reload counter WITHOUT changing its start time, i.e. it models fsnotify
  hot-reload (reload, not restart).
- ``GET /config/router`` -> the current active config text (for the demo).

The real router already ships these (see route_config_deploy.go and
server_config_watch.go); this mock only exists for the offline verify path.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from . import fleet_lib  # type: ignore
except Exception:  # pragma: no cover
    import fleet_lib


class RouterState:
    """Tracks the active config file plus reload/restart bookkeeping."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.start_time = time.time()   # never changes -> proves "no restart"
        self.reload_count = 0
        self._last_loaded = None
        self._lock = threading.Lock()
        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write("")
        self._refresh()

    def _refresh(self):
        """Read the file; if it changed since last load, count a hot-reload."""
        try:
            with open(self.config_path, "rb") as fh:
                data = fh.read()
        except OSError:
            data = b""
        with self._lock:
            if self._last_loaded is None:
                self._last_loaded = data
            elif data != self._last_loaded:
                self._last_loaded = data
                self.reload_count += 1
            return data

    def active_hash(self) -> str:
        return fleet_lib.sha256_hex(self._refresh())

    def active_text(self) -> str:
        return self._refresh().decode("utf-8", "replace")

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "reload_count": self.reload_count,
                "start_time": self.start_time,
                "uptime_s": round(time.time() - self.start_time, 3),
            }


def make_handler(state: RouterState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            return

        def _send(self, code: int, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"status": "ok"})
            if self.path == "/config/hash":
                return self._send(200, {"hash": state.active_hash()})
            if self.path == "/config/router":
                return self._send(200, {"config": state.active_text()})
            if self.path == "/debug/router-state":
                return self._send(200, state.snapshot())
            return self._send(404, {"error": "not found"})

    return Handler


def make_server(host: str, port: int, state: RouterState) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(state))


def main() -> int:
    host = os.environ.get("MOCK_ROUTER_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_ROUTER_PORT", "8080"))
    config_path = os.environ.get("MOCK_ROUTER_CONFIG", os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".mock-router-config.yaml"))
    state = RouterState(config_path)
    server = make_server(host, port, state)
    print("mock router on %s:%d (config=%s)" % (host, port, config_path))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
