#!/usr/bin/env bash
#
# Headless PASS/FAIL verification against a LIVE fleet brought up by
# deploy-fleet-2box.sh. Exercises the PL-0036 exit criteria end to end:
#   1. edit-once converge   - one CCP edit converges BOTH boxes
#   2. drift self-heal      - an out-of-band local edit is reverted (mock mode)
#   3. fleet rollback       - desired<-prior content converges both boxes back
#   4. central audit        - the CCP recorded the applies
#
# Signed-bundle tamper rejection and the hot-reload-not-restart property are
# proven offline by verify_local.py (no hardware needed).
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
ORIG_CFG="${SCRIPT_DIR}/sample-desired-config.yaml"
FAIL=0

fctl() { "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" "$@"; }

# run_check "name" command...  (set -e safe: the command runs in an if condition)
run_check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "[PASS] ${name}"
  else
    echo "[FAIL] ${name}"
    FAIL=1
  fi
}

echo "== verify-fleet (mode=${FLEET_MODE:-mock}) =="

# 1. edit-once converge -------------------------------------------------------
V2="${FLEET_STATE_DIR}/desired-ruleB.yaml"
sed 's/rule-set A/rule-set B (edited once at the CCP)/' "${ORIG_CFG}" >"${V2}"
fctl set-desired "${V2}" >/dev/null
run_check "edit-once converges both boxes" \
  fctl wait-converged --boxes halo-a,halo-b --timeout 60

# 2. drift self-heal (mock mode can corrupt the local config file) ------------
if [ "${FLEET_MODE:-mock}" = "mock" ]; then
  printf 'version: v0.3\n# UNAUTHORIZED local edit\n' >"${FLEET_STATE_DIR}/halo-a-config.yaml"
  run_check "drift self-heal (out-of-band edit reverted to desired)" \
    fctl wait-converged --boxes halo-a --timeout 30
else
  echo "[SKIP] drift self-heal (gateway mode; do not corrupt a live gateway config)"
fi

# 3. fleet rollback -----------------------------------------------------------
fctl set-desired "${ORIG_CFG}" >/dev/null
run_check "fleet rollback (desired<-prior content converges both)" \
  fctl wait-converged --boxes halo-a,halo-b --timeout 60

# 4. central audit ------------------------------------------------------------
if [ "$(fctl audit | wc -l | tr -d ' ')" -gt 0 ]; then
  echo "[PASS] central audit log captured applies"
else
  echo "[FAIL] central audit log captured applies"
  FAIL=1
fi

echo "== fleet status =="
fctl status || true
if [ "${FAIL}" = 0 ]; then echo "ALL VERIFY CHECKS PASSED"; else echo "SOME VERIFY CHECKS FAILED" >&2; fi
exit "${FAIL}"
