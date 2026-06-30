#!/usr/bin/env bash
#
# Start the Central Control Plane (CCP) on this box (Halo-A). The CCP versions,
# signs, and serves the desired router config and keeps the central audit log.
# It is a pure-stdlib Python process (no container needed).
#
# Required env: FLEET_SIGNING_KEY, FLEET_TOKEN
# Optional env: CCP_PORT (9300), CCP_INIT_CONFIG (sample-desired-config.yaml),
#               FLEET_STATE_DIR
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"

: "${FLEET_SIGNING_KEY:?set FLEET_SIGNING_KEY}"
: "${FLEET_TOKEN:?set FLEET_TOKEN}"
INIT_CONFIG="${CCP_INIT_CONFIG:-${SCRIPT_DIR}/sample-desired-config.yaml}"
PIDFILE="${FLEET_STATE_DIR}/ccp.pid"
LOGFILE="${FLEET_STATE_DIR}/ccp.log"
PYBIN="$(fleet_pybin)"

# Idempotent: stop any previous CCP before starting a fresh one.
fleet_stop_pidfile "${PIDFILE}"

echo "==> Starting CCP on 0.0.0.0:${CCP_PORT} (init=${INIT_CONFIG})"
CCP_HOST="0.0.0.0" \
CCP_PORT="${CCP_PORT}" \
FLEET_SIGNING_KEY="${FLEET_SIGNING_KEY}" \
FLEET_TOKEN="${FLEET_TOKEN}" \
CCP_STATE_DIR="${FLEET_STATE_DIR}/ccp" \
CCP_INIT_CONFIG="${INIT_CONFIG}" \
  nohup "${PYBIN}" "${SCRIPT_DIR}/ccp_server.py" >"${LOGFILE}" 2>&1 &
echo $! >"${PIDFILE}"

if fleet_wait_http "http://localhost:${CCP_PORT}/healthz" 30; then
  echo "    CCP is up (pid $(cat "${PIDFILE}"), log ${LOGFILE})"
else
  echo "ERROR: CCP did not become healthy on :${CCP_PORT}; see ${LOGFILE}" >&2
  exit 1
fi
