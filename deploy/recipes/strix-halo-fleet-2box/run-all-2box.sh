#!/usr/bin/env bash
#
# ONE-SHOT, hands-off runner: deploy + verify + (non-interactive) demo across
# BOTH Strix Halo boxes, capturing a complete log bundle for offline review.
#
# Use exactly the same env as deploy-fleet-2box.sh, e.g.:
#   HALO_A_IP=10.0.0.1 HALO_B_IP=10.0.0.2 HALO_B_SSH=user@10.0.0.2 \
#   HALO_B_REPO=/home/user/semantic-router FLEET_MODE=gateway \
#     bash run-all-2box.sh
#
# Win or lose, every relevant log lands in one directory you can share. Exit code
# is the deploy/verify result. Optional: SKIP_DEMO=1 to skip the demo step.
#
set -uo pipefail   # deliberately NOT -e: always reach the log-collection step
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"
REMOTE_STATE="\${TMPDIR:-/tmp}/vllm-sr-fleet"

RUN_DIR="${FLEET_STATE_DIR}/run-$(date +%Y%m%d-%H%M%S)"
mkdir -p "${RUN_DIR}"
MAIN_LOG="${RUN_DIR}/run-all.log"
echo "==> [run-all] mode=${FLEET_MODE:-mock}; full transcript -> ${MAIN_LOG}"

echo "==> [run-all] STEP 1/2: deploy + verify" | tee -a "${MAIN_LOG}"
bash "${SCRIPT_DIR}/deploy-fleet-2box.sh" 2>&1 | tee -a "${MAIN_LOG}"
rc="${PIPESTATUS[0]}"

if [ "${rc}" -eq 0 ] && [ "${SKIP_DEMO:-}" != "1" ]; then
  echo "==> [run-all] STEP 2/2: demo (non-interactive)" | tee -a "${MAIN_LOG}"
  bash "${SCRIPT_DIR}/demo-fleet.sh" </dev/null 2>&1 | tee -a "${MAIN_LOG}"
elif [ "${rc}" -ne 0 ]; then
  echo "==> [run-all] deploy/verify failed (rc=${rc}); skipping demo" | tee -a "${MAIN_LOG}"
fi

# --- collect logs (best-effort, regardless of outcome) ----------------------
echo "==> [run-all] collecting logs into ${RUN_DIR}"
ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
for f in fleet.env ccp.log halo-a-agent.log halo-a-router.log; do
  cp -f "${FLEET_STATE_DIR}/${f}" "${RUN_DIR}/" 2>/dev/null || true
done

# Pull Halo-B logs + capture a final status/audit snapshot (best-effort).
if [ -f "${ENV_FILE}" ]; then
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
fi
if [ -n "${HALO_B_SSH:-}" ]; then
  SSH_OPTS=()
  [ -n "${HALO_B_SSH_KEY:-}" ] && SSH_OPTS+=(-i "${HALO_B_SSH_KEY}")
  [ -n "${HALO_B_SSH_PORT:-}" ] && SSH_OPTS+=(-p "${HALO_B_SSH_PORT}")
  ssh "${SSH_OPTS[@]}" "${HALO_B_SSH}" "cat ${REMOTE_STATE}/halo-b-agent.log" \
    >"${RUN_DIR}/halo-b-agent.log" 2>/dev/null || true
fi
PYBIN="$(fleet_pybin)"
fctl() { CCP_URL="${CCP_URL:-http://localhost:9300}" FLEET_TOKEN="${FLEET_TOKEN:-}" \
         "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" "$@"; }
fctl status >"${RUN_DIR}/fleet-status.txt" 2>&1 || true
fctl audit  >"${RUN_DIR}/fleet-audit.txt"  2>&1 || true

echo
echo "=============================================================="
if [ "${rc}" -eq 0 ]; then
  echo "PASS: deploy + verify completed (mode=${FLEET_MODE:-mock})."
else
  echo "FAIL: deploy/verify returned ${rc}."
fi
echo "Log bundle (share this whole directory if anything failed):"
echo "  ${RUN_DIR}"
ls -1 "${RUN_DIR}" 2>/dev/null | sed 's/^/    /'
echo "=============================================================="
exit "${rc}"
