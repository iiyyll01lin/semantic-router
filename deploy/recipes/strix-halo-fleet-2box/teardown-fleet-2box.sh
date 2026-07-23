#!/usr/bin/env bash
#
# Tear down the fleet brought up by deploy-fleet-2box.sh: stop the CCP, the
# Halo-A router+agent, and (over SSH) EVERY remote box's router+agent, and
# remove the shipped recipe files on each remote box. Idempotent.
#
# Reuses ${FLEET_STATE_DIR}/fleet.env for HALO_B_SSH / FLEET_MODE when present.
# For an N-box fleet it reads the same fleet.hosts (see fleet.hosts.example) the
# deploy used; otherwise it falls back to the single HALO_B_* box. HALO_B_SSH can
# also be passed via the environment.
#
# R10: if a shared SSH ControlMaster is active (FLEET_SSH_CONTROL_PATH), the
# remote stops reuse it (no extra password prompts). Key/port per box come from
# fleet.hosts or HALO_B_SSH_KEY / HALO_B_SSH_PORT.
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
# ~ and ${TMPDIR} are intentionally NOT expanded locally: they expand on the
# REMOTE shell (inside the ssh command string below).
# shellcheck disable=SC2088,SC2016
REMOTE_DIR="~/.vllm-sr-fleet-2box"
REMOTE_STATE='${TMPDIR:-/tmp}/vllm-sr-fleet'

echo "==> Stopping local (Halo-A) CCP, router, and agent"
fleet_stop_pidfile "${FLEET_STATE_DIR}/halo-a-agent.pid"
# Safety net: reap a local halo-a agent that outlived its pidfile. Scoped by the
# exact "--tag halo-a" argv so it only matches this box's agent and never the
# CCP (ccp_server.py) or another box's process.
command -v pkill >/dev/null 2>&1 && pkill -f "fleet_agent.py --tag halo-a" 2>/dev/null || true
fleet_stop_pidfile "${FLEET_STATE_DIR}/halo-a-router.pid"
fleet_stop_pidfile "${FLEET_STATE_DIR}/ccp.pid"
if [ "${FLEET_MODE:-mock}" = "gateway" ]; then
  echo "    gateway mode: stopping the local vllm-sr gateway containers"
  vllm-sr stop >/dev/null 2>&1 || true
fi

# --- R7: resolve the remote boxes (fleet.hosts else the single HALO_B_*) ------
FLEET_HOSTS_FILE="${FLEET_HOSTS_FILE:-${SCRIPT_DIR}/fleet.hosts}"
BOX_IDS=(); BOX_SSH=(); BOX_MODE=(); BOX_PORT=(); BOX_KEY=()
_undash() { case "${1:-}" in ""|-) printf '' ;; *) printf '%s' "$1" ;; esac; }
if [ -f "${FLEET_HOSTS_FILE}" ] && grep -qvE '^[[:space:]]*(#.*)?$' "${FLEET_HOSTS_FILE}"; then
  # shellcheck disable=SC2034  # positional read: ip/repo/rest unused in teardown
  while read -r id ssht ip mode repo port key rest; do
    [ -z "${id:-}" ] && continue
    case "${id}" in \#*) continue ;; esac
    [ -z "${ssht:-}" ] && continue
    [ "${id}" = "halo-a" ] && continue
    BOX_IDS+=("${id}"); BOX_SSH+=("${ssht}")
    BOX_MODE+=("$(_undash "${mode:-}")"); BOX_PORT+=("$(_undash "${port:-}")")
    BOX_KEY+=("$(_undash "${key:-}")")
  done < "${FLEET_HOSTS_FILE}"
elif [ -n "${HALO_B_SSH:-}" ]; then
  BOX_IDS+=("halo-b"); BOX_SSH+=("${HALO_B_SSH}")
  BOX_MODE+=("${HALO_B_MODE:-${FLEET_MODE:-mock}}"); BOX_PORT+=("${HALO_B_SSH_PORT:-}")
  BOX_KEY+=("${HALO_B_SSH_KEY:-}")
fi

if [ "${#BOX_IDS[@]}" -eq 0 ]; then
  echo "==> No remote boxes known (HALO_B_SSH unset, no fleet.hosts); skipping remote teardown"
else
  for idx in "${!BOX_IDS[@]}"; do
    id="${BOX_IDS[$idx]}"; target="${BOX_SSH[$idx]}"
    echo "==> Stopping ${id} router and agent over SSH (${target})"
    SSH_OPTS=()
    [ -n "${FLEET_SSH_CONTROL_PATH:-}" ] && SSH_OPTS+=(-o ControlMaster=auto -o ControlPath="${FLEET_SSH_CONTROL_PATH}")
    [ -n "${BOX_KEY[$idx]:-}" ] && SSH_OPTS+=(-i "${BOX_KEY[$idx]}")
    [ -n "${BOX_PORT[$idx]:-}" ] && SSH_OPTS+=(-p "${BOX_PORT[$idx]}")
    # gateway boxes also stop the vllm-sr containers (harmless no-op on a mock box)
    _bmode="${BOX_MODE[$idx]:-}"; [ -z "${_bmode}" ] && _bmode="${FLEET_MODE:-mock}"
    REMOTE_STOP=""
    [ "${_bmode}" = "gateway" ] && REMOTE_STOP="vllm-sr stop >/dev/null 2>&1 || true; "
    ssh "${SSH_OPTS[@]}" "${target}" \
      "${REMOTE_STOP}for f in ${id}-agent ${id}-router; do p=${REMOTE_STATE}/\$f.pid; [ -f \"\$p\" ] && kill \"\$(cat \"\$p\")\" 2>/dev/null || true; rm -f \"\$p\"; done; command -v pkill >/dev/null 2>&1 && pkill -f 'fleet_agent.py --tag ${id}' 2>/dev/null || true; rm -rf ${REMOTE_DIR}" \
      || echo "    (warning: could not reach ${id}; stop it manually if it is still running)"
  done
fi

echo "==> Teardown complete. State dir retained for logs: ${FLEET_STATE_DIR}"
