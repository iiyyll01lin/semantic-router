#!/usr/bin/env bash
#
# Start the Central Control Plane (CCP) on this box (Halo-A). The CCP versions,
# signs, and serves the desired router config and keeps the central audit log.
# It is a pure-stdlib Python process (no container needed).
#
# Required env: FLEET_TOKEN, and FLEET_SIGNING_KEY (HMAC mode; not needed when
#               FLEET_SIGN_MODE=ed25519).
# Optional env: CCP_PORT (9300), CCP_INIT_CONFIG (sample-desired-config.yaml),
#               FLEET_STATE_DIR, CCP_HOST (bind; default 0.0.0.0 for the fleet
#               topology so remotes can reach it -- override to tighten).
# Optional OPT-IN hardening (safe fallback = today's HMAC + plain HTTP; see
# docs/security-hardening.md), all forwarded to ccp_server.py when set:
#   FLEET_SIGN_MODE=ed25519 + FLEET_ED25519_SECRET or FLEET_ED25519_SECRET_FILE
#       -> asymmetric signing (agents verify with the PUBLIC key only)
#   FLEET_BUNDLE_TS=1                 -> stamp bundles with a freshness timestamp
#   CCP_TLS_CERT + CCP_TLS_KEY        -> serve HTTPS (optional CCP_TLS_CLIENT_CA = mTLS)
#   CCP_AUDIT_MEMORY_MAX             -> bounded in-memory audit view size
# When the CCP requires client certs (mTLS via CCP_TLS_CLIENT_CA), also set
#   FLEET_TLS_CLIENT_CERT + FLEET_TLS_CLIENT_KEY (C1) so the LOCAL liveness probe
#   (and any co-located agent/fleetctl) can complete the TLS handshake.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"

: "${FLEET_TOKEN:?set FLEET_TOKEN}"
# HMAC mode needs the shared signing key; Ed25519 mode uses a private seed instead.
if [ "${FLEET_SIGN_MODE:-hmac}" != "ed25519" ]; then
  : "${FLEET_SIGNING_KEY:?set FLEET_SIGNING_KEY (or FLEET_SIGN_MODE=ed25519 with an Ed25519 key)}"
fi
INIT_CONFIG="${CCP_INIT_CONFIG:-${SCRIPT_DIR}/sample-desired-config.yaml}"
PIDFILE="${FLEET_STATE_DIR}/ccp.pid"
LOGFILE="${FLEET_STATE_DIR}/ccp.log"
PYBIN="$(fleet_pybin)"

# Idempotent: stop any previous CCP before starting a fresh one.
fleet_stop_pidfile "${PIDFILE}"

# Default bind stays 0.0.0.0 so a remote box can pull from Halo-A AND the local
# agent/health-probe can hit localhost. Override CCP_HOST to tighten. (The bare
# `python3 ccp_server.py` default is loopback; this fleet topology opts into
# exposure and should pair it with TLS + a strong token.)
CCP_BIND_HOST="${CCP_HOST:-0.0.0.0}"
SCHEME="http"
if [ -n "${CCP_TLS_CERT:-}" ] && [ -n "${CCP_TLS_KEY:-}" ]; then SCHEME="https"; fi
echo "==> Starting CCP on ${SCHEME}://${CCP_BIND_HOST}:${CCP_PORT} (init=${INIT_CONFIG}, sign=${FLEET_SIGN_MODE:-hmac})"
CCP_HOST="${CCP_BIND_HOST}" \
CCP_PORT="${CCP_PORT}" \
FLEET_SIGNING_KEY="${FLEET_SIGNING_KEY:-}" \
FLEET_TOKEN="${FLEET_TOKEN}" \
CCP_STATE_DIR="${FLEET_STATE_DIR}/ccp" \
CCP_INIT_CONFIG="${INIT_CONFIG}" \
FLEET_SIGN_MODE="${FLEET_SIGN_MODE:-}" \
FLEET_ED25519_SECRET="${FLEET_ED25519_SECRET:-}" \
FLEET_ED25519_SECRET_FILE="${FLEET_ED25519_SECRET_FILE:-}" \
FLEET_BUNDLE_TS="${FLEET_BUNDLE_TS:-}" \
CCP_TLS_CERT="${CCP_TLS_CERT:-}" \
CCP_TLS_KEY="${CCP_TLS_KEY:-}" \
CCP_TLS_CLIENT_CA="${CCP_TLS_CLIENT_CA:-}" \
CCP_AUDIT_MEMORY_MAX="${CCP_AUDIT_MEMORY_MAX:-}" \
FLEET_TLS_CLIENT_CERT="${FLEET_TLS_CLIENT_CERT:-}" \
FLEET_TLS_CLIENT_KEY="${FLEET_TLS_CLIENT_KEY:-}" \
  nohup "${PYBIN}" "${SCRIPT_DIR}/ccp_server.py" >"${LOGFILE}" 2>&1 &
echo $! >"${PIDFILE}"

# Health probe. fleet_wait_http uses `curl -fsS`; for a self-signed HTTPS CCP we
# add -k here (only for the local liveness wait) so TLS bring-up is not blocked.
_ccp_healthy() {
  if [ "${SCHEME}" = "https" ]; then
    # If the CCP requires client certs (mTLS via CCP_TLS_CLIENT_CA), the local
    # liveness probe must present one too or the TLS handshake is refused before
    # /healthz is ever reached. (-k already skips server-cert verification for the
    # self-signed local wait.)
    local cc=()
    if [ -n "${FLEET_TLS_CLIENT_CERT:-}" ] && [ -n "${FLEET_TLS_CLIENT_KEY:-}" ]; then
      cc=(--cert "${FLEET_TLS_CLIENT_CERT}" --key "${FLEET_TLS_CLIENT_KEY}")
    fi
    curl -fsSk ${cc[@]+"${cc[@]}"} "https://localhost:${CCP_PORT}/healthz" >/dev/null 2>&1
  else
    curl -fsS "http://localhost:${CCP_PORT}/healthz" >/dev/null 2>&1
  fi
}
_ccp_up=0
for _i in $(seq 1 30); do if _ccp_healthy; then _ccp_up=1; break; fi; sleep 1; done
if [ "${_ccp_up}" = "1" ]; then
  echo "    CCP is up (pid $(cat "${PIDFILE}"), log ${LOGFILE})"
else
  echo "ERROR: CCP did not become healthy on :${CCP_PORT}; see ${LOGFILE}" >&2
  exit 1
fi
