#!/usr/bin/env bash
#
# strix-halo-fleet-2box: ONE-CLICK pull-mode fleet config control plane.
#
# Run on Halo-A. Stands up the Central Control Plane (CCP) on Halo-A, an edge
# node (router + pull agent) on Halo-A, and an edge node on a BARE Halo-B over
# SSH/scp, then waits until BOTH boxes have converged to the CCP's desired
# config. Implements PL-0036 phases P1-P4 (fan-out, audit, signing, pull agent).
#
# Default FLEET_MODE=mock runs a stdlib mock router on each box so the whole
# fan-out is verifiable WITHOUT ROCm. Set FLEET_MODE=gateway to point the agents
# at real `vllm-sr serve` routers you started on each box (see README).
#
# Required env:
#   HALO_A_IP    address of THIS box (Halo-A) reachable FROM Halo-B (CCP URL).
#   HALO_B_IP    address of Halo-B (used for the convergence reachability note).
#   HALO_B_SSH   user@host control address for Halo-B.
# Optional env:
#   HALO_B_SSH_PORT, HALO_B_SSH_KEY, FLEET_MODE (mock), CCP_PORT (9300),
#   ROUTER_PORT (8080), POLL_INTERVAL (3), SKIP_VERIFY=1
#
# Usage:
#   HALO_A_IP=192.0.2.10 HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 \
#     bash deploy-fleet-2box.sh
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"

FLEET_MODE="${FLEET_MODE:-mock}"
PYBIN="$(fleet_pybin)"
REMOTE_DIR="~/.vllm-sr-fleet-2box"
REMOTE_STATE="\${TMPDIR:-/tmp}/vllm-sr-fleet"

SSH_CTRL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vllm-sr-fleet-ssh.XXXXXX")"
SSH_CTRL_PATH="${SSH_CTRL_DIR}/cm-%r@%h:%p"
SSH_BASE_OPTS=()
SSH_PORT_OPTS=()
SCP_PORT_OPTS=()

cleanup() {
  if [ -n "${HALO_B_SSH:-}" ] && [ "${#SSH_BASE_OPTS[@]}" -gt 0 ]; then
    ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" -O exit "${HALO_B_SSH}" >/dev/null 2>&1 || true
  fi
  rm -rf "${SSH_CTRL_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> [1/6] Preflight on Halo-A"
: "${HALO_A_IP:?set HALO_A_IP (this box address, reachable from Halo-B)}"
: "${HALO_B_IP:?set HALO_B_IP}"
: "${HALO_B_SSH:?set HALO_B_SSH (user@host for Halo-B)}"
for bin in "${PYBIN}" ssh scp curl; do
  command -v "${bin}" >/dev/null 2>&1 || { echo "ERROR: '${bin}' not found on Halo-A" >&2; exit 1; }
done

# Generate the per-deployment signing key + token (the CCP<->agent trust boundary).
FLEET_SIGNING_KEY="$("${PYBIN}" -c 'import secrets; print(secrets.token_hex(32))')"
FLEET_TOKEN="$("${PYBIN}" -c 'import secrets; print(secrets.token_hex(32))')"
CCP_URL_LOCAL="http://localhost:${CCP_PORT}"
CCP_URL_REMOTE="http://${HALO_A_IP}:${CCP_PORT}"

# Persist the deployment env so verify/demo/teardown can reuse it.
ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
cat >"${ENV_FILE}" <<EOF
export FLEET_SIGNING_KEY=${FLEET_SIGNING_KEY}
export FLEET_TOKEN=${FLEET_TOKEN}
export CCP_URL=${CCP_URL_LOCAL}
export CCP_PORT=${CCP_PORT}
export ROUTER_PORT=${ROUTER_PORT}
export FLEET_MODE=${FLEET_MODE}
export HALO_A_IP=${HALO_A_IP}
export HALO_B_IP=${HALO_B_IP}
export HALO_B_SSH=${HALO_B_SSH}
EOF
echo "    mode=${FLEET_MODE}  CCP=:${CCP_PORT}  state=${FLEET_STATE_DIR}  env=${ENV_FILE}"

echo "==> [2/6] Starting CCP on Halo-A"
FLEET_SIGNING_KEY="${FLEET_SIGNING_KEY}" FLEET_TOKEN="${FLEET_TOKEN}" \
CCP_PORT="${CCP_PORT}" FLEET_STATE_DIR="${FLEET_STATE_DIR}" \
  bash "${SCRIPT_DIR}/ccp-bring-up.sh"

echo "==> [3/6] Bringing up the Halo-A edge node (router + agent)"
BOX_ID="halo-a" CCP_URL="${CCP_URL_LOCAL}" FLEET_MODE="${FLEET_MODE}" \
FLEET_SIGNING_KEY="${FLEET_SIGNING_KEY}" FLEET_TOKEN="${FLEET_TOKEN}" \
ROUTER_PORT="${ROUTER_PORT}" POLL_INTERVAL="${POLL_INTERVAL}" FLEET_STATE_DIR="${FLEET_STATE_DIR}" \
  bash "${SCRIPT_DIR}/node-bring-up.sh"

echo "==> [4/6] Provisioning Halo-B over SSH (mode=${FLEET_MODE})"
SSH_BASE_OPTS=(-o ControlMaster=auto -o ControlPath="${SSH_CTRL_PATH}" -o ControlPersist=2m)
[ -n "${HALO_B_SSH_KEY:-}" ] && SSH_BASE_OPTS+=(-i "${HALO_B_SSH_KEY}")
if [ -n "${HALO_B_SSH_PORT:-}" ]; then
  SSH_PORT_OPTS=(-p "${HALO_B_SSH_PORT}")
  SCP_PORT_OPTS=(-P "${HALO_B_SSH_PORT}")
fi
if ! ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" true; then
  echo "ERROR: cannot SSH to ${HALO_B_SSH}. Install your key once:" >&2
  echo "         ssh-copy-id ${HALO_B_SSH_PORT:+-p ${HALO_B_SSH_PORT}} ${HALO_B_SSH}" >&2
  exit 1
fi
ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" "mkdir -p ${REMOTE_DIR}"
# Ship only the self-contained recipe files Halo-B needs (stdlib python + scripts).
scp "${SSH_BASE_OPTS[@]}" "${SCP_PORT_OPTS[@]}" \
  "${SCRIPT_DIR}/fleet_lib.py" "${SCRIPT_DIR}/fleet_agent.py" "${SCRIPT_DIR}/mock_router.py" \
  "${SCRIPT_DIR}/fleet_common.sh" "${SCRIPT_DIR}/node-bring-up.sh" \
  "${HALO_B_SSH}:${REMOTE_DIR}/"
echo "    starting Halo-B edge node ..."
ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" \
  "BOX_ID=halo-b CCP_URL=${CCP_URL_REMOTE} FLEET_MODE=${FLEET_MODE} \
   FLEET_SIGNING_KEY=${FLEET_SIGNING_KEY} FLEET_TOKEN=${FLEET_TOKEN} \
   ROUTER_PORT=${ROUTER_PORT} POLL_INTERVAL=${POLL_INTERVAL} FLEET_STATE_DIR=${REMOTE_STATE} \
   bash ${REMOTE_DIR}/node-bring-up.sh"

echo "==> [5/6] Waiting for both boxes to converge to the CCP desired config"
if CCP_URL="${CCP_URL_LOCAL}" FLEET_TOKEN="${FLEET_TOKEN}" \
   "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" wait-converged --boxes halo-a,halo-b --timeout 120; then
  echo "    both boxes converged."
else
  echo "ERROR: boxes did not converge in time. Inspect:" >&2
  echo "         CCP log: ${FLEET_STATE_DIR}/ccp.log" >&2
  echo "         agent logs: ${FLEET_STATE_DIR}/halo-a-agent.log ; on Halo-B: ${REMOTE_STATE}/halo-b-agent.log" >&2
  exit 1
fi

echo "==> [6/6] Done."
CCP_URL="${CCP_URL_LOCAL}" FLEET_TOKEN="${FLEET_TOKEN}" "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" status || true
if [ "${SKIP_VERIFY:-}" = "1" ]; then
  echo "    SKIP_VERIFY=1; skipping verify-fleet.sh"
else
  echo "==> Running verify-fleet.sh"
  bash "${SCRIPT_DIR}/verify-fleet.sh" || { echo "verify-fleet.sh FAILED" >&2; exit 1; }
fi

cat <<EOF

PASS: fleet is up and converged (mode=${FLEET_MODE}).
  Demo:     bash ${SCRIPT_DIR}/demo-fleet.sh
  Re-verify: bash ${SCRIPT_DIR}/verify-fleet.sh
  Teardown: HALO_B_SSH=${HALO_B_SSH} bash ${SCRIPT_DIR}/teardown-fleet-2box.sh
  Env saved: ${ENV_FILE}
EOF
