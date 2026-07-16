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

import calendar
import json
import os
import shutil
import time

try:
    from . import fleet_lib  # type: ignore
except Exception:  # pragma: no cover
    import fleet_lib


def _env_or(name: str, default: str) -> str:
    """os.environ.get that treats an empty/whitespace value as unset.

    The bring-up/deploy scripts forward OPTIONAL tuning vars as env-prefixes that
    may be empty strings; without this, ``float("")`` on a blank numeric env would
    crash the agent. Empty -> default keeps those opt-in passthroughs safe.
    """
    val = os.environ.get(name, "")
    return val if val.strip() != "" else default


def _version_num(version) -> "int | None":
    """Parse the integer from a ``vN`` version string; None if not parseable."""
    s = str(version).strip()
    if s[:1] == "v":
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        return None


def _bundle_age_seconds(ts: str):
    """Seconds since the bundle's UTC ``ts`` (``%Y-%m-%dT%H:%M:%SZ``); None if
    unparseable so freshness enforcement fails safe (does not falsely reject)."""
    try:
        parsed = time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None
    return time.time() - calendar.timegm(parsed)


class AgentConfig:
    def __init__(self, ccp_url, router_api, config_file, signing_key, token,
                 box_id, poll_interval=5.0, apply_timeout=15.0, health_path="",
                 health_timeout=5.0, apply_backoff=30.0, apply_backoff_max=300.0,
                 verifier=None, bundle_max_age=0.0, status_buffer="",
                 status_buffer_max=1000):
        self.ccp_url = ccp_url.rstrip("/")
        self.router_api = router_api.rstrip("/")
        self.config_file = config_file
        self.signing_key = signing_key
        self.token = token
        self.box_id = box_id
        self.poll_interval = float(poll_interval)
        self.apply_timeout = float(apply_timeout)
        # R4: the bundle verifier (HMAC by default; Ed25519 public key when
        # configured). A bare signing_key keeps the old behavior for direct
        # constructors (e.g. verify_local passes the shared HMAC key).
        self.verifier = verifier if verifier is not None else fleet_lib.hmac_verifier(signing_key)
        # R4 freshness: reject a signed bundle older than this many seconds (0 =
        # off). Only meaningful when the CCP stamps bundles (FLEET_BUNDLE_TS=1).
        self.bundle_max_age = float(bundle_max_age)
        # R9 status buffering: if the CCP is down, apply outcomes are appended to
        # a local buffer file and re-sent on the next successful report (so a CCP
        # outage no longer silently loses what each box applied). Defaults beside
        # the config file; bounded so a long outage cannot grow it without limit.
        self.status_buffer = status_buffer or (config_file + ".status-buffer.jsonl")
        self.status_buffer_max = int(status_buffer_max)
        # R8 health-gated apply: an OPTIONAL stronger readiness probe hit after
        # apply (e.g. a lightweight completion or /health). When unset, health is
        # simply "GET /config/hash still answers 200" (the router is alive).
        self.health_path = ("/" + health_path.strip("/")) if health_path and health_path.strip("/") else ""
        self.health_timeout = float(health_timeout)
        # R8 backoff: after a failed (unhealthy/unconverged) apply we restore the
        # backup and refuse to rewrite the SAME bad version until this grows-then-
        # caps window elapses, so one bad config does not thrash the router.
        self.apply_backoff = float(apply_backoff)
        self.apply_backoff_max = float(apply_backoff_max)

    @classmethod
    def from_env(cls):
        mode = os.environ.get("FLEET_SIGN_MODE", fleet_lib.SIGN_HMAC).strip().lower()
        required = ["CCP_URL", "ROUTER_API", "CONFIG_FILE", "FLEET_TOKEN", "BOX_ID"]
        if mode != fleet_lib.SIGN_ED25519:
            required.append("FLEET_SIGNING_KEY")  # HMAC needs the shared key
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise SystemExit("missing env: " + ", ".join(missing))
        try:
            verifier = fleet_lib.verifier_from_env()  # HMAC by default; Ed25519 if configured
        except (ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from exc
        return cls(
            ccp_url=os.environ["CCP_URL"],
            router_api=os.environ["ROUTER_API"],
            config_file=os.environ["CONFIG_FILE"],
            signing_key=os.environ.get("FLEET_SIGNING_KEY", ""),
            token=os.environ["FLEET_TOKEN"],
            box_id=os.environ["BOX_ID"],
            poll_interval=_env_or("POLL_INTERVAL", "5"),
            apply_timeout=_env_or("APPLY_TIMEOUT", "15"),
            health_path=os.environ.get("ROUTER_HEALTH_PATH", ""),
            health_timeout=_env_or("ROUTER_HEALTH_TIMEOUT", "5"),
            apply_backoff=_env_or("APPLY_BACKOFF", "30"),
            apply_backoff_max=_env_or("APPLY_BACKOFF_MAX", "300"),
            verifier=verifier,
            bundle_max_age=_env_or("FLEET_BUNDLE_MAX_AGE", "0"),
            status_buffer=os.environ.get("STATUS_BUFFER", ""),
            status_buffer_max=_env_or("STATUS_BUFFER_MAX", "1000"),
        )


class AgentState:
    def __init__(self):
        self.applied_version = ""
        self.last_result = ""
        # R8 auto-rollback backoff bookkeeping.
        self.failed_version = ""
        self.backoff_until = 0.0
        self.backoff_seconds = 0.0
        # R9 hot-reload latency: last write->converge time (seconds).
        self.last_apply_seconds = None


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


def _router_health(cfg: AgentConfig):
    """Return ``(ok, detail)``: does the router still SERVE after an apply?

    Hash convergence proves the router *read* the new bytes, not that it can
    still serve (a bad-but-parseable config can wedge it). Liveness here is "GET
    /config/hash still answers 200"; when ``ROUTER_HEALTH_PATH`` is set we also
    require a 200 from that stronger readiness/completion probe.
    """
    try:
        status, _obj = fleet_lib.http_get_json(
            cfg.router_api + "/config/hash", token=None, timeout=cfg.health_timeout)
    except Exception as exc:  # non-200 raises HTTPError -> unhealthy
        return False, "config/hash unreachable: %s" % exc
    if status != 200:
        return False, "config/hash returned %d" % status
    if cfg.health_path:
        try:
            hstatus, _txt = fleet_lib.http_get_text(
                cfg.router_api + cfg.health_path, token=None, timeout=cfg.health_timeout)
        except Exception as exc:
            return False, "health probe %s failed: %s" % (cfg.health_path, exc)
        if hstatus != 200:
            return False, "health probe %s returned %d" % (cfg.health_path, hstatus)
    return True, "ok"


def _restore_backup(cfg: AgentConfig) -> bool:
    """Restore the ``.bak`` written by ``_write_config`` IN PLACE (same inode).

    Uses truncate+write (never an atomic rename) for the exact reason documented
    in ``_write_config``: the router bind-mounts the config as a single file and
    watches that inode; a rename would swap in a new inode the container never
    sees, so the rollback would be invisible and no fsnotify reload would fire.
    """
    bak = cfg.config_file + ".bak"
    if not os.path.exists(bak):
        return False
    try:
        with open(bak, "rb") as fh:
            data = fh.read()
        with open(cfg.config_file, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        return False


def _read_status_buffer(cfg: AgentConfig) -> list:
    out = []
    try:
        with open(cfg.status_buffer, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _write_status_buffer(cfg: AgentConfig, records: list) -> None:
    # Keep only the most recent N so a long CCP outage cannot grow this without
    # bound. This is agent-local metadata (NOT the router config), so an atomic
    # temp+replace is correct here -- the in-place rule only applies to the
    # bind-mounted config file in _write_config.
    if len(records) > cfg.status_buffer_max:
        records = records[-cfg.status_buffer_max:]
    try:
        if not records:
            if os.path.exists(cfg.status_buffer):
                os.remove(cfg.status_buffer)
            return
        tmp = cfg.status_buffer + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        os.replace(tmp, cfg.status_buffer)
    except OSError:
        pass


def _report(cfg: AgentConfig, payload: dict) -> None:
    """Report an apply outcome to the CCP, buffering across CCP downtime (R9).

    Best-effort and non-blocking as before, but instead of dropping outcomes when
    the CCP is unreachable we append them to a local buffer and flush the backlog
    (oldest first) on the next successful report, so the central audit does not
    silently lose what a box applied while the CCP was down.
    """
    payload = dict(payload)
    payload["box_id"] = cfg.box_id
    pending = _read_status_buffer(cfg)
    pending.append(payload)
    unsent = []
    failed = False
    for rec in pending:
        if failed:
            unsent.append(rec)
            continue
        try:
            status, _obj = fleet_lib.http_post_json(
                cfg.ccp_url + "/fleet/status", rec, token=cfg.token)
            if status != 200:
                failed = True
                unsent.append(rec)
        except Exception:
            failed = True
            unsent.append(rec)
    _write_status_buffer(cfg, unsent)


def reconcile_once(cfg: AgentConfig, state: AgentState) -> dict:
    """Run one reconcile cycle. Returns a result dict (also used by the verifier)."""
    try:
        status, bundle = fleet_lib.http_get_json(cfg.ccp_url + "/fleet/desired", token=cfg.token)
    except Exception as exc:  # network/pull failure -> retry next cycle
        return {"result": "pull_error", "reason": str(exc)}
    if status != 200:
        return {"result": "pull_error", "reason": "CCP returned %d" % status}

    ok, reason = fleet_lib.verify_bundle(cfg.verifier, bundle)
    if not ok:
        _report(cfg, {"result": "rejected", "reason": reason,
                      "version": str(bundle.get("version", ""))})
        return {"result": "rejected", "reason": reason}

    version = str(bundle["version"])

    # R4 anti-downgrade / replay: never accept a bundle OLDER than the version we
    # already applied (an attacker could replay a stale, still-validly-signed
    # bundle to revert the fleet). Equal versions are allowed so drift self-heal
    # can re-apply the current desired config after an out-of-band edit.
    new_n, last_n = _version_num(version), _version_num(state.applied_version)
    if new_n is not None and last_n is not None and new_n < last_n:
        reason = "anti-downgrade: %s is older than applied %s" % (version, state.applied_version)
        _report(cfg, {"result": "rejected", "reason": reason, "version": version})
        return {"result": "rejected", "reason": reason}

    # R4 freshness (opt-in): reject a signed-but-stale bundle when the CCP stamps
    # bundles (FLEET_BUNDLE_TS=1) and the agent enforces a max age.
    if cfg.bundle_max_age > 0 and bundle.get("ts"):
        age = _bundle_age_seconds(str(bundle["ts"]))
        if age is not None and age > cfg.bundle_max_age:
            reason = "stale bundle: age %.0fs > max %.0fs" % (age, cfg.bundle_max_age)
            _report(cfg, {"result": "rejected", "reason": reason, "version": version})
            return {"result": "rejected", "reason": reason}

    desired_hash = bundle["sha256"]
    try:
        cur_hash = _router_hash(cfg)
    except Exception as exc:
        return {"result": "router_error", "reason": str(exc)}

    if cur_hash == desired_hash:
        # Router already serves the desired config -> clear any prior failure so
        # a later good version is retried immediately.
        state.failed_version = ""
        state.backoff_until = 0.0
        state.backoff_seconds = 0.0
        if state.applied_version != version:
            state.applied_version = version
            _report(cfg, {"result": "in_sync", "version": version, "hash": cur_hash})
        return {"result": "in_sync", "version": version, "hash": cur_hash}

    # Drift detected: we must (re)apply this version. If this exact version just
    # failed to apply healthily, back off instead of rewriting the same bad
    # config every cycle (that would thrash the router's fsnotify hot-reload).
    now = time.time()
    if version == state.failed_version and now < state.backoff_until:
        return {"result": "apply_backoff", "version": version,
                "reason": "backing off %.1fs after a failed apply of %s"
                          % (state.backoff_until - now, version)}

    # Apply + time the write->converge window (R9 hot-reload latency timer).
    t0 = time.monotonic()
    _write_config(cfg, bundle["config"])
    converged = _wait_for_hash(cfg, desired_hash)
    apply_seconds = round(time.monotonic() - t0, 3)

    healthy, health_detail = (True, "ok")
    if converged:
        healthy, health_detail = _router_health(cfg)

    if converged and healthy:
        try:
            new_hash = _router_hash(cfg)
        except Exception:
            new_hash = desired_hash
        state.applied_version = version
        state.last_result = "applied"
        state.last_apply_seconds = apply_seconds
        state.failed_version = ""
        state.backoff_until = 0.0
        state.backoff_seconds = 0.0
        _report(cfg, {"result": "applied", "version": version, "hash": new_hash,
                      "apply_seconds": apply_seconds})
        return {"result": "applied", "version": version, "hash": new_hash,
                "apply_seconds": apply_seconds}

    # Health-gated apply FAILED (never converged, or converged but the router is
    # no longer serving): restore the .bak and report rolled_back (R8). Never
    # mark the bad version as applied; keep the last good applied_version.
    if not converged:
        reason = "did not converge within %.1fs" % cfg.apply_timeout
    else:
        reason = "router unhealthy after apply: %s" % health_detail
    restored = _restore_backup(cfg)
    prev_hash = ""
    if restored:
        # Best-effort: read the hash the router serves after the rollback reload.
        try:
            prev_hash = _router_hash(cfg)
        except Exception:
            prev_hash = ""
    # Exponential backoff (grows, then caps) keyed to this failed version.
    state.failed_version = version
    state.backoff_seconds = min(
        cfg.apply_backoff_max,
        cfg.apply_backoff if state.backoff_seconds <= 0 else state.backoff_seconds * 2)
    state.backoff_until = now + state.backoff_seconds
    state.last_result = "rolled_back"
    detail = reason + ("; restored .bak" if restored else "; no .bak to restore")
    _report(cfg, {"result": "rolled_back", "version": version, "hash": prev_hash,
                  "reason": detail, "apply_seconds": apply_seconds})
    return {"result": "rolled_back", "version": version, "hash": prev_hash,
            "reason": detail}


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
