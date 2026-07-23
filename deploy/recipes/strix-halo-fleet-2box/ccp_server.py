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
See docs/agent/plans/pl-0040-edge-fleet-config-control-plane.md.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import ssl
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from . import fleet_lib  # type: ignore
except Exception:  # pragma: no cover - module is normally run as a script
    import fleet_lib


_VERSION_FILE_RE = re.compile(r"^v(\d+)\.yaml$")


def _version_int(version):
    """Parse the integer from a ``vN`` version string; None if not parseable."""
    s = str(version).strip()
    if s[:1] == "v":
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        return None


def _prom_label(value: str) -> str:
    """Escape a Prometheus label value (backslash, double-quote, newline)."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class CCPState:
    """In-memory + on-disk desired config, fleet status, and audit log.

    Durable across restarts (R6): on startup it restores the latest persisted
    ``desired/<vN>.yaml`` and the version counter, so a CCP restart no longer
    forgets the desired config (it used to answer ``GET /fleet/desired`` with a
    404 until someone re-POSTed) and no longer resets the counter to 0 (which
    would re-issue v1, v2, ... and collide with the persisted history).

    The audit log is kept as a BOUNDED in-memory view (a ``deque``) backed by the
    append-only ``audit.log`` on disk: ``record_status`` used to append to an
    unbounded list forever (a slow memory leak on a long-running CCP). The full
    history still lives in ``audit.log``; ``audit_count`` reports the running
    total so the HTTP shape is unchanged.
    """

    def __init__(
        self,
        signing_key: str,
        token: str,
        state_dir: str,
        audit_memory_max: int = 1000,
        signer=None,
        bundle_ts: bool = False,
    ):
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
        self.boxes = {}  # box_id -> last reported status
        # Bounded in-memory audit view; full history is on disk in audit.log.
        self.audit = deque(maxlen=max(1, int(audit_memory_max)))
        self._audit_total = 0  # every record ever recorded (persisted count)
        # R9: apply-outcome counters ("box_id\x00result" -> count) for /metrics.
        self.outcomes = {}
        # R4: the bundle signer (HMAC by default; Ed25519 when configured). A bare
        # string key keeps the old behavior for direct constructors (e.g. tests).
        self.signer = (
            signer if signer is not None else fleet_lib.hmac_signer(signing_key)
        )
        self.bundle_ts = bool(bundle_ts)  # opt-in freshness timestamp in bundles
        self._restore()

    def _restore(self) -> None:
        """Reload the latest persisted desired config + version, and the tail of
        the audit log, so the CCP survives a restart without losing state."""
        latest_num, latest_name = 0, ""
        try:
            names = os.listdir(self.desired_dir)
        except OSError:
            names = []
        for name in names:
            m = _VERSION_FILE_RE.match(name)
            if m:
                num = int(m.group(1))
                if num > latest_num:
                    latest_num, latest_name = num, name
        if latest_name:
            try:
                with open(
                    os.path.join(self.desired_dir, latest_name), "r", encoding="utf-8"
                ) as fh:
                    self.config_text = fh.read()
                self._version_num = latest_num
                self.version = "v%d" % latest_num
            except OSError:
                pass
        # Replay the persisted audit log: recover the running total, the most
        # recent record per box (fleet status view), and the bounded tail.
        if os.path.isfile(self.audit_path):
            try:
                with open(self.audit_path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(rec, dict):
                            continue
                        self._audit_total += 1
                        self.audit.append(rec)
                        self.boxes[str(rec.get("box_id", "unknown"))] = rec
                        self._bump_outcome(
                            str(rec.get("box_id", "unknown")),
                            str(rec.get("result", "")),
                        )
            except OSError:
                pass

    def _bump_outcome(self, box_id: str, result: str) -> None:
        if not result:
            return
        key = box_id + "\x00" + result
        self.outcomes[key] = self.outcomes.get(key, 0) + 1

    def set_desired(self, config_text: str) -> str:
        """Store a new desired config, bump the version, persist it, return version."""
        with self._lock:
            self._version_num += 1
            self.version = "v%d" % self._version_num
            self.config_text = config_text
            with open(
                os.path.join(self.desired_dir, self.version + ".yaml"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write(config_text)
            return self.version

    def bundle(self) -> dict:
        with self._lock:
            ts = None
            if self.bundle_ts:
                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return fleet_lib.build_bundle(
                self.signer, self.version, self.config_text, ts=ts
            )

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
        if payload.get("loaded_hash") is not None:
            rec["loaded_hash"] = str(payload.get("loaded_hash", ""))
        # R9: keep the agent's write->converge timer when present (used by
        # /metrics and by fleet_metrics.py to compute p50/p95 hot-reload latency).
        if payload.get("apply_seconds") is not None:
            try:
                rec["apply_seconds"] = float(payload["apply_seconds"])
            except (TypeError, ValueError):
                pass
        with self._lock:
            self.boxes[rec["box_id"]] = rec
            self.audit.append(rec)
            self._audit_total += 1
            self._bump_outcome(rec["box_id"], rec["result"])
            try:
                with open(self.audit_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
            except OSError:
                pass

    def audit_view(self) -> list:
        """Bounded, JSON-serializable snapshot of the recent audit records."""
        with self._lock:
            return list(self.audit)

    def status_view(self) -> dict:
        with self._lock:
            return {
                "desired_version": self.version,
                "boxes": dict(self.boxes),
                "audit_count": self._audit_total,
            }

    def metrics_text(self) -> str:
        """Render a Prometheus text-exposition of fleet convergence + outcomes."""
        with self._lock:
            desired_n = _version_int(self.version)
            boxes = dict(self.boxes)
            outcomes = dict(self.outcomes)
            audit_total = self._audit_total
        lines = [
            "# HELP fleet_desired_version_number CCP desired config version (integer).",
            "# TYPE fleet_desired_version_number gauge",
            "fleet_desired_version_number %d" % (desired_n or 0),
            "# HELP fleet_audit_records_total Total status records recorded by the CCP.",
            "# TYPE fleet_audit_records_total counter",
            "fleet_audit_records_total %d" % audit_total,
            "# HELP fleet_boxes Number of edge boxes that have reported at least once.",
            "# TYPE fleet_boxes gauge",
            "fleet_boxes %d" % len(boxes),
            "# HELP fleet_box_version_lag Desired version minus the box's last-applied version.",
            "# TYPE fleet_box_version_lag gauge",
            "# HELP fleet_box_last_apply_seconds Last write->converge time the box reported (s).",
            "# TYPE fleet_box_last_apply_seconds gauge",
        ]
        for box_id, rec in sorted(boxes.items()):
            label = _prom_label(box_id)
            box_n = _version_int(rec.get("version", ""))
            lag = (
                (desired_n - box_n)
                if (desired_n is not None and box_n is not None)
                else 0
            )
            lines.append('fleet_box_version_lag{box_id="%s"} %d' % (label, lag))
            if rec.get("apply_seconds") is not None:
                lines.append(
                    'fleet_box_last_apply_seconds{box_id="%s"} %s'
                    % (label, rec["apply_seconds"])
                )
        lines.append(
            "# HELP fleet_apply_outcomes_total Agent-reported apply outcomes by result."
        )
        lines.append("# TYPE fleet_apply_outcomes_total counter")
        for key in sorted(outcomes):
            box_id, _sep, result = key.partition("\x00")
            lines.append(
                'fleet_apply_outcomes_total{box_id="%s",result="%s"} %d'
                % (_prom_label(box_id), _prom_label(result), outcomes[key])
            )
        return "\n".join(lines) + "\n"


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

        def _send_text(
            self,
            code: int,
            text: str,
            content_type: str = "text/plain; version=0.0.4; charset=utf-8",
        ):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            if not state.token:
                return True
            got = self.headers.get("Authorization", "")
            # Constant-time compare so the bearer token is not leaked through a
            # response-timing side channel (R4).
            return hmac.compare_digest(got, "Bearer " + state.token)

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
            if self.path == "/metrics":
                # Prometheus scrape (R9). Behind the bearer token like the other
                # admin endpoints; a scraper sends Authorization: Bearer <token>.
                return self._send_text(200, state.metrics_text())
            if self.path == "/fleet/desired":
                if not state.version:
                    return self._send(404, {"error": "no desired config set yet"})
                return self._send(200, state.bundle())
            if self.path == "/fleet/status":
                return self._send(200, state.status_view())
            if self.path == "/fleet/audit":
                return self._send(200, {"audit": state.audit_view()})
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
                return self._send(
                    200,
                    {
                        "version": version,
                        "sha256": fleet_lib.sha256_hex(config_text.encode("utf-8")),
                    },
                )
            if self.path == "/fleet/status":
                state.record_status(payload)
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

    return Handler


def make_server(host: str, port: int, state: CCPState) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(state))


def _maybe_wrap_tls(server) -> str:
    """Wrap the server socket in TLS when CCP_TLS_CERT/CCP_TLS_KEY are set (R5).

    Returns the scheme ("https" or "http"). Optional client-cert auth (mTLS) is
    enabled when CCP_TLS_CLIENT_CA is set. Absent TLS env, plain HTTP is kept.
    """
    cert = os.environ.get("CCP_TLS_CERT", "").strip()
    key = os.environ.get("CCP_TLS_KEY", "").strip()
    if not (cert and key):
        return "http"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    client_ca = os.environ.get("CCP_TLS_CLIENT_CA", "").strip()
    if client_ca:
        ctx.load_verify_locations(client_ca)
        ctx.verify_mode = ssl.CERT_REQUIRED
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return "https"


def main() -> int:
    # Default bind is loopback (R5): admin endpoints like POST /fleet/desired must
    # not be exposed on every interface by accident. A multi-box deploy explicitly
    # sets CCP_HOST to a reachable interface (see ccp-bring-up.sh) and should add
    # TLS + a strong token when doing so.
    host = os.environ.get("CCP_HOST", "127.0.0.1") or "127.0.0.1"
    port = int(os.environ.get("CCP_PORT") or "9300")
    signing_key = os.environ.get("FLEET_SIGNING_KEY", "")
    token = os.environ.get("FLEET_TOKEN", "")
    state_dir = os.environ.get(
        "CCP_STATE_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ccp-state"),
    )
    audit_memory_max = int(os.environ.get("CCP_AUDIT_MEMORY_MAX") or "1000")
    sign_mode = os.environ.get("FLEET_SIGN_MODE", fleet_lib.SIGN_HMAC).strip().lower()
    bundle_ts = os.environ.get("FLEET_BUNDLE_TS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not token:
        raise SystemExit("FLEET_TOKEN must be set")
    if sign_mode != fleet_lib.SIGN_ED25519 and not signing_key:
        raise SystemExit("FLEET_SIGNING_KEY and FLEET_TOKEN must be set")
    try:
        signer = fleet_lib.signer_from_env()  # HMAC by default; Ed25519 if configured
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc

    state = CCPState(
        signing_key,
        token,
        state_dir,
        audit_memory_max=audit_memory_max,
        signer=signer,
        bundle_ts=bundle_ts,
    )
    # Seed from CCP_INIT_CONFIG only when nothing was restored from disk, so a
    # restart keeps the operator's desired config + version instead of re-issuing
    # v1 from the init file on every boot (R6 durability).
    init_cfg = os.environ.get("CCP_INIT_CONFIG", "")
    if not state.version and init_cfg and os.path.isfile(init_cfg):
        with open(init_cfg, "r", encoding="utf-8") as fh:
            state.set_desired(fh.read())

    server = make_server(host, port, state)
    scheme = _maybe_wrap_tls(server)
    print(
        "CCP listening on %s://%s:%d (desired_version=%s)"
        % (scheme, host, port, state.version or "none")
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
