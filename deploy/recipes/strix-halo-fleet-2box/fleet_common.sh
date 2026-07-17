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

# --- Remote mTLS/Ed25519 staging convention (single source of truth) ----------
# run-hardware-validation.sh stage_certs() copies each remote box's material to
# these HOME-relative locations; deploy-fleet-2box.sh remote_agent_env() forwards
# the matching paths (plus that box's OWN client cert/key) to the box's agent.
# Both scripts source THIS file, so the staged locations and the forwarded paths
# cannot drift. The leading '~' is intentional and must stay UNEXPANDED locally:
# it is forwarded verbatim and expands to each REMOTE's own $HOME (assignment-
# position tilde expansion on the remote shell).
# shellcheck disable=SC2088
FLEET_REMOTE_KEYS_DIR="${FLEET_REMOTE_KEYS_DIR:-~/keys}"
# shellcheck disable=SC2088
FLEET_REMOTE_MTLS_DIR="${FLEET_REMOTE_MTLS_DIR:-~/mtls-certs}"

# Home-relative remote paths for the agent-side path vars, consumed by
# deploy-fleet-2box.sh remote_agent_env(). The '~' expands on the remote box.
fleet_remote_ed25519_pub() { printf '%s/ccp_ed25519.pub' "${FLEET_REMOTE_KEYS_DIR}"; }
fleet_remote_tls_ca()      { printf '%s/ca-cert.pem'      "${FLEET_REMOTE_MTLS_DIR}"; }
fleet_remote_client_cert() { printf '%s/%s-client-cert.pem' "${FLEET_REMOTE_MTLS_DIR}" "${1:?fleet_remote_client_cert: box id required}"; }
fleet_remote_client_key()  { printf '%s/%s-client-key.pem'  "${FLEET_REMOTE_MTLS_DIR}" "${1:?fleet_remote_client_key: box id required}"; }
