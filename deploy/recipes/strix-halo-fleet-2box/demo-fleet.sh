#!/usr/bin/env bash
#
# Narrated demo of the edge-fleet config control plane against a live fleet
# (run deploy-fleet-2box.sh first). Shows: edit one rule once at the CCP, watch
# BOTH Strix Halo boxes hot-reload and converge, show the central audit log,
# then roll the fleet back.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"
ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
[ -f "${ENV_FILE}" ] || { echo "ERROR: run deploy-fleet-2box.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source "${ENV_FILE}"
PYBIN="$(fleet_pybin)"
# In gateway mode, edit/roll a STABLE SNAPSHOT of the REAL rendered gateway
# config -- never the mock sample (it would replace the real router's valid
# config) and never the live file directly (convergence overwrites it).
if [ "${FLEET_MODE:-mock}" = "gateway" ]; then
  ORIG_CFG="${FLEET_STATE_DIR}/demo-orig-gateway.yaml"
  cp -f "${FLEET_STATE_DIR}/gateway/config.yaml" "${ORIG_CFG}"
else
  ORIG_CFG="${SCRIPT_DIR}/sample-desired-config.yaml"
fi
fctl() { "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" "$@"; }
# Interactive narration when run by a human; in a non-interactive run (no tty on
# stdin, e.g. via run-all-2box.sh) print the cue and keep going instead of blocking.
pause() {
  echo
  if [ -t 0 ]; then
    read -r -p ">> ${1:-press Enter to continue} " _ || true
  else
    echo ">> ${1:-continuing} (non-interactive)"
  fi
  echo
}

echo "=============================================================="
echo " Edge-fleet config control plane demo (pull mode, 2x Strix Halo)"
echo "=============================================================="
echo "Both boxes pull signed config from ONE central control plane."
pause "Show the current fleet convergence (Enter)"
fctl status

pause "Now EDIT ONE RULE once at the CCP and watch both boxes converge (Enter)"
DEMO="${FLEET_STATE_DIR}/demo-desired.yaml"
sed 's/rule-set A.*/rule-set DEMO (changed once, centrally)/' "${ORIG_CFG}" >"${DEMO}"
echo "+ fleetctl set-desired (rule-set DEMO)"
fctl set-desired "${DEMO}"
echo "+ waiting for halo-a and halo-b to hot-reload and converge ..."
fctl wait-converged --boxes halo-a,halo-b --timeout 60
fctl status

pause "Show the CENTRAL AUDIT LOG (who applied what, when) (Enter)"
fctl audit

pause "Roll the whole fleet BACK with one edit (Enter)"
fctl set-desired "${ORIG_CFG}"
fctl wait-converged --boxes halo-a,halo-b --timeout 60
fctl status
echo
echo "Done. One central edit converged the whole fleet, with a full audit trail."
