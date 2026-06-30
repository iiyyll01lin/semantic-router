# Sourced by the strix-halo-fleet-2box recipe scripts. Defines shared paths,
# the python binary, and small wait/stop helpers. Kept dependency-free.
# shellcheck shell=bash

FLEET_STATE_DIR="${FLEET_STATE_DIR:-${TMPDIR:-/tmp}/vllm-sr-fleet}"
CCP_PORT="${CCP_PORT:-9300}"
ROUTER_PORT="${ROUTER_PORT:-8080}"
POLL_INTERVAL="${POLL_INTERVAL:-3}"
mkdir -p "${FLEET_STATE_DIR}"

fleet_pybin() {
  if command -v python3 >/dev/null 2>&1; then
    echo python3
  else
    echo python
  fi
}

# fleet_wait_http URL [TRIES]
fleet_wait_http() {
  local url="$1" tries="${2:-30}" i
  for i in $(seq 1 "${tries}"); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# fleet_stop_pidfile PIDFILE
fleet_stop_pidfile() {
  local pf="$1" pid
  if [ -f "${pf}" ]; then
    pid="$(cat "${pf}" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
    rm -f "${pf}"
  fi
}
