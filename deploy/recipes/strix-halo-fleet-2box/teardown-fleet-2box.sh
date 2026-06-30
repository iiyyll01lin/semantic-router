#!/usr/bin/env bash
#
# Tear down the fleet brought up by deploy-fleet-2box.sh: stop the CCP, the
# Halo-A router+agent, and (over SSH) the Halo-B router+agent, and remove the
# shipped recipe files on Halo-B. Idempotent.
#
# Reuses ${FLEET_STATE_DIR}/fleet.env for HALO_B_SSH when present; HALO_B_SSH can
# also be passed via the environment.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"
ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
if [ -f "${ENV_FILE}" ]; then
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
fi
REMOTE_DIR="~/.vllm-sr-fleet-2box"
REMOTE_STATE='${TMPDIR:-/tmp}/vllm-sr-fleet'

echo "==> Stopping local (Halo-A) CCP, router, and agent"
fleet_stop_pidfile "${FLEET_STATE_DIR}/halo-a-agent.pid"
fleet_stop_pidfile "${FLEET_STATE_DIR}/halo-a-router.pid"
fleet_stop_pidfile "${FLEET_STATE_DIR}/ccp.pid"

if [ -n "${HALO_B_SSH:-}" ]; then
  echo "==> Stopping Halo-B router and agent over SSH (${HALO_B_SSH})"
  SSH_OPTS=()
  [ -n "${HALO_B_SSH_KEY:-}" ] && SSH_OPTS+=(-i "${HALO_B_SSH_KEY}")
  [ -n "${HALO_B_SSH_PORT:-}" ] && SSH_OPTS+=(-p "${HALO_B_SSH_PORT}")
  ssh "${SSH_OPTS[@]}" "${HALO_B_SSH}" \
    "for f in halo-b-agent halo-b-router; do p=${REMOTE_STATE}/\$f.pid; [ -f \"\$p\" ] && kill \"\$(cat \"\$p\")\" 2>/dev/null || true; rm -f \"\$p\"; done; rm -rf ${REMOTE_DIR}" \
    || echo "    (warning: could not reach Halo-B; stop it manually if it is still running)"
else
  echo "==> HALO_B_SSH not set; skipping remote teardown (set it to clean Halo-B)"
fi

echo "==> Teardown complete. State dir retained for logs: ${FLEET_STATE_DIR}"
