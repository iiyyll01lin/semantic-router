#!/usr/bin/env bash
#
# Bring up ONE edge node: a router on :ROUTER_PORT plus a co-located pull agent.
# Used both locally on Halo-A and (scp'd) on Halo-B by deploy-fleet-2box.sh.
#
# FLEET_MODE=mock (default): start mock_router.py so the fan-out can be verified
#   without ROCm. FLEET_MODE=gateway: expect a real `vllm-sr serve` router to be
#   listening on :ROUTER_PORT already (see README), and only start the agent.
#
# Required env: BOX_ID, CCP_URL, FLEET_SIGNING_KEY, FLEET_TOKEN
# Optional env: FLEET_MODE (mock), ROUTER_PORT (8080), CONFIG_FILE, POLL_INTERVAL
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"

: "${BOX_ID:?set BOX_ID}"
: "${CCP_URL:?set CCP_URL}"
: "${FLEET_SIGNING_KEY:?set FLEET_SIGNING_KEY}"
: "${FLEET_TOKEN:?set FLEET_TOKEN}"
FLEET_MODE="${FLEET_MODE:-mock}"
CONFIG_FILE="${CONFIG_FILE:-${FLEET_STATE_DIR}/${BOX_ID}-config.yaml}"
ROUTER_PIDFILE="${FLEET_STATE_DIR}/${BOX_ID}-router.pid"
AGENT_PIDFILE="${FLEET_STATE_DIR}/${BOX_ID}-agent.pid"
PYBIN="$(fleet_pybin)"

# Idempotent restart.
fleet_stop_pidfile "${AGENT_PIDFILE}"

if [ "${FLEET_MODE}" = "mock" ]; then
  fleet_stop_pidfile "${ROUTER_PIDFILE}"
  if [ ! -f "${CONFIG_FILE}" ]; then
    printf 'version: v0.3\n# %s baseline (pre-convergence)\n' "${BOX_ID}" >"${CONFIG_FILE}"
  fi
  echo "==> [${BOX_ID}] starting mock router on :${ROUTER_PORT} (config ${CONFIG_FILE})"
  MOCK_ROUTER_HOST="0.0.0.0" \
  MOCK_ROUTER_PORT="${ROUTER_PORT}" \
  MOCK_ROUTER_CONFIG="${CONFIG_FILE}" \
    nohup "${PYBIN}" "${SCRIPT_DIR}/mock_router.py" \
      >"${FLEET_STATE_DIR}/${BOX_ID}-router.log" 2>&1 &
  echo $! >"${ROUTER_PIDFILE}"
  if ! fleet_wait_http "http://localhost:${ROUTER_PORT}/healthz" 30; then
    echo "ERROR: [${BOX_ID}] mock router did not come up on :${ROUTER_PORT}" >&2
    exit 1
  fi
else
  echo "==> [${BOX_ID}] gateway mode: expecting a real router on :${ROUTER_PORT}"
  if ! fleet_wait_http "http://localhost:${ROUTER_PORT}/config/hash" 30; then
    echo "ERROR: [${BOX_ID}] no router answering GET /config/hash on :${ROUTER_PORT}." >&2
    echo "       Start it first (vllm-sr serve --config ${CONFIG_FILE} ...); see README." >&2
    exit 1
  fi
  : "${CONFIG_FILE:?gateway mode needs CONFIG_FILE pointing at the served config}"
fi

echo "==> [${BOX_ID}] starting pull agent -> CCP ${CCP_URL}"
CCP_URL="${CCP_URL}" \
ROUTER_API="http://localhost:${ROUTER_PORT}" \
CONFIG_FILE="${CONFIG_FILE}" \
FLEET_SIGNING_KEY="${FLEET_SIGNING_KEY}" \
FLEET_TOKEN="${FLEET_TOKEN}" \
BOX_ID="${BOX_ID}" \
POLL_INTERVAL="${POLL_INTERVAL}" \
  nohup "${PYBIN}" "${SCRIPT_DIR}/fleet_agent.py" \
    >"${FLEET_STATE_DIR}/${BOX_ID}-agent.log" 2>&1 &
echo $! >"${AGENT_PIDFILE}"
echo "    [${BOX_ID}] up (mode=${FLEET_MODE}, router:${ROUTER_PORT}, agent pid $(cat "${AGENT_PIDFILE}"))"
