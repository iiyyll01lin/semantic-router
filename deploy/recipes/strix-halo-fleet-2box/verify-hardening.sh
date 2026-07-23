#!/usr/bin/env bash
#
# verify-hardening.sh -- OPT-IN, gateway-safe HARDWARE verifier for the CCP
# production-hardening (Part A + R1). It is deliberately SEPARATE from
# verify-fleet.sh so the core verifier stays stable: this script exercises the
# harder-to-fake, on-hardware behaviors of the just-landed hardening and the new
# C1/C2 features against a LIVE fleet brought up by deploy-fleet-2box.sh.
#
# It mirrors verify-fleet.sh's conventions (sources fleet_common.sh + fleet.env,
# drives the fleet through fleetctl.py, PASS/FAIL per check) and adds a [SKIP]
# for every check whose prerequisite env/hardware is absent, so it is always
# safe to run: with nothing enabled it simply skips the opt-in checks. The
# offline, deterministic proofs of the SAME behaviors live in verify_local.py
# (mTLS handshake, warm-standby restore, Ed25519 forge-reject, auto-rollback,
# durability) and run in CI without hardware.
#
# Checks (each guarded; [SKIP] when its prerequisite is missing):
#   R1  drift-heal on gateway  -- FLEET_VERIFY_DRIFT_ON_GATEWAY=1 + a bind-mounted
#                                 gateway config: append a harmless COMMENT
#                                 out-of-band and assert the agent reverts it via
#                                 /config/hash (a comment is a config the router
#                                 still accepts, so this is safe on a live box).
#   R8  auto-rollback          -- gateway mode: push a config the REAL router
#                                 rejects on reload; assert the agent restores the
#                                 .bak, reports rolled_back, and the gateway keeps
#                                 serving. Always restores the good desired after.
#   R6  CCP durability         -- restart the local CCP; assert GET /fleet/desired
#                                 still returns the last version+hash (no 404, no
#                                 v1 reset).
#   R4/R5/C1 Ed25519+TLS/mTLS   -- FLEET_SIGN_MODE=ed25519: assert the fleet
#                                 converges over the deployed transport (https +
#                                 mTLS when configured) and that a forged /
#                                 HMAC-downgraded bundle is rejected by the
#                                 deployed public key.
#   R9  metrics                -- scrape GET /metrics (Bearer token) and assert
#                                 version-lag + outcome counters; run
#                                 fleet_metrics.py over the CCP audit.log and
#                                 assert hot_reload_latency_seconds p50/p95.
#   R7  N-box                  -- if FLEET_BOXES lists >2 boxes, assert all
#                                 converge.
#   C2  warm-standby dry-run   -- FLEET_VERIFY_STANDBY=1: replicate the live state
#                                 dir via ccp-standby-sync.sh (local mode) and
#                                 assert a fresh CCPState restores the latest
#                                 version (the full promotion drill is in the
#                                 runbook).
#
# It CANNOT fully run without a live fleet; the on-hardware execution is a
# documented step in docs/hardware-validation-runbook.md.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"

ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
[ -f "${ENV_FILE}" ] || { echo "ERROR: ${ENV_FILE} not found; run deploy-fleet-2box.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source "${ENV_FILE}"
PYBIN="$(fleet_pybin)"

FLEET_MODE="${FLEET_MODE:-mock}"
FLEET_SIGN_MODE="${FLEET_SIGN_MODE:-hmac}"
FLEET_BOXES="${FLEET_BOXES:-halo-a,halo-b}"
ROUTER_PORT="${ROUTER_PORT:-8080}"
CCP_URL="${CCP_URL:-http://localhost:${CCP_PORT}}"
case "${CCP_URL}" in
  https://*) CCP_SCHEME="https" ;;
  *)         CCP_SCHEME="http"  ;;
esac

# A snapshot of the CURRENT desired config, taken before any mutation, so the
# rollback/standby checks can restore the fleet to byte-identical desired bytes
# (a new version number is expected and harmless).
ORIG_CFG="${FLEET_STATE_DIR}/verify-hardening-orig.yaml"
BAD_CFG="${FLEET_STATE_DIR}/verify-hardening-bad.yaml"
DESIRED_DIRTY=0

FAIL=0
PASS_N=0
FAIL_N=0
SKIP_N=0
pass() { echo "[PASS] $*"; PASS_N=$((PASS_N + 1)); }
fail() { echo "[FAIL] $*"; FAIL_N=$((FAIL_N + 1)); FAIL=1; }
skip() { echo "[SKIP] $*"; SKIP_N=$((SKIP_N + 1)); }
fctl() { "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" "$@"; }

# Restore the good desired if we left a bad one in place (safety net for an early
# exit); clean up scratch files. Best-effort -- never fail the run in cleanup.
# shellcheck disable=SC2329  # invoked indirectly via 'trap cleanup EXIT'
cleanup() {
  if [ "${DESIRED_DIRTY}" = "1" ] && [ -s "${ORIG_CFG}" ]; then
    fctl set-desired "${ORIG_CFG}" >/dev/null 2>&1 || true
    fctl wait-converged --boxes halo-a --timeout 60 >/dev/null 2>&1 || true
  fi
  rm -f "${BAD_CFG}" "${ORIG_CFG}" 2>/dev/null || true
}
trap cleanup EXIT

# --- small helpers (all TLS/mTLS-aware via fleet_lib) ------------------------

# Snapshot the current desired-config bytes into ORIG_CFG (0 on success).
save_desired() {
  PYTHONPATH="${SCRIPT_DIR}" "${PYBIN}" - "${ORIG_CFG}" <<'PY'
import os, sys, fleet_lib
url = os.environ["CCP_URL"].rstrip("/")
try:
    st, obj = fleet_lib.http_get_json(url + "/fleet/desired", token=os.environ["FLEET_TOKEN"])
except Exception as exc:  # noqa: BLE001
    print("save_desired: %s" % exc, file=sys.stderr); sys.exit(1)
if st != 200 or not isinstance(obj, dict) or "config" not in obj:
    print("save_desired: unexpected response status=%s" % (st,), file=sys.stderr); sys.exit(1)
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write(obj["config"])
PY
}

# Print the local router's active /config/hash ("" on error).
router_hash() {
  PYTHONPATH="${SCRIPT_DIR}" ROUTER_PORT="${ROUTER_PORT}" "${PYBIN}" - <<'PY'
import os, fleet_lib
port = os.environ["ROUTER_PORT"]
try:
    st, obj = fleet_lib.http_get_json("http://localhost:%s/config/hash" % port, token=None)
    print(obj.get("hash", "") if st == 200 else "")
except Exception:  # noqa: BLE001
    print("")
PY
}

# Print the local router's /config/hash HTTP status (200 == still serving).
router_serving() {
  PYTHONPATH="${SCRIPT_DIR}" ROUTER_PORT="${ROUTER_PORT}" "${PYBIN}" - <<'PY'
import os, fleet_lib
port = os.environ["ROUTER_PORT"]
try:
    st, _ = fleet_lib.http_get_json("http://localhost:%s/config/hash" % port, token=None)
    print(st)
except Exception:  # noqa: BLE001
    print("ERR")
PY
}

# --- check functions (return 0 = pass, non-zero = fail) ---------------------

# R1: out-of-band append a comment to the bind-mounted gateway config; the agent
# must revert it so the router's /config/hash returns to the desired hash.
check_drift_heal() {
  local cfg desired_h cur _i
  cfg="${FLEET_STATE_DIR}/gateway/config.yaml"
  desired_h="$(fctl desired-hash)"
  [ -n "${desired_h}" ] || return 1
  fctl wait-converged --boxes halo-a --timeout 60 >/dev/null 2>&1 || true
  printf '\n# fleet-verify drift probe %s (out-of-band; the agent must revert this)\n' \
    "$(date -u +%Y%m%dT%H%M%SZ)" >>"${cfg}"
  for _i in $(seq 1 60); do
    cur="$(router_hash)"
    if [ -n "${cur}" ] && [ "${cur}" = "${desired_h}" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# R8: push a config the real router rejects on reload; the agent restores .bak
# and reports rolled_back while the gateway keeps serving. Always restores good.
check_auto_rollback() {
  local outcome serving _i
  cp -f "${ORIG_CFG}" "${BAD_CFG}"
  # Valid file bytes but invalid YAML flow ({[}) -> the real router's reload
  # parse fails, /config/hash never reaches the bad hash -> agent rolls back.
  printf '\n# fleet-verify induced-rollback probe (invalid YAML on purpose)\nfleet_verify_rollback_probe: {[}\n' \
    >>"${BAD_CFG}"
  DESIRED_DIRTY=1
  if ! fctl set-desired "${BAD_CFG}" >/dev/null 2>&1; then
    fctl set-desired "${ORIG_CFG}" >/dev/null 2>&1 || true
    DESIRED_DIRTY=0
    return 1
  fi
  outcome=""
  for _i in $(seq 1 90); do
    outcome="$(fctl audit 2>/dev/null | awk '$2=="halo-a"{r=$4} END{print r}')"
    [ "${outcome}" = "rolled_back" ] && break
    sleep 1
  done
  serving="$(router_serving)"
  # Restore the good desired and reconverge; leave no bad config behind.
  fctl set-desired "${ORIG_CFG}" >/dev/null 2>&1 || true
  fctl wait-converged --boxes halo-a --timeout 120 >/dev/null 2>&1 || true
  DESIRED_DIRTY=0
  [ "${outcome}" = "rolled_back" ] && [ "${serving}" = "200" ]
}

# R6: restart the local CCP and confirm desired version+hash survive (durability
# is _restore() at boot; no 404, no v1 reset).
check_ccp_durability() {
  local before_v before_h after_v after_h
  before_v="$(fctl desired-version)"
  before_h="$(fctl desired-hash)"
  [ -n "${before_v}" ] && [ -n "${before_h}" ] || return 1
  fleet_stop_pidfile "${FLEET_STATE_DIR}/ccp.pid"
  bash "${SCRIPT_DIR}/ccp-bring-up.sh" >/dev/null 2>&1 || return 1
  after_v="$(fctl desired-version)"
  after_h="$(fctl desired-hash)"
  [ -n "${after_v}" ] && [ "${after_v}" = "${before_v}" ] && [ "${after_h}" = "${before_h}" ]
}

# R4/C1: the deployed verifier (public key from env) must reject a forged bundle
# (different Ed25519 key) and an HMAC-downgraded bundle; where the private seed
# is available a genuine bundle must verify.
check_forge_reject() {
  PYTHONPATH="${SCRIPT_DIR}" "${PYBIN}" - <<'PY'
import sys
import fleet_lib
try:
    verifier = fleet_lib.verifier_from_env()
except Exception as exc:  # noqa: BLE001
    print("verifier_from_env failed: %s" % exc, file=sys.stderr); sys.exit(1)
seed, _pub = fleet_lib.ed25519_keygen()
forged = fleet_lib.build_bundle(fleet_lib.ed25519_signer(seed), "v1", "forged\n")
ok_forge, _ = fleet_lib.verify_bundle(verifier, forged)
hmacb = fleet_lib.build_bundle("attacker-shared-hmac", "v1", "forged\n")
ok_hmac, _ = fleet_lib.verify_bundle(verifier, hmacb)
genuine_ok = True
try:  # only the CCP/Halo-A box holds the private seed
    signer = fleet_lib.signer_from_env()
    genuine = fleet_lib.build_bundle(signer, "v1", "genuine\n")
    genuine_ok, _ = fleet_lib.verify_bundle(verifier, genuine)
except Exception:  # noqa: BLE001 - public-only box: nothing to prove here
    genuine_ok = True
sys.exit(0 if (not ok_forge and not ok_hmac and genuine_ok) else 2)
PY
}

# R9: scrape GET /metrics (token-gated, TLS/mTLS-aware) and require the key
# counters to be present.
check_metrics_scrape() {
  PYTHONPATH="${SCRIPT_DIR}" "${PYBIN}" - <<'PY'
import os, sys, fleet_lib
url = os.environ["CCP_URL"].rstrip("/") + "/metrics"
try:
    st, body = fleet_lib.http_get_text(url, token=os.environ["FLEET_TOKEN"])
except Exception as exc:  # noqa: BLE001
    print("scrape failed: %s" % exc, file=sys.stderr); sys.exit(1)
need = ("fleet_box_version_lag", "fleet_apply_outcomes_total", "fleet_desired_version_number")
missing = [k for k in need if k not in body]
if st != 200 or missing:
    print("status=%s missing=%s" % (st, missing), file=sys.stderr); sys.exit(1)
PY
}

# R9: fleet_metrics.py must emit hot_reload_latency_seconds p50/p95 from the CCP
# audit.log (which carries the agent write->converge timer, apply_seconds).
check_latency_metrics() {
  local tmpb rc
  tmpb="$(mktemp -d "${TMPDIR:-/tmp}/verify-hardening-metrics.XXXXXX")"
  if ! cp -f "${FLEET_STATE_DIR}/ccp/audit.log" "${tmpb}/audit.log" 2>/dev/null; then
    rm -rf "${tmpb}"; return 1
  fi
  if ! "${PYBIN}" "${SCRIPT_DIR}/fleet_metrics.py" --bundle "${tmpb}" >/dev/null 2>&1; then
    rm -rf "${tmpb}"; return 1
  fi
  METRICS_JSON="${tmpb}/metrics.json" "${PYBIN}" - <<'PY'
import json, os, sys
try:
    m = json.load(open(os.environ["METRICS_JSON"], encoding="utf-8"))
except Exception:  # noqa: BLE001
    sys.exit(1)
lat = m.get("hot_reload_latency_seconds") or {}
sys.exit(0 if ("p50_seconds" in lat and "p95_seconds" in lat) else 1)
PY
  rc=$?
  rm -rf "${tmpb}"
  return "${rc}"
}

# C2: replicate the live CCP state dir with ccp-standby-sync.sh (local mode) and
# confirm a fresh CCPState restores the same latest desired version.
check_standby_dry() {
  local dst before after
  dst="$(mktemp -d "${TMPDIR:-/tmp}/verify-hardening-standby.XXXXXX")"
  before="$(fctl desired-version)"
  if ! SYNC_ONCE=1 STANDBY_HOST="" CCP_STATE_DIR="${FLEET_STATE_DIR}/ccp" \
        STANDBY_STATE_DIR="${dst}/ccp" bash "${SCRIPT_DIR}/ccp-standby-sync.sh" >/dev/null 2>&1; then
    rm -rf "${dst}"; return 1
  fi
  after="$(CCP_RESTORE_DIR="${dst}/ccp" PYTHONPATH="${SCRIPT_DIR}" "${PYBIN}" - <<'PY'
import os, ccp_server
s = ccp_server.CCPState("verify", "verify", os.environ["CCP_RESTORE_DIR"])
print(s.version)
PY
)"
  rm -rf "${dst}"
  [ -n "${before}" ] && [ "${after}" = "${before}" ]
}

# ===========================================================================
echo "== verify-hardening (mode=${FLEET_MODE}, sign=${FLEET_SIGN_MODE}, ccp=${CCP_SCHEME}, boxes=${FLEET_BOXES}) =="

# Snapshot desired up front (best-effort); rollback/standby depend on it.
if save_desired; then
  :
else
  echo "WARN: could not snapshot the current desired config; some checks may skip" >&2
fi

# --- R1: drift-heal on the gateway (opt-in; safe -- a comment is valid config) --
if [ "${FLEET_VERIFY_DRIFT_ON_GATEWAY:-0}" != "1" ]; then
  skip "R1 drift-heal on gateway (set FLEET_VERIFY_DRIFT_ON_GATEWAY=1 to enable)"
elif [ ! -f "${FLEET_STATE_DIR}/gateway/config.yaml" ]; then
  skip "R1 drift-heal on gateway (no bind-mounted gateway config; gateway mode only)"
elif check_drift_heal; then
  pass "R1 drift-heal on gateway (out-of-band comment reverted via /config/hash)"
else
  fail "R1 drift-heal on gateway (agent did not revert the out-of-band edit in time)"
fi

# --- R8: auto-rollback on a config the real router rejects ---------------------
if [ "${FLEET_MODE}" != "gateway" ]; then
  skip "auto-rollback R8 (needs a real router that rejects a bad reload; gateway mode only)"
elif [ ! -s "${ORIG_CFG}" ]; then
  skip "auto-rollback R8 (could not snapshot the current desired config)"
elif check_auto_rollback; then
  pass "auto-rollback R8 (bad config -> .bak restored, rolled_back, gateway still serving)"
else
  fail "auto-rollback R8 (no rolled_back outcome or the gateway stopped serving)"
fi

# --- R6: CCP restart durability ----------------------------------------------
if [ ! -f "${FLEET_STATE_DIR}/ccp.pid" ]; then
  skip "CCP restart durability R6 (no local ccp.pid; CCP not managed on this box)"
elif check_ccp_durability; then
  pass "CCP restart durability R6 (GET /fleet/desired keeps last version+hash, no v1 reset)"
else
  fail "CCP restart durability R6 (desired changed or the CCP did not restart cleanly)"
fi

# --- R4/R5/C1: Ed25519 + TLS/mTLS converge, and forge/downgrade rejected ------
if [ "${FLEET_SIGN_MODE}" != "ed25519" ]; then
  skip "Ed25519+TLS/mTLS R4/R5/C1 (set FLEET_SIGN_MODE=ed25519 + keys, https CCP for mTLS)"
else
  if fctl wait-converged --boxes "${FLEET_BOXES}" --timeout 90 >/dev/null 2>&1; then
    pass "Ed25519 fleet converges over ${CCP_SCHEME} (R4/R5/C1: boxes=${FLEET_BOXES})"
  else
    fail "Ed25519 fleet did not converge over ${CCP_SCHEME} (boxes=${FLEET_BOXES})"
  fi
  if check_forge_reject; then
    pass "Ed25519 forge + HMAC-downgrade rejected by the deployed public key (R4)"
  else
    fail "Ed25519 forge/HMAC-downgrade NOT rejected (verifier misconfigured?)"
  fi
fi

# --- R9: /metrics scrape + p50/p95 hot-reload latency -------------------------
if check_metrics_scrape; then
  pass "metrics R9: GET /metrics exposes version-lag + outcome counters (token-gated)"
else
  fail "metrics R9: GET /metrics scrape failed or is missing counters"
fi
AUDIT_LOG="${FLEET_STATE_DIR}/ccp/audit.log"
if [ ! -f "${AUDIT_LOG}" ]; then
  skip "metrics R9 p50/p95 (no ${AUDIT_LOG} yet)"
elif ! grep -q apply_seconds "${AUDIT_LOG}" 2>/dev/null; then
  skip "metrics R9 p50/p95 (no apply_seconds samples yet; drive an edit to converge first)"
elif check_latency_metrics; then
  pass "metrics R9: fleet_metrics.py emits hot_reload_latency_seconds p50/p95 from audit.log"
else
  fail "metrics R9: fleet_metrics.py did not emit p50/p95 latency"
fi

# --- R7: N-box convergence (only meaningful with >2 boxes) --------------------
BOX_COUNT="$(awk -F, '{c=0; for (i=1;i<=NF;i++) if ($i != "") c++; print c}' <<<"${FLEET_BOXES}")"
if [ "${BOX_COUNT}" -le 2 ]; then
  skip "N-box R7 (${BOX_COUNT} box(es); needs >2 via fleet.hosts / FLEET_BOXES)"
elif fctl wait-converged --boxes "${FLEET_BOXES}" --timeout 150 >/dev/null 2>&1; then
  pass "N-box R7 (all ${BOX_COUNT} boxes converged: ${FLEET_BOXES})"
else
  fail "N-box R7 (not all ${BOX_COUNT} boxes converged: ${FLEET_BOXES})"
fi

# --- C2: warm-standby dry-run (replicate -> fresh CCPState restores latest) ---
if [ "${FLEET_VERIFY_STANDBY:-0}" != "1" ]; then
  skip "warm-standby dry-run C2 (set FLEET_VERIFY_STANDBY=1; full promotion drill is in the runbook)"
elif [ ! -d "${FLEET_STATE_DIR}/ccp/desired" ]; then
  skip "warm-standby dry-run C2 (no local CCP state dir at ${FLEET_STATE_DIR}/ccp)"
elif check_standby_dry; then
  pass "warm-standby dry-run C2 (ccp-standby-sync.sh replica -> fresh CCPState restores latest)"
else
  fail "warm-standby dry-run C2 (sync or restore version mismatch)"
fi

# --- summary -----------------------------------------------------------------
echo "== verify-hardening summary: ${PASS_N} passed, ${FAIL_N} failed, ${SKIP_N} skipped =="
if [ "${FAIL}" = 0 ]; then
  echo "ALL VERIFY-HARDENING CHECKS PASSED (${SKIP_N} skipped as not-applicable)"
else
  echo "SOME VERIFY-HARDENING CHECKS FAILED" >&2
fi
exit "${FAIL}"
