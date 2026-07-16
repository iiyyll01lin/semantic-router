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

Exit code 0 means all checks passed. This verifies the NEW fan-out/drift/sign/
audit logic; the real ROCm gateway path is exercised by deploy-fleet-2box.sh on
the Strix Halo boxes.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time

import ccp_server
import fleet_agent
import fleet_lib
import mock_router

SIGNING_KEY = "poc-fleet-signing-key"
TOKEN = "poc-fleet-token"

CHECKS = []


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
        ccp_url + "/fleet/desired", {"config": config_text}, token=TOKEN)
    if status != 200:
        raise RuntimeError("set_desired failed: %d %s" % (status, obj))
    return obj["version"]


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
            ccp_url=ccp_url, router_api="http://127.0.0.1:%d" % rport,
            config_file=cfg_path, signing_key=SIGNING_KEY, token=TOKEN,
            box_id=box, poll_interval=0.05, apply_timeout=5.0)
        boxes.append({"box": box, "cfg_path": cfg_path, "rstate": rstate,
                      "acfg": acfg, "astate": fleet_agent.AgentState()})

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
        for b in boxes)
    no_restart = all(
        b["rstate"].snapshot()["start_time"] == starts_after_v1[b["box"]] for b in boxes)
    record("edit-once converges both boxes", converged2)
    record("applied via hot-reload, not restart (reload+1, start_time stable)",
           hot_reloaded and no_restart)

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
        ccp_url=ccp_url, router_api=boxes[0]["acfg"].router_api,
        config_file=boxes[0]["cfg_path"], signing_key="WRONG-KEY", token=TOKEN,
        box_id="halo-a", poll_interval=0.05, apply_timeout=2.0)
    before = boxes[0]["rstate"].active_hash()
    res = fleet_agent.reconcile_once(bad_cfg, fleet_agent.AgentState())
    after = boxes[0]["rstate"].active_hash()
    rejected = res.get("result") == "rejected" and before == after
    record("tamper rejection (untrusted bundle rejected, config unchanged)", rejected,
           res.get("reason", ""))
    _ = bad

    # --- 6. central audit captured every apply --------------------------
    status, audit = fleet_lib.http_get_json(ccp_url + "/fleet/audit", token=TOKEN)
    applied_records = [r for r in audit.get("audit", []) if r.get("result") in ("applied", "in_sync")]
    rejected_records = [r for r in audit.get("audit", []) if r.get("result") == "rejected"]
    audit_ok = status == 200 and len(applied_records) >= 4 and len(rejected_records) >= 1
    record("central audit log captured applies + the rejection", audit_ok,
           "applies=%d rejects=%d" % (len(applied_records), len(rejected_records)))

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
        ccp_url=ccp_url, router_api="http://127.0.0.1:1", config_file=inode_path,
        signing_key=SIGNING_KEY, token=TOKEN, box_id="inode-check",
        poll_interval=0.05, apply_timeout=1.0)
    new_text = "version: v0.3\n# after, rewritten in place\n"
    fleet_agent._write_config(inode_cfg, new_text)
    ino_after = os.stat(inode_path).st_ino
    with open(inode_path, "rb") as fh:
        on_disk = fh.read()
    inode_ok = (ino_before == ino_after and on_disk == new_text.encode("utf-8"))
    record("in-place write keeps the inode (real-gateway reload safe)", inode_ok,
           "inode %s preserved" % ino_before if inode_ok
           else "inode %s -> %s" % (ino_before, ino_after))

    # --- 8. router outage is retryable -----------------------------------
    # A real gateway can briefly drop :8080/config/hash while containers restart
    # or hot-reload. The long-running pull agent must report a retryable result,
    # not exit with an uncaught urllib connection error.
    down_cfg = fleet_agent.AgentConfig(
        ccp_url=ccp_url, router_api="http://127.0.0.1:1",
        config_file=inode_path, signing_key=SIGNING_KEY, token=TOKEN,
        box_id="router-down", poll_interval=0.05, apply_timeout=0.2)
    try:
        down_res = fleet_agent.reconcile_once(down_cfg, fleet_agent.AgentState())
        outage_retry = down_res.get("result") == "router_error"
    except Exception as exc:
        down_res = {"result": "raised", "reason": repr(exc)}
        outage_retry = False
    record("router outage returns retryable router_error (agent stays alive)",
           outage_retry, down_res.get("reason", ""))

    passed = sum(1 for _n, ok, _d in CHECKS if ok)
    total = len(CHECKS)
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
