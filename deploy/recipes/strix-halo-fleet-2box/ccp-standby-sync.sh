#!/usr/bin/env bash
#
# R6 warm standby -- replicate the ACTIVE CCP's state dir (desired/ + audit.log)
# to a STANDBY box so a second, IDENTICAL ccp_server.py can _restore() it and
# take over. This is the "replicate" half of warm standby; promote-standby.sh is
# the "take over" half. See docs/ha-standby.md.
#
# It is OPT-IN and additive: it only READS the active CCP's state dir and never
# touches the active CCP process, the agents, or the default single-CCP flow.
# Auto-failover (floating address / quorum) is explicitly OUT OF SCOPE.
#
# The state dir is exactly what ccp_server.CCPState persists (and _restore()s on
# boot): desired/<vN>.yaml (config + version counter) plus the append-only
# audit.log. Replicating those two is sufficient for a standby to reconstruct the
# whole CCP -- no database, no extra state.
#
# SOURCE (active):   ${CCP_STATE_DIR} (default ${FLEET_STATE_DIR}/ccp, matching
#                    ccp-bring-up.sh). Must hold desired/; audit.log appears once
#                    any agent has reported.
# DEST (standby):    ${STANDBY_STATE_DIR} on ${STANDBY_HOST}, over SSH+rsync. With
#                    STANDBY_HOST empty the copy is LOCAL (to a path or a mounted
#                    volume) -- handy for a shared-storage standby or a dry-run.
#
# Env (all have sane defaults; nothing here is required for the default flow):
#   FLEET_STATE_DIR        active fleet state root         [fleet_common.sh default]
#   CCP_STATE_DIR          source CCP state dir            [${FLEET_STATE_DIR}/ccp]
#   STANDBY_HOST           ssh target (user@host) of the standby; empty => local copy
#   STANDBY_STATE_DIR      dest CCP state dir              [same path as CCP_STATE_DIR]
#   STANDBY_SSH_PORT       ssh port for the standby        [none]
#   STANDBY_SSH_KEY        ssh identity file (no spaces)   [none]
#   FLEET_SSH_CONTROL_PATH reuse a shared SSH ControlMaster socket if set (R10)
#   SYNC_INTERVAL          seconds between sync passes      [15]
#   SYNC_ONCE              1 => a single pass then exit (cron / systemd timer / test)
#   SYNC_RSYNC_OPTS        extra rsync flags appended to the defaults [none]
#   SYNC_STATUS_FILE       where to stamp each successful pass
#                          [${FLEET_STATE_DIR}/ccp-standby-sync.status]
#
# Examples:
#   # continuous replication to a standby box, reusing the deploy's SSH socket
#   STANDBY_HOST=ubuntu@10.0.0.3 bash ccp-standby-sync.sh
#   # one planned pass right before a graceful promotion (=> zero RPO)
#   STANDBY_HOST=ubuntu@10.0.0.3 SYNC_ONCE=1 bash ccp-standby-sync.sh
#   # local copy to a mounted volume (no SSH)
#   STANDBY_STATE_DIR=/mnt/standby/ccp SYNC_ONCE=1 bash ccp-standby-sync.sh
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"

# --- resolve source / dest ---------------------------------------------------
CCP_STATE_DIR="${CCP_STATE_DIR:-${FLEET_STATE_DIR}/ccp}"
CCP_STATE_DIR="${CCP_STATE_DIR%/}"
STANDBY_HOST="${STANDBY_HOST:-}"
STANDBY_STATE_DIR="${STANDBY_STATE_DIR:-${CCP_STATE_DIR}}"
STANDBY_STATE_DIR="${STANDBY_STATE_DIR%/}"
STANDBY_SSH_PORT="${STANDBY_SSH_PORT:-}"
STANDBY_SSH_KEY="${STANDBY_SSH_KEY:-}"
SYNC_INTERVAL="${SYNC_INTERVAL:-15}"
SYNC_ONCE="${SYNC_ONCE:-}"
SYNC_RSYNC_OPTS="${SYNC_RSYNC_OPTS:-}"
SYNC_STATUS_FILE="${SYNC_STATUS_FILE:-${FLEET_STATE_DIR}/ccp-standby-sync.status}"

SRC_DESIRED="${CCP_STATE_DIR}/desired"
SRC_AUDIT="${CCP_STATE_DIR}/audit.log"

if command -v rsync >/dev/null 2>&1; then USE_RSYNC=1; else USE_RSYNC=0; fi

# Human-readable description of the destination for logs.
if [ -n "${STANDBY_HOST}" ]; then
  DEST_DESC="${STANDBY_HOST}:${STANDBY_STATE_DIR}"
else
  DEST_DESC="${STANDBY_STATE_DIR} (local)"
fi

# --- SSH / rsync option plumbing (reuses the shared ControlMaster if present) -
SSH_OPTS=()
[ -n "${FLEET_SSH_CONTROL_PATH:-}" ] && SSH_OPTS+=(-o ControlMaster=auto -o ControlPath="${FLEET_SSH_CONTROL_PATH}" -o ControlPersist=10m)
[ -n "${STANDBY_SSH_PORT}" ] && SSH_OPTS+=(-p "${STANDBY_SSH_PORT}")
[ -n "${STANDBY_SSH_KEY}" ] && SSH_OPTS+=(-i "${STANDBY_SSH_KEY}")

# scp takes an uppercase -P for the port; otherwise the same options.
SCP_OPTS=()
[ -n "${FLEET_SSH_CONTROL_PATH:-}" ] && SCP_OPTS+=(-o ControlMaster=auto -o ControlPath="${FLEET_SSH_CONTROL_PATH}" -o ControlPersist=10m)
[ -n "${STANDBY_SSH_PORT}" ] && SCP_OPTS+=(-P "${STANDBY_SSH_PORT}")
[ -n "${STANDBY_SSH_KEY}" ] && SCP_OPTS+=(-i "${STANDBY_SSH_KEY}")

# Optional extra rsync flags as an array (empty => no args), so passing them is
# word-safe without an unquoted expansion.
read -r -a RSYNC_EXTRA <<<"${SYNC_RSYNC_OPTS}"

# rsync -e wants ONE string; ControlPath/%-tokens contain no spaces so plain
# concatenation is safe (keep STANDBY_SSH_KEY a space-free path).
build_rsh() {
  local rsh="ssh"
  [ -n "${FLEET_SSH_CONTROL_PATH:-}" ] && rsh="${rsh} -o ControlMaster=auto -o ControlPath=${FLEET_SSH_CONTROL_PATH} -o ControlPersist=10m"
  [ -n "${STANDBY_SSH_PORT}" ] && rsh="${rsh} -p ${STANDBY_SSH_PORT}"
  [ -n "${STANDBY_SSH_KEY}" ] && rsh="${rsh} -i ${STANDBY_SSH_KEY}"
  printf '%s' "${rsh}"
}

# mkdir -p the destination state dir (remote or local) so rsync/scp/cp land it.
ensure_dest() {
  if [ -n "${STANDBY_HOST}" ]; then
    # STANDBY_STATE_DIR intentionally expands locally: it is the dest path we
    # want created on the standby (no remote-side variable is involved).
    # shellcheck disable=SC2029
    ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${STANDBY_HOST}" "mkdir -p ${STANDBY_STATE_DIR}/desired"
  else
    mkdir -p "${STANDBY_STATE_DIR}/desired"
  fi
}

# One replication pass. Copies desired/ then audit.log (if it exists yet).
# Idempotent: rsync ships only deltas; the scp/cp fallback overwrites in place.
# desired/<vN>.yaml files are immutable and append-only in count, so no deletion
# is needed and none is performed (safer for a live target).
do_sync() {
  if [ ! -d "${SRC_DESIRED}" ]; then
    echo "ERROR: source desired dir not found: ${SRC_DESIRED}" >&2
    echo "       Is the active CCP running with CCP_STATE_DIR=${CCP_STATE_DIR}?" >&2
    return 1
  fi
  ensure_dest
  local rsh
  if [ -n "${STANDBY_HOST}" ]; then
    if [ "${USE_RSYNC}" = "1" ]; then
      rsh="$(build_rsh)"
      rsync -a ${RSYNC_EXTRA[@]+"${RSYNC_EXTRA[@]}"} -e "${rsh}" "${SRC_DESIRED}/" "${STANDBY_HOST}:${STANDBY_STATE_DIR}/desired/"
      if [ -f "${SRC_AUDIT}" ]; then
        rsync -a ${RSYNC_EXTRA[@]+"${RSYNC_EXTRA[@]}"} -e "${rsh}" "${SRC_AUDIT}" "${STANDBY_HOST}:${STANDBY_STATE_DIR}/audit.log"
      fi
    else
      scp -r ${SCP_OPTS[@]+"${SCP_OPTS[@]}"} "${SRC_DESIRED}" "${STANDBY_HOST}:${STANDBY_STATE_DIR}/"
      [ -f "${SRC_AUDIT}" ] && scp ${SCP_OPTS[@]+"${SCP_OPTS[@]}"} "${SRC_AUDIT}" "${STANDBY_HOST}:${STANDBY_STATE_DIR}/audit.log"
    fi
  else
    if [ "${USE_RSYNC}" = "1" ]; then
      rsync -a ${RSYNC_EXTRA[@]+"${RSYNC_EXTRA[@]}"} "${SRC_DESIRED}/" "${STANDBY_STATE_DIR}/desired/"
      [ -f "${SRC_AUDIT}" ] && rsync -a ${RSYNC_EXTRA[@]+"${RSYNC_EXTRA[@]}"} "${SRC_AUDIT}" "${STANDBY_STATE_DIR}/audit.log"
    else
      cp -a "${SRC_DESIRED}/." "${STANDBY_STATE_DIR}/desired/"
      [ -f "${SRC_AUDIT}" ] && cp -a "${SRC_AUDIT}" "${STANDBY_STATE_DIR}/audit.log"
    fi
  fi
  stamp_status
}

# Record an informational stamp of the last successful pass (NOT replicated; it
# lives outside the state dir so it can never confuse _restore). audit_records is
# an approximate line count -- the CCP /fleet/status audit_count is authoritative.
stamp_status() {
  local ver="none" n=0 latest="" f base num
  # Highest desired/vN.yaml version, via a glob (filenames are controlled: vN.yaml).
  for f in "${SRC_DESIRED}"/v*.yaml; do
    [ -e "${f}" ] || continue
    base="${f##*/}"; num="${base#v}"; num="${num%.yaml}"
    case "${num}" in ''|*[!0-9]*) continue ;; esac
    if [ -z "${latest}" ] || [ "${num}" -gt "${latest}" ]; then latest="${num}"; fi
  done
  [ -n "${latest}" ] && ver="v${latest}"
  [ -f "${SRC_AUDIT}" ] && n="$(grep -c . "${SRC_AUDIT}" 2>/dev/null || echo 0)"
  {
    echo "last_sync_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "src=${CCP_STATE_DIR}"
    echo "dest=${DEST_DESC}"
    echo "desired_version=${ver}"
    echo "audit_records_approx=${n}"
  } >"${SYNC_STATUS_FILE}" 2>/dev/null || true
}

echo "==> [standby-sync] src=${CCP_STATE_DIR}  ->  dest=${DEST_DESC}  (rsync=${USE_RSYNC})"

if [ "${SYNC_ONCE}" = "1" ]; then
  do_sync
  echo "    one-shot sync complete (stamp: ${SYNC_STATUS_FILE})"
else
  echo "    replicating every ${SYNC_INTERVAL}s; Ctrl-C to stop"
  while :; do
    # Run each pass in a subshell so a transient failure (e.g. the standby box
    # briefly unreachable) is logged and retried instead of killing the loop.
    if ( do_sync ); then
      echo "    [$(date -u +%H:%M:%SZ)] synced -> ${DEST_DESC}"
    else
      echo "WARN: [$(date -u +%H:%M:%SZ)] sync pass failed; retrying in ${SYNC_INTERVAL}s" >&2
    fi
    sleep "${SYNC_INTERVAL}"
  done
fi
