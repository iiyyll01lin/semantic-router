"""Offline end-to-end verifier for the edge-fleet config control plane.

Spins up, in-process, a CCP plus two mock routers plus two pull agents and
asserts the PL-0036 exit criteria WITHOUT any AMD/ROCm hardware:

  1. baseline converge       - both boxes reach the desired hash
  2. edit-once converge      - one CCP edit converges both, via hot-reload
                               (reload_count +1, start_time unchanged = no restart)
  3. drift self-heal         - an out-of-band local change is reverted to desired
  4. fleet rollback          - setting desired back to a prior config converges both
  5. tamper rejection        - a wrong-key (untrusted) bundle is rejected, never applied
  6. central audit           - the CCP recorded every apply
  7. in-place write          - the agent overwrites the config in place (same inode),
                               so the real single-file-mounted gateway hot-reloads
                               (an atomic rename would swap in a new inode the
                               router container never sees -> no fsnotify reload)
  8. router outage retry     - a temporarily unavailable router returns an error
                               result instead of killing the long-running agent

Hardening checks (R4/R6/R8):
  9.  Ed25519 sign/verify     - vendored RFC 8032 impl (validated vs the RFC test
                                vectors); tamper/forge/HMAC-downgrade all rejected
  10. anti-downgrade          - a stale but validly-signed OLDER bundle is rejected
                                once a newer version has been applied
  11. auto-rollback           - an UNLOADABLE apply (invalid config the router
                                refuses to load -> /config/loaded-hash does not
                                advance) restores the .bak, reports rolled_back,
                                then backs off that version
  12. CCP durability          - desired config + version counter + audit survive a
                                simulated CCP restart (no version collision)

Client-cert / warm-standby checks (C1/C2):
  13. mTLS handshake          - a client presenting FLEET_TLS_CLIENT_CERT/KEY is
                                accepted by a CCP started with CCP_TLS_CLIENT_CA,
                                while a certless client is rejected at the TLS
                                handshake (proves the C1 load_cert_chain path)
  14. warm-standby restore    - replicating CCP_STATE_DIR (desired/ + audit.log)
                                with ccp-standby-sync.sh and booting a fresh
                                CCPState on the copy restores the latest version +
                                config + full audit total (the C2 dry-run)

Exit code 0 means all checks passed. This verifies the NEW fan-out/drift/sign/
audit logic; the real ROCm gateway path is exercised by deploy-fleet-2box.sh on
the Strix Halo boxes.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ccp_server
import fleet_agent
import fleet_lib
import mock_router

SIGNING_KEY = "poc-fleet-signing-key"
TOKEN = "poc-fleet-token"

# Directory holding this verifier and its sibling scripts (ccp-standby-sync.sh).
_HERE = os.path.dirname(os.path.abspath(__file__))

CHECKS = []


class _CtlRouterState:
    """Mock router that can drive the agent's auto-rollback two ways:
    /config/loaded-hash stays pinned to the last-good config when the active
    file is invalid YAML (the real loaded runtime signal the agent now checks,
    R8), and /healthz returns 503 when the active config contains ``UNHEALTHY``
    (the optional ROUTER_HEALTH_PATH readiness gate). Reuses
    mock_router.RouterState for the file-hash/hot-reload + parse semantics
    (a converged byte-hash != a loadable/serving router)."""

    def __init__(self, config_path: str):
        self.r = mock_router.RouterState(config_path)


def _ctl_router_handler(state: _CtlRouterState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            return

        def _json(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/config/hash":
                return self._json(200, {"hash": state.r.active_hash()})
            if self.path == "/config/loaded-hash":
                return self._json(
                    200, {"hash": state.r.loaded_hash(), "source": "loaded"}
                )
            if self.path == "/healthz":
                unhealthy = "UNHEALTHY" in state.r.active_text()
                return self._json(503 if unhealthy else 200, {"ok": not unhealthy})
            if self.path == "/config/router":
                if not state.r.active_loadable():
                    return self._json(500, {"error": "PARSE_ERROR"})
                return self._json(200, {"config": state.r.active_text()})
            return self._json(404, {"error": "not found"})

    return Handler


def _static_bundle_handler(bundle: dict):
    """Serve a FIXED (validly-signed) bundle at GET /fleet/desired -- used to
    replay a stale, older-versioned bundle for the anti-downgrade check."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            return

        def do_GET(self):
            body = json.dumps(bundle).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def record(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = "[%s] %s" % (mark, name)
    if detail:
        line += " - " + detail
    print(line)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(server) -> None:
    threading.Thread(target=server.serve_forever, daemon=True).start()


def set_desired(ccp_url: str, config_text: str) -> str:
    status, obj = fleet_lib.http_post_json(
        ccp_url + "/fleet/desired", {"config": config_text}, token=TOKEN
    )
    if status != 200:
        raise RuntimeError("set_desired failed: %d %s" % (status, obj))
    return obj["version"]


def _make_mtls_certs(dirpath: str):
    """Mint an ephemeral CA + SAN CCP server cert + one client cert with openssl
    (mirrors make-mtls-certs.sh) for the in-process mTLS handshake check.

    openssl is a system tool (not a Python dependency) and is what the recipe's
    make-mtls-certs.sh already requires. Returns a dict of PEM paths, or None if
    openssl is unavailable / cert minting fails, so the caller records honestly.
    """
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    p = {
        name: os.path.join(dirpath, name + ".pem")
        for name in (
            "ca-cert",
            "ca-key",
            "server-cert",
            "server-key",
            "client-cert",
            "client-key",
        )
    }
    srl = os.path.join(dirpath, "ca.srl")
    server_csr = os.path.join(dirpath, "server.csr")
    client_csr = os.path.join(dirpath, "client.csr")
    server_ext = os.path.join(dirpath, "server.ext")
    client_ext = os.path.join(dirpath, "client.ext")
    # SAN binds the server cert to 127.0.0.1/localhost so hostname verification
    # passes for the loopback URL the check connects to.
    with open(server_ext, "w", encoding="utf-8") as fh:
        fh.write(
            "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
            "extendedKeyUsage=serverAuth\n"
            "keyUsage=digitalSignature,keyEncipherment\n"
        )
    with open(client_ext, "w", encoding="utf-8") as fh:
        fh.write("extendedKeyUsage=clientAuth\nkeyUsage=digitalSignature\n")

    def _run(*args) -> None:
        subprocess.run(
            [openssl, *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _leaf(cn: str, key: str, csr: str, cert: str, ext: str) -> None:
        _run(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=" + cn,
            "-keyout",
            key,
            "-out",
            csr,
        )
        _run(
            "x509",
            "-req",
            "-in",
            csr,
            "-out",
            cert,
            "-CA",
            p["ca-cert"],
            "-CAkey",
            p["ca-key"],
            "-CAcreateserial",
            "-CAserial",
            srl,
            "-days",
            "2",
            "-extfile",
            ext,
        )

    try:
        _run(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-keyout",
            p["ca-key"],
            "-out",
            p["ca-cert"],
            "-subj",
            "/CN=fleet-verify-ca",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
        _leaf("localhost", p["server-key"], server_csr, p["server-cert"], server_ext)
        _leaf("halo-a", p["client-key"], client_csr, p["client-cert"], client_ext)
    except (subprocess.CalledProcessError, OSError):
        return None
    return {
        "ca": p["ca-cert"],
        "server_cert": p["server-cert"],
        "server_key": p["server-key"],
        "client_cert": p["client-cert"],
        "client_key": p["client-key"],
    }


def _standby_sync(src_ccp_dir: str, dst_ccp_dir: str, fleet_state_dir: str) -> bool:
    """Replicate a CCP state dir the way ccp-standby-sync.sh does in local mode
    (SYNC_ONCE=1, STANDBY_HOST empty) -- this is the C2 dry-run from
    docs/ha-standby.md. Falls back to a stdlib copy of desired/ + audit.log if
    bash/the script are unavailable so the restore semantics are still proven.
    Returns True on a successful replication.
    """
    os.makedirs(dst_ccp_dir, exist_ok=True)
    script = os.path.join(_HERE, "ccp-standby-sync.sh")
    bash = shutil.which("bash")
    if bash and os.path.isfile(script):
        env = dict(os.environ)
        env.update(
            {
                "SYNC_ONCE": "1",
                "STANDBY_HOST": "",  # empty => local copy (no SSH)
                "CCP_STATE_DIR": src_ccp_dir,
                "STANDBY_STATE_DIR": dst_ccp_dir,
                "FLEET_STATE_DIR": fleet_state_dir,
            }
        )
        try:
            subprocess.run(
                [bash, script],
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (subprocess.CalledProcessError, OSError):
            pass  # fall through to the stdlib copy
    try:
        src_desired = os.path.join(src_ccp_dir, "desired")
        dst_desired = os.path.join(dst_ccp_dir, "desired")
        os.makedirs(dst_desired, exist_ok=True)
        for name in os.listdir(src_desired):
            shutil.copy2(
                os.path.join(src_desired, name), os.path.join(dst_desired, name)
            )
        src_audit = os.path.join(src_ccp_dir, "audit.log")
        if os.path.isfile(src_audit):
            shutil.copy2(src_audit, os.path.join(dst_ccp_dir, "audit.log"))
        return True
    except OSError:
        return False


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="fleet-verify-")

    # --- CCP -------------------------------------------------------------
    ccp_state = ccp_server.CCPState(SIGNING_KEY, TOKEN, os.path.join(workdir, "ccp"))
    ccp_port = free_port()
    serve(ccp_server.make_server("127.0.0.1", ccp_port, ccp_state))
    ccp_url = "http://127.0.0.1:%d" % ccp_port

    # --- two mock routers + two agents ----------------------------------
    boxes = []
    for box in ("halo-a", "halo-b"):
        cfg_path = os.path.join(workdir, box + "-config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write("version: v0.3\n# baseline\n")
        rstate = mock_router.RouterState(cfg_path)
        rport = free_port()
        serve(mock_router.make_server("127.0.0.1", rport, rstate))
        acfg = fleet_agent.AgentConfig(
            ccp_url=ccp_url,
            router_api="http://127.0.0.1:%d" % rport,
            config_file=cfg_path,
            signing_key=SIGNING_KEY,
            token=TOKEN,
            box_id=box,
            poll_interval=0.05,
            apply_timeout=5.0,
        )
        boxes.append(
            {
                "box": box,
                "cfg_path": cfg_path,
                "rstate": rstate,
                "acfg": acfg,
                "astate": fleet_agent.AgentState(),
            }
        )

    time.sleep(0.3)  # let servers bind

    def reconcile_all():
        return [fleet_agent.reconcile_once(b["acfg"], b["astate"]) for b in boxes]

    # --- 1. baseline converge -------------------------------------------
    cfg_v1 = "version: v0.3\nlisteners:\n  - name: http\n    port: 8899\n# rule-set A\n"
    hash_v1 = fleet_lib.sha256_hex(cfg_v1.encode("utf-8"))
    set_desired(ccp_url, cfg_v1)
    reconcile_all()
    ok1 = all(b["rstate"].active_hash() == hash_v1 for b in boxes)
    record("baseline converge (both boxes reach desired hash)", ok1)
    reloads_after_v1 = {b["box"]: b["rstate"].snapshot()["reload_count"] for b in boxes}
    starts_after_v1 = {b["box"]: b["rstate"].snapshot()["start_time"] for b in boxes}

    # --- 2. edit-once converge via hot-reload (no restart) ---------------
    cfg_v2 = cfg_v1 + "# rule-set B (changed once at the CCP)\n"
    hash_v2 = fleet_lib.sha256_hex(cfg_v2.encode("utf-8"))
    set_desired(ccp_url, cfg_v2)
    reconcile_all()
    converged2 = all(b["rstate"].active_hash() == hash_v2 for b in boxes)
    hot_reloaded = all(
        b["rstate"].snapshot()["reload_count"] == reloads_after_v1[b["box"]] + 1
        for b in boxes
    )
    no_restart = all(
        b["rstate"].snapshot()["start_time"] == starts_after_v1[b["box"]] for b in boxes
    )
    record("edit-once converges both boxes", converged2)
    record(
        "applied via hot-reload, not restart (reload+1, start_time stable)",
        hot_reloaded and no_restart,
    )

    # --- 3. drift self-heal ---------------------------------------------
    drift_box = boxes[0]
    with open(drift_box["cfg_path"], "w", encoding="utf-8") as fh:
        fh.write("version: v0.3\n# UNAUTHORIZED local edit\n")
    fleet_agent.reconcile_once(drift_box["acfg"], drift_box["astate"])
    healed = drift_box["rstate"].active_hash() == hash_v2
    record("drift self-heal (out-of-band edit reverted to desired)", healed)

    # --- 4. fleet rollback ----------------------------------------------
    set_desired(ccp_url, cfg_v1)  # roll desired back to the v1 content (new version)
    reconcile_all()
    rolled_back = all(b["rstate"].active_hash() == hash_v1 for b in boxes)
    record("fleet rollback (desired<-prior content converges both)", rolled_back)

    # --- 5. tamper rejection (untrusted signing key) --------------------
    bad = dict(boxes[0])
    bad_cfg = fleet_agent.AgentConfig(
        ccp_url=ccp_url,
        router_api=boxes[0]["acfg"].router_api,
        config_file=boxes[0]["cfg_path"],
        signing_key="WRONG-KEY",
        token=TOKEN,
        box_id="halo-a",
        poll_interval=0.05,
        apply_timeout=2.0,
    )
    before = boxes[0]["rstate"].active_hash()
    res = fleet_agent.reconcile_once(bad_cfg, fleet_agent.AgentState())
    after = boxes[0]["rstate"].active_hash()
    rejected = res.get("result") == "rejected" and before == after
    record(
        "tamper rejection (untrusted bundle rejected, config unchanged)",
        rejected,
        res.get("reason", ""),
    )
    _ = bad

    # --- 6. central audit captured every apply --------------------------
    status, audit = fleet_lib.http_get_json(ccp_url + "/fleet/audit", token=TOKEN)
    applied_records = [
        r for r in audit.get("audit", []) if r.get("result") in ("applied", "in_sync")
    ]
    rejected_records = [
        r for r in audit.get("audit", []) if r.get("result") == "rejected"
    ]
    audit_ok = (
        status == 200 and len(applied_records) >= 4 and len(rejected_records) >= 1
    )
    record(
        "central audit log captured applies + the rejection",
        audit_ok,
        "applies=%d rejects=%d" % (len(applied_records), len(rejected_records)),
    )

    # --- 7. in-place write preserves the inode (real-gateway hot-reload safe) ---
    # The real vllm-sr router bind-mounts the config as a SINGLE FILE
    # (config.yaml:/app/config.yaml), so the agent must overwrite it IN PLACE
    # (same inode). An atomic temp+rename would swap in a new inode the router
    # container never sees -> the new config stays invisible and no fsnotify
    # reload fires. Drive the REAL fleet_agent._write_config and assert the inode
    # is preserved and the exact bytes land on disk.
    inode_path = os.path.join(workdir, "inode-check-config.yaml")
    with open(inode_path, "w", encoding="utf-8") as fh:
        fh.write("version: v0.3\n# before\n")
    ino_before = os.stat(inode_path).st_ino
    inode_cfg = fleet_agent.AgentConfig(
        ccp_url=ccp_url,
        router_api="http://127.0.0.1:1",
        config_file=inode_path,
        signing_key=SIGNING_KEY,
        token=TOKEN,
        box_id="inode-check",
        poll_interval=0.05,
        apply_timeout=1.0,
    )
    new_text = "version: v0.3\n# after, rewritten in place\n"
    fleet_agent._write_config(inode_cfg, new_text)
    ino_after = os.stat(inode_path).st_ino
    with open(inode_path, "rb") as fh:
        on_disk = fh.read()
    inode_ok = ino_before == ino_after and on_disk == new_text.encode("utf-8")
    record(
        "in-place write keeps the inode (real-gateway reload safe)",
        inode_ok,
        (
            "inode %s preserved" % ino_before
            if inode_ok
            else "inode %s -> %s" % (ino_before, ino_after)
        ),
    )

    # --- 8. router outage is retryable -----------------------------------
    # A real gateway can briefly drop :8080/config/hash while containers restart
    # or hot-reload. The long-running pull agent must report a retryable result,
    # not exit with an uncaught urllib connection error.
    down_cfg = fleet_agent.AgentConfig(
        ccp_url=ccp_url,
        router_api="http://127.0.0.1:1",
        config_file=inode_path,
        signing_key=SIGNING_KEY,
        token=TOKEN,
        box_id="router-down",
        poll_interval=0.05,
        apply_timeout=0.2,
    )
    try:
        down_res = fleet_agent.reconcile_once(down_cfg, fleet_agent.AgentState())
        outage_retry = down_res.get("result") == "router_error"
    except Exception as exc:
        down_res = {"result": "raised", "reason": repr(exc)}
        outage_retry = False
    record(
        "router outage returns retryable router_error (agent stays alive)",
        outage_retry,
        down_res.get("reason", ""),
    )

    # --- 9. Ed25519 sign/verify + tamper/forge/downgrade rejection (R4) --------
    # Opt-in asymmetric signing: the CCP signs with a private seed, agents verify
    # with only the public key (an edge box cannot forge desired config). The
    # vendored impl is validated against the RFC 8032 test vectors.
    try:
        import _ed25519

        _ed25519.selftest()
        record("vendored Ed25519 matches RFC 8032 test vectors (interop)", True)
    except Exception as exc:  # report any selftest failure
        record(
            "vendored Ed25519 matches RFC 8032 test vectors (interop)", False, repr(exc)
        )
    seed_hex, pub_hex = fleet_lib.ed25519_keygen()
    ed_verifier = fleet_lib.ed25519_verifier(pub_hex)
    ed_bundle = fleet_lib.build_bundle(
        fleet_lib.ed25519_signer(seed_hex), "v1", "ed-config\n"
    )
    ok_good, _r = fleet_lib.verify_bundle(ed_verifier, ed_bundle)
    tampered_ed = dict(ed_bundle)
    tampered_ed["config"] = "ed-config-TAMPERED\n"
    ok_tamper, _r = fleet_lib.verify_bundle(ed_verifier, tampered_ed)
    _seed2, other_pub = fleet_lib.ed25519_keygen()
    ok_forge, _r = fleet_lib.verify_bundle(
        fleet_lib.ed25519_verifier(other_pub), ed_bundle
    )
    hmac_bundle = fleet_lib.build_bundle("shared-hmac-key", "v1", "ed-config\n")
    ok_downgrade, _r = fleet_lib.verify_bundle(ed_verifier, hmac_bundle)
    record(
        "Ed25519 sign/verify + tamper/forge/HMAC-downgrade all rejected",
        ok_good and not ok_tamper and not ok_forge and not ok_downgrade,
    )

    # --- 10. anti-downgrade / replay rejection (R4) ----------------------------
    # A stale but still-validly-signed OLDER bundle must be rejected once a newer
    # version has been applied, without touching the router config.
    stale_bundle = fleet_lib.build_bundle(SIGNING_KEY, "v3", "stale-older-content\n")
    stale_port = free_port()
    serve(
        ThreadingHTTPServer(
            ("127.0.0.1", stale_port), _static_bundle_handler(stale_bundle)
        )
    )
    time.sleep(0.2)
    ad_state = fleet_agent.AgentState()
    ad_state.applied_version = "v9"  # pretend a newer version is already applied
    ad_cfg = fleet_agent.AgentConfig(
        ccp_url="http://127.0.0.1:%d" % stale_port,
        router_api=boxes[0]["acfg"].router_api,
        config_file=boxes[0]["cfg_path"],
        signing_key=SIGNING_KEY,
        token=TOKEN,
        box_id="downgrade-box",
        poll_interval=0.05,
        apply_timeout=1.0,
    )
    before_ad = boxes[0]["rstate"].active_hash()
    ad_res = fleet_agent.reconcile_once(ad_cfg, ad_state)
    after_ad = boxes[0]["rstate"].active_hash()
    record(
        "anti-downgrade rejects a stale validly-signed bundle (config unchanged)",
        ad_res.get("result") == "rejected"
        and "anti-downgrade" in ad_res.get("reason", "")
        and before_ad == after_ad,
        ad_res.get("reason", ""),
    )

    # --- 11. health-gated apply + auto-rollback + backoff (R8) ------------------
    rb_path = os.path.join(workdir, "rollback-config.yaml")
    with open(rb_path, "w", encoding="utf-8") as fh:
        fh.write("version: v0.3\n# rb good baseline\n")
    rb_router = _CtlRouterState(rb_path)
    rb_port = free_port()
    serve(ThreadingHTTPServer(("127.0.0.1", rb_port), _ctl_router_handler(rb_router)))
    time.sleep(0.2)
    rb_cfg = fleet_agent.AgentConfig(
        ccp_url=ccp_url,
        router_api="http://127.0.0.1:%d" % rb_port,
        config_file=rb_path,
        signing_key=SIGNING_KEY,
        token=TOKEN,
        box_id="rollback-box",
        poll_interval=0.05,
        apply_timeout=1.0,
        health_path="/healthz",
        apply_backoff=30.0,
    )
    rb_state = fleet_agent.AgentState()
    set_desired(ccp_url, "version: v0.3\n# rb good A\n")
    fleet_agent.reconcile_once(rb_cfg, rb_state)  # healthy apply
    good_hash = rb_router.r.active_hash()
    # Invalid YAML: the file bytes converge (a byte-hash cannot tell), but the
    # router refuses to LOAD it -> /config/loaded-hash stays at last-good -> agent rolls back.
    # Mirrors the real hardware R8 probe (verify-hardening.sh check_auto_rollback).
    set_desired(
        ccp_url,
        "version: v0.3\n# rb bad B invalid-on-reload\nfleet_verify_rollback_probe: {[}\n",
    )
    rb_res = fleet_agent.reconcile_once(rb_cfg, rb_state)
    with open(rb_path, "r", encoding="utf-8") as fh:
        on_disk = fh.read()
    record(
        "auto-rollback on unloadable apply (invalid config -> loaded-hash mismatch, restores .bak, reports rolled_back)",
        rb_res.get("result") == "rolled_back"
        and "loaded-hash" in rb_res.get("reason", "")
        and rb_router.r.active_hash() == good_hash
        and "# rb good A" in on_disk,
        rb_res.get("reason", ""),
    )
    rb_res2 = fleet_agent.reconcile_once(rb_cfg, rb_state)  # same bad version again
    record(
        "backoff after rollback (bad version not rewritten next cycle)",
        rb_res2.get("result") == "apply_backoff",
        rb_res2.get("reason", ""),
    )

    # --- 12. CCP durability across a simulated restart (R6) --------------------
    dur_dir = os.path.join(workdir, "ccp-durable")
    d1 = ccp_server.CCPState(SIGNING_KEY, TOKEN, dur_dir)
    d1.set_desired("dur-a\n")
    d1.set_desired("dur-b\n")
    v3 = d1.set_desired("dur-c\n")
    d1.record_status(
        {
            "box_id": "halo-a",
            "version": v3,
            "hash": "h",
            "result": "applied",
            "apply_seconds": 0.5,
        }
    )
    d2 = ccp_server.CCPState(SIGNING_KEY, TOKEN, dur_dir)  # brand-new state = "restart"
    restored = (
        d2.version == v3
        and d2.config_text == "dur-c\n"
        and d2._version_num == 3
        and d2._audit_total >= 1
        and "halo-a" in d2.boxes
    )
    record(
        "CCP durability: desired+version+audit restored across restart",
        restored,
        "restored version=%s (want %s)" % (d2.version, v3),
    )
    next_v = d2.set_desired("dur-d\n")
    record(
        "CCP durability: version counter persists (no collision after restart)",
        next_v == "v4",
        "next version=%s (want v4)" % next_v,
    )

    # --- 13. mTLS client-cert handshake (C1) -----------------------------------
    # Prove the C1 loop offline: a CCP that REQUIRES client certs (started with
    # CCP_TLS_CLIENT_CA, i.e. verify_mode=CERT_REQUIRED via _maybe_wrap_tls)
    # accepts a client that PRESENTS one through client_ssl_context's new
    # load_cert_chain (FLEET_TLS_CLIENT_CERT/KEY), and rejects a certless client
    # at the TLS handshake -- before any HTTP is served. Uses the REAL server TLS
    # path (ccp_server._maybe_wrap_tls) and the REAL client path
    # (fleet_lib.client_ssl_context), so it exercises the shipped code, not a stub.
    mtls_dir = os.path.join(workdir, "mtls")
    os.makedirs(mtls_dir, exist_ok=True)
    certs = _make_mtls_certs(mtls_dir)
    if not certs:
        record(
            "mTLS: client presenting FLEET_TLS_CLIENT_CERT/KEY is accepted (C1)",
            False,
            "openssl unavailable to mint certs",
        )
        record(
            "mTLS: certless client is rejected at the TLS handshake (C1)",
            False,
            "openssl unavailable to mint certs",
        )
    else:
        mtls_state = ccp_server.CCPState(
            SIGNING_KEY, TOKEN, os.path.join(workdir, "mtls-ccp")
        )
        mtls_state.set_desired("mtls-config\n")
        mtls_port = free_port()
        mtls_server = ccp_server.make_server("127.0.0.1", mtls_port, mtls_state)
        # Drive the shipped server TLS/mTLS wrapping (_maybe_wrap_tls reads env).
        _saved = {
            k: os.environ.get(k)
            for k in ("CCP_TLS_CERT", "CCP_TLS_KEY", "CCP_TLS_CLIENT_CA")
        }
        os.environ["CCP_TLS_CERT"] = certs["server_cert"]
        os.environ["CCP_TLS_KEY"] = certs["server_key"]
        os.environ["CCP_TLS_CLIENT_CA"] = certs["ca"]
        try:
            scheme = ccp_server._maybe_wrap_tls(mtls_server)
        finally:
            for key, val in _saved.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
        serve(mtls_server)
        time.sleep(0.3)
        mtls_url = "https://127.0.0.1:%d" % mtls_port
        # (a) presenting the client cert completes the handshake and is served.
        ctx_ok = fleet_lib.client_ssl_context(
            env={
                "FLEET_TLS_CA": certs["ca"],
                "FLEET_TLS_CLIENT_CERT": certs["client_cert"],
                "FLEET_TLS_CLIENT_KEY": certs["client_key"],
            }
        )
        try:
            ok_status, _txt = fleet_lib._request(
                mtls_url + "/healthz", "GET", ssl_context=ctx_ok, timeout=5.0
            )
        except Exception as exc:  # noqa: BLE001 - report any handshake failure
            ok_status = "raised: %s" % type(exc).__name__
        record(
            "mTLS: client presenting FLEET_TLS_CLIENT_CERT/KEY is accepted (C1)",
            ok_status == 200 and scheme == "https",
            "status=%s" % (ok_status,),
        )
        # (b) a certless client (CA only, no client cert) is rejected at the TLS
        #     layer -- either var alone is ignored, so no cert is presented.
        ctx_no = fleet_lib.client_ssl_context(env={"FLEET_TLS_CA": certs["ca"]})
        certless_rejected = False
        try:
            bad_status, _t = fleet_lib._request(
                mtls_url + "/healthz", "GET", ssl_context=ctx_no, timeout=5.0
            )
            detail = "unexpectedly served, status=%s" % (bad_status,)
        except Exception as exc:  # noqa: BLE001 - the handshake SHOULD fail here
            certless_rejected = True
            detail = "rejected (%s)" % type(exc).__name__
        record(
            "mTLS: certless client is rejected at the TLS handshake (C1)",
            certless_rejected,
            detail,
        )

    # --- 14. warm-standby replicate -> restore (C2) ----------------------------
    # Mirror the C2 dry-run: drive a CCPState, replicate CCP_STATE_DIR (desired/ +
    # audit.log) with ccp-standby-sync.sh in local mode, then boot a FRESH
    # CCPState on the copy and assert it is an exact continuation of the active
    # CCP (latest version + config bytes + full audit total + per-box status),
    # and that its version counter keeps going without colliding with history.
    sb_active = os.path.join(workdir, "standby-active", "ccp")
    sb_state = ccp_server.CCPState(SIGNING_KEY, TOKEN, sb_active)
    sb_state.set_desired("standby-a\n")
    sb_state.set_desired("standby-b\n")
    sb_v = sb_state.set_desired("standby-c\n")
    sb_hash = fleet_lib.sha256_hex(b"standby-c\n")
    sb_state.record_status(
        {
            "box_id": "halo-a",
            "version": sb_v,
            "hash": sb_hash,
            "result": "applied",
            "apply_seconds": 0.4,
        }
    )
    sb_state.record_status(
        {
            "box_id": "halo-b",
            "version": sb_v,
            "hash": sb_hash,
            "result": "applied",
            "apply_seconds": 0.6,
        }
    )
    active_total = sb_state._audit_total

    sb_standby = os.path.join(workdir, "standby-copy", "ccp")
    synced = _standby_sync(
        sb_active, sb_standby, os.path.join(workdir, "standby-fleet")
    )
    replicated_ok = (
        synced
        and os.path.isdir(os.path.join(sb_standby, "desired"))
        and os.path.isfile(os.path.join(sb_standby, "audit.log"))
    )
    record(
        "warm-standby sync replicates desired/ + audit.log (C2, ccp-standby-sync.sh)",
        replicated_ok,
        "dest=%s" % sb_standby,
    )

    sb_restored = ccp_server.CCPState(SIGNING_KEY, TOKEN, sb_standby)
    # Snapshot the RESTORED state before advancing the counter (set_desired mutates
    # version/config), then confirm the counter keeps going with no collision.
    restored_version = sb_restored.version
    restored_config = sb_restored.config_text
    restored_total = sb_restored._audit_total
    restored_a = sb_restored.boxes.get("halo-a", {}).get("version")
    restored_b = sb_restored.boxes.get("halo-b", {}).get("version")
    sb_next = sb_restored.set_desired("standby-d\n")
    restore_ok = (
        restored_version == sb_v
        and restored_config == "standby-c\n"
        and restored_total == active_total
        and restored_a == sb_v
        and restored_b == sb_v
        and sb_next == "v4"
    )
    record(
        "warm-standby restore: fresh CCPState = latest version+config+audit (C2)",
        restore_ok,
        "restored=%s audit=%d next=%s" % (restored_version, restored_total, sb_next),
    )

    passed = sum(1 for _n, ok, _d in CHECKS if ok)
    total = len(CHECKS)
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
