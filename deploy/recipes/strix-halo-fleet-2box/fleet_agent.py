"""Pull agent for the edge-fleet config control plane (stdlib only).

One agent runs co-located with each edge gateway. It is PULL-ONLY: it makes
outbound calls to the CCP and to the local router only, so a NAT'd/firewalled
edge AIPC needs no inbound exposure. Each cycle:

  1. GET  <CCP>/fleet/desired              (pull the signed bundle)
  2. verify the HMAC signature + content hash (reject tampered/unsigned bundles)
  3. GET  <router>/config/hash             (read the active-config drift signal)
  4. if drift: back up + write the local config file -> the router hot-reloads
     via fsnotify (no restart); poll /config/hash until it converges
  5. POST <CCP>/fleet/status               (report what was applied -> audit)

Applying by writing the watched config file reuses the router's existing
fsnotify hot-reload and avoids depending on the (auth-less) PUT /config/router
JSON schema; the trust boundary is the signed CCP bundle plus localhost-only
writes. See docs/agent/plans/pl-0036-edge-fleet-config-control-plane.md.
"""

from __future__ import annotations

import os
import shutil
import time

try:
    from . import fleet_lib  # type: ignore
except Exception:  # pragma: no cover
    import fleet_lib


class AgentConfig:
    def __init__(self, ccp_url, router_api, config_file, signing_key, token,
                 box_id, poll_interval=5.0, apply_timeout=15.0):
        self.ccp_url = ccp_url.rstrip("/")
        self.router_api = router_api.rstrip("/")
        self.config_file = config_file
        self.signing_key = signing_key
        self.token = token
        self.box_id = box_id
        self.poll_interval = float(poll_interval)
        self.apply_timeout = float(apply_timeout)

    @classmethod
    def from_env(cls):
        required = ["CCP_URL", "ROUTER_API", "CONFIG_FILE", "FLEET_SIGNING_KEY",
                    "FLEET_TOKEN", "BOX_ID"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise SystemExit("missing env: " + ", ".join(missing))
        return cls(
            ccp_url=os.environ["CCP_URL"],
            router_api=os.environ["ROUTER_API"],
            config_file=os.environ["CONFIG_FILE"],
            signing_key=os.environ["FLEET_SIGNING_KEY"],
            token=os.environ["FLEET_TOKEN"],
            box_id=os.environ["BOX_ID"],
            poll_interval=os.environ.get("POLL_INTERVAL", "5"),
            apply_timeout=os.environ.get("APPLY_TIMEOUT", "15"),
        )


class AgentState:
    def __init__(self):
        self.applied_version = ""
        self.last_result = ""


def _router_hash(cfg: AgentConfig) -> str:
    status, obj = fleet_lib.http_get_json(cfg.router_api + "/config/hash", token=None)
    if status != 200:
        raise RuntimeError("router /config/hash returned %d" % status)
    return str(obj.get("hash", ""))


def _write_config(cfg: AgentConfig, config_text: str) -> None:
    # Back up the current file next to it, then overwrite the config IN PLACE
    # (same inode) -- NOT via a temp-file rename. The real vllm-sr gateway
    # bind-mounts this file into the router container as a SINGLE FILE
    # (`<host>/config.yaml:/app/config.yaml:z`, see docker_start.py), which pins
    # the inode: an atomic rename swaps in a NEW inode the container never sees,
    # so the new config would be invisible AND the in-container fsnotify file
    # watch would never fire a reload. An in-place truncate+write keeps the
    # inode, so the change is visible in the container and triggers the
    # Write-event hot-reload (server_config_watch.go). The router debounces and
    # waits ~300ms before reading, so it never observes the brief write window.
    # Write the exact bytes (no newline translation) so the on-disk SHA256
    # matches the bundle hash GET /config/hash reports, on every platform.
    data = config_text.encode("utf-8")
    if os.path.exists(cfg.config_file):
        shutil.copy2(cfg.config_file, cfg.config_file + ".bak")
    with open(cfg.config_file, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _wait_for_hash(cfg: AgentConfig, desired_hash: str) -> bool:
    deadline = time.time() + cfg.apply_timeout
    while time.time() < deadline:
        try:
            if _router_hash(cfg) == desired_hash:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _report(cfg: AgentConfig, payload: dict) -> None:
    payload = dict(payload)
    payload["box_id"] = cfg.box_id
    try:
        fleet_lib.http_post_json(cfg.ccp_url + "/fleet/status", payload, token=cfg.token)
    except Exception:
        pass  # status reporting is best-effort; never block reconcile on it


def reconcile_once(cfg: AgentConfig, state: AgentState) -> dict:
    """Run one reconcile cycle. Returns a result dict (also used by the verifier)."""
    try:
        status, bundle = fleet_lib.http_get_json(cfg.ccp_url + "/fleet/desired", token=cfg.token)
    except Exception as exc:  # network/pull failure -> retry next cycle
        return {"result": "pull_error", "reason": str(exc)}
    if status != 200:
        return {"result": "pull_error", "reason": "CCP returned %d" % status}

    ok, reason = fleet_lib.verify_bundle(cfg.signing_key, bundle)
    if not ok:
        _report(cfg, {"result": "rejected", "reason": reason,
                      "version": str(bundle.get("version", ""))})
        return {"result": "rejected", "reason": reason}

    desired_hash = bundle["sha256"]
    try:
        cur_hash = _router_hash(cfg)
    except Exception as exc:
        return {"result": "router_error", "reason": str(exc)}

    if cur_hash == desired_hash:
        if state.applied_version != bundle["version"]:
            state.applied_version = bundle["version"]
            _report(cfg, {"result": "in_sync", "version": bundle["version"], "hash": cur_hash})
        return {"result": "in_sync", "version": bundle["version"], "hash": cur_hash}

    _write_config(cfg, bundle["config"])
    converged = _wait_for_hash(cfg, desired_hash)
    try:
        new_hash = _router_hash(cfg)
    except Exception:
        new_hash = ""
    result = "applied" if converged else "apply_unconverged"
    state.applied_version = bundle["version"]
    state.last_result = result
    _report(cfg, {"result": result, "version": bundle["version"], "hash": new_hash})
    return {"result": result, "version": bundle["version"], "hash": new_hash}


def run_loop(cfg: AgentConfig) -> int:
    state = AgentState()
    print("agent[%s] pulling %s -> router %s every %.1fs" % (
        cfg.box_id, cfg.ccp_url, cfg.router_api, cfg.poll_interval))
    while True:
        res = reconcile_once(cfg, state)
        if res.get("result") not in ("in_sync",):
            print("agent[%s] %s" % (cfg.box_id, res))
        time.sleep(cfg.poll_interval)


def main() -> int:
    cfg = AgentConfig.from_env()
    if os.environ.get("ONESHOT"):
        print(reconcile_once(cfg, AgentState()))
        return 0
    return run_loop(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
