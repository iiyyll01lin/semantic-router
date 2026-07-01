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

# Idempotent restart. Stop any prior agent AND any prior mock router on this
# box's port -- gateway mode must also clear a leftover mock router so the real
# gateway can bind the same host API port (8080).
fleet_stop_pidfile "${AGENT_PIDFILE}"
fleet_stop_pidfile "${ROUTER_PIDFILE}"

if [ "${FLEET_MODE}" = "mock" ]; then
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
  echo "==> [${BOX_ID}] gateway mode: starting a real vllm-sr gateway"
  GATEWAY_CONFIG="${GATEWAY_CONFIG:-${FLEET_STATE_DIR}/gateway/config.yaml}"
  # STRIX_POC_DIR (optional) lets the orchestrator point a shipped copy of this
  # script at the repo's strix-halo-poc assets on a bare Halo-B. Export it rather
  # than passing `${STRIX_POC_DIR:+STRIX_POC_DIR=...}` as an inline env prefix: an
  # assignment produced by an expansion is NOT honored as an assignment by bash --
  # it becomes the command name and (because the value has a '/') fails with
  # "No such file or directory" (exit 127). gateway-bring-up.sh reads it from env.
  [ -n "${STRIX_POC_DIR:-}" ] && export STRIX_POC_DIR
  [ -n "${VLLM_SR_BIN:-}" ] && export VLLM_SR_BIN
  GATEWAY_CONFIG="${GATEWAY_CONFIG}" ROUTER_PORT="${ROUTER_PORT}" FLEET_STATE_DIR="${FLEET_STATE_DIR}" \
    bash "${SCRIPT_DIR}/gateway-bring-up.sh"
  # The agent manages the gateway's bind-mounted source config (GET /config/hash
  # reads it; an external write triggers the router's fsnotify hot-reload).
  CONFIG_FILE="${GATEWAY_CONFIG}"
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
