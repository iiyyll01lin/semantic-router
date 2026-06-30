"""Central Control Plane (CCP) for the edge-fleet config PoC (stdlib only).

The CCP is the single place an operator edits router config; it versions and
signs the desired config and serves it to pull agents, and it keeps a central
audit log of what each edge box applied. It owns NO inbound connection to the
edge boxes: agents always pull (NAT/firewall friendly).

Endpoints (all under a shared bearer token except /healthz):
- GET  /healthz        -> liveness
- GET  /fleet/desired  -> the signed desired-config bundle (agents pull this)
- POST /fleet/desired  -> admin "edit once": set new config, bump version, re-sign
- GET  /fleet/status   -> fleet convergence view (for the demo)
- POST /fleet/status   -> an agent reports what it applied (appended to audit)
- GET  /fleet/audit    -> the central audit log

Run as a CLI daemon (reads env) or import ``make_server``/``CCPState`` for tests.
See docs/agent/plans/pl-0036-edge-fleet-config-control-plane.md.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from . import fleet_lib  # type: ignore
except Exception:  # pragma: no cover - module is normally run as a script
    import fleet_lib


class CCPState:
    """In-memory + on-disk desired config, fleet status, and audit log."""

    def __init__(self, signing_key: str, token: str, state_dir: str):
        self.signing_key = signing_key
        self.token = token
        self.state_dir = state_dir
        self.desired_dir = os.path.join(state_dir, "desired")
        self.audit_path = os.path.join(state_dir, "audit.log")
        os.makedirs(self.desired_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._version_num = 0
        self.version = ""
        self.config_text = ""
        self.boxes = {}   # box_id -> last reported status
        self.audit = []   # list of audit records

    def set_desired(self, config_text: str) -> str:
        """Store a new desired config, bump the version, persist it, return version."""
        with self._lock:
            self._version_num += 1
            self.version = "v%d" % self._version_num
            self.config_text = config_text
            with open(os.path.join(self.desired_dir, self.version + ".yaml"), "w",
                      encoding="utf-8") as fh:
                fh.write(config_text)
            return self.version

    def bundle(self) -> dict:
        with self._lock:
            return fleet_lib.build_bundle(self.signing_key, self.version, self.config_text)

    def record_status(self, payload: dict) -> None:
        """Append an agent status report to the audit log and fleet view."""
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "box_id": str(payload.get("box_id", "unknown")),
            "version": str(payload.get("version", "")),
            "hash": str(payload.get("hash", "")),
            "result": str(payload.get("result", "")),
            "reason": str(payload.get("reason", "")),
        }
        with self._lock:
            self.boxes[rec["box_id"]] = rec
            self.audit.append(rec)
            try:
                with open(self.audit_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
            except OSError:
                pass

    def status_view(self) -> dict:
        with self._lock:
            return {
                "desired_version": self.version,
                "boxes": dict(self.boxes),
                "audit_count": len(self.audit),
            }


def make_handler(state: CCPState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # silence default stderr logging
            return

        def _send(self, code: int, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            if not state.token:
                return True
            got = self.headers.get("Authorization", "")
            return got == "Bearer " + state.token

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                return None

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"status": "ok"})
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            if self.path == "/fleet/desired":
                if not state.version:
                    return self._send(404, {"error": "no desired config set yet"})
                return self._send(200, state.bundle())
            if self.path == "/fleet/status":
                return self._send(200, state.status_view())
            if self.path == "/fleet/audit":
                return self._send(200, {"audit": state.audit})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            payload = self._read_json()
            if payload is None:
                return self._send(400, {"error": "invalid JSON body"})
            if self.path == "/fleet/desired":
                config_text = payload.get("config")
                if not isinstance(config_text, str) or not config_text.strip():
                    return self._send(400, {"error": "missing 'config' string"})
                version = state.set_desired(config_text)
                return self._send(200, {"version": version,
                                        "sha256": fleet_lib.sha256_hex(config_text.encode("utf-8"))})
            if self.path == "/fleet/status":
                state.record_status(payload)
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

    return Handler


def make_server(host: str, port: int, state: CCPState) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(state))


def main() -> int:
    host = os.environ.get("CCP_HOST", "0.0.0.0")
    port = int(os.environ.get("CCP_PORT", "9300"))
    signing_key = os.environ.get("FLEET_SIGNING_KEY", "")
    token = os.environ.get("FLEET_TOKEN", "")
    state_dir = os.environ.get("CCP_STATE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ccp-state"))
    if not signing_key or not token:
        raise SystemExit("FLEET_SIGNING_KEY and FLEET_TOKEN must be set")

    state = CCPState(signing_key, token, state_dir)
    init_cfg = os.environ.get("CCP_INIT_CONFIG", "")
    if init_cfg and os.path.isfile(init_cfg):
        with open(init_cfg, "r", encoding="utf-8") as fh:
            state.set_desired(fh.read())

    server = make_server(host, port, state)
    print("CCP listening on %s:%d (desired_version=%s)" % (host, port, state.version or "none"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
