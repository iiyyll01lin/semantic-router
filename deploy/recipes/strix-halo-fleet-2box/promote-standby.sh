#!/usr/bin/env bash
#
# R6 warm standby -- PROMOTE the standby CCP to active. This is the "take over"
# half of warm standby (ccp-standby-sync.sh is the "replicate" half). It:
#   1. waits for the standby ccp_server to be serving the restored desired config,
#   2. measures the recovery time,
#   3. confirms ZERO AUDIT LOSS (audit record counts active vs standby),
#   4. repoints the fleet by rewriting CCP_URL in ${FLEET_STATE_DIR}/fleet.env,
#   5. prints (and optionally runs) the agent re-broadcast/restart.
#
# It is an OPERATOR action (or a health check can invoke it); there is NO
# automatic failover, floating address, or quorum here -- that is OUT OF SCOPE
# (documented as future work in docs/ha-standby.md). Nothing here runs in the
# default single-CCP flow.
#
# Prereqs (see docs/ha-standby.md for the exact commands):
#   * The standby box already runs the SAME ccp_server.py against the SYNCED
#     CCP_STATE_DIR, with the SAME FLEET_TOKEN + signing keys. Because a running
#     ccp_server only _restore()s at boot, (re)start it at promotion time so it
#     loads the freshly-synced desired+audit (a graceful drill also runs one
#     final `SYNC_ONCE=1 ccp-standby-sync.sh` first => zero RPO).
#   * fleet.env from the deploy exists under ${FLEET_STATE_DIR} (it carries the
#     bearer token and the current CCP_URL).
#
# Env:
#   FLEET_STATE_DIR     active fleet state root (holds fleet.env) [fleet_common default]
#   STANDBY_HOST        host/IP the agents will reach the standby CCP on   [REQUIRED]
#   STANDBY_CCP_PORT    standby CCP port                          [${CCP_PORT} or 9300]
#   STANDBY_SCHEME      http | https                              [scheme of current CCP_URL]
#   FLEET_TOKEN         bearer token                              [from fleet.env]
#   ACTIVE_CCP_URL      old active URL for the audit diff         [current fleet.env CCP_URL]
#   EXPECT_AUDIT_COUNT  cross-check the standby against a count you captured before
#                       stopping the active (used when the active is unreachable)
#   PROMOTE_SINCE       epoch seconds when the outage/drill began (for the RTO)  [now]
#   PROMOTE_WAIT        seconds to wait for the standby to serve a version  [60]
#   PROMOTE_APPLY       1 => also restart the LOCAL halo-a agent at the new CCP_URL
#                       (remote agents are always printed, never auto-SSH'd here)
#   DRY_RUN             1 => show what would change; do NOT edit fleet.env
#   FLEET_TLS_CA / FLEET_TLS_INSECURE   passed through for an https standby
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"
PYBIN="$(fleet_pybin)"

ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
if [ ! -f "${ENV_FILE}" ]; then
  echo "ERROR: ${ENV_FILE} not found -- run the deploy first (it writes fleet.env)." >&2
  echo "       Point FLEET_STATE_DIR at the active fleet's state dir." >&2
  exit 1
fi
# fleet.env is a set of `export VAR=value` lines written by the deploy.
# shellcheck source=/dev/null
source "${ENV_FILE}"

: "${STANDBY_HOST:?set STANDBY_HOST (host/IP the agents will reach the standby CCP on)}"
FLEET_TOKEN="${FLEET_TOKEN:-}"
: "${FLEET_TOKEN:?FLEET_TOKEN not set (expected from fleet.env)}"

STANDBY_CCP_PORT="${STANDBY_CCP_PORT:-${CCP_PORT:-9300}}"
CUR_CCP_URL="${CCP_URL:-}"
case "${CUR_CCP_URL}" in
  https://*) CUR_SCHEME="https" ;;
  *)         CUR_SCHEME="http"  ;;
esac
STANDBY_SCHEME="${STANDBY_SCHEME:-${CUR_SCHEME}}"
NEW_CCP_URL="${STANDBY_SCHEME}://${STANDBY_HOST}:${STANDBY_CCP_PORT}"
ACTIVE_CCP_URL="${ACTIVE_CCP_URL:-${CUR_CCP_URL}}"
PROMOTE_WAIT="${PROMOTE_WAIT:-60}"
FLEET_TLS_CA="${FLEET_TLS_CA:-}"
FLEET_TLS_INSECURE="${FLEET_TLS_INSECURE:-}"

# curl -k only for an https healthz probe (the liveness wait; the authenticated
# JSON calls go through fleetctl.py, which honors FLEET_TLS_CA/FLEET_TLS_INSECURE).
_healthz_ok() {  # $1 = base url
  case "$1" in
    https://*) curl -fsSk "$1/healthz" >/dev/null 2>&1 ;;
    *)         curl -fsS  "$1/healthz" >/dev/null 2>&1 ;;
  esac
}

# Print the standby's desired version via the existing fleetctl.py (empty on fail).
_desired_version() {  # $1 = base url
  CCP_URL="$1" FLEET_TOKEN="${FLEET_TOKEN}" \
  FLEET_TLS_CA="${FLEET_TLS_CA}" FLEET_TLS_INSECURE="${FLEET_TLS_INSECURE}" \
    "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" desired-version 2>/dev/null || true
}

# Extract audit_count from fleetctl status (empty on failure). Reuses the CCP's
# authoritative running total; no jq needed.
_audit_count() {  # $1 = base url
  local out
  out="$(CCP_URL="$1" FLEET_TOKEN="${FLEET_TOKEN}" \
        FLEET_TLS_CA="${FLEET_TLS_CA}" FLEET_TLS_INSECURE="${FLEET_TLS_INSECURE}" \
        "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" status 2>/dev/null)" || return 0
  printf '%s\n' "${out}" | sed -n 's/.*audit_count=\([0-9][0-9]*\).*/\1/p' | head -n1
}

echo "=============================================================="
echo "  R6 warm-standby promotion"
echo "  active (old): ${ACTIVE_CCP_URL:-<none>}"
echo "  standby (new): ${NEW_CCP_URL}"
echo "  fleet.env:     ${ENV_FILE}"
echo "=============================================================="

# --- 1/4: wait for the standby to serve the restored desired config, timing it -
T0="${PROMOTE_SINCE:-$(date +%s)}"
echo "==> [1/4] waiting up to ${PROMOTE_WAIT}s for the standby CCP to serve a version"
served_version=""
served_at=""
for _i in $(seq 1 "${PROMOTE_WAIT}"); do
  if _healthz_ok "${NEW_CCP_URL}"; then
    served_version="$(_desired_version "${NEW_CCP_URL}")"
    if [ -n "${served_version}" ]; then
      served_at="$(date +%s)"
      break
    fi
  fi
  sleep 1
done
if [ -z "${served_version}" ]; then
  echo "ERROR: standby CCP at ${NEW_CCP_URL} did not serve a desired version within ${PROMOTE_WAIT}s." >&2
  echo "       Confirm the standby ccp_server is (re)started against the synced CCP_STATE_DIR" >&2
  echo "       with the SAME FLEET_TOKEN (+ signing keys / TLS trust). See docs/ha-standby.md." >&2
  exit 1
fi
RECOVERY_S=$(( served_at - T0 ))
[ "${RECOVERY_S}" -lt 0 ] && RECOVERY_S=0
echo "    standby is serving desired ${served_version}"
echo "    recovery time: ${RECOVERY_S}s (since $( [ -n "${PROMOTE_SINCE:-}" ] && echo 'PROMOTE_SINCE' || echo 'promotion start' ))"

# --- 2/4: zero-audit-loss check (compare audit record counts) ----------------
echo "==> [2/4] confirming zero audit loss (audit record counts active vs standby)"
STANDBY_COUNT="$(_audit_count "${NEW_CCP_URL}")"
STANDBY_COUNT="${STANDBY_COUNT:-0}"
ACTIVE_COUNT=""
[ -n "${ACTIVE_CCP_URL}" ] && ACTIVE_COUNT="$(_audit_count "${ACTIVE_CCP_URL}")"
ZERO_LOSS="unknown"
if [ -n "${ACTIVE_COUNT}" ]; then
  echo "    active audit_count=${ACTIVE_COUNT}  standby audit_count=${STANDBY_COUNT}"
  if [ "${STANDBY_COUNT}" -ge "${ACTIVE_COUNT}" ]; then
    ZERO_LOSS="PASS"
    echo "    ZERO AUDIT LOSS: PASS (standby has all ${ACTIVE_COUNT} active records)"
  else
    ZERO_LOSS="FAIL"
    echo "    ZERO AUDIT LOSS: FAIL -- standby is behind by $(( ACTIVE_COUNT - STANDBY_COUNT )) record(s)." >&2
    echo "      Run one final 'SYNC_ONCE=1 bash ccp-standby-sync.sh' and (re)start the standby CCP." >&2
  fi
elif [ -n "${EXPECT_AUDIT_COUNT:-}" ]; then
  echo "    active unreachable; standby audit_count=${STANDBY_COUNT}  expected>=${EXPECT_AUDIT_COUNT}"
  if [ "${STANDBY_COUNT}" -ge "${EXPECT_AUDIT_COUNT}" ]; then
    ZERO_LOSS="PASS"; echo "    ZERO AUDIT LOSS: PASS (vs captured EXPECT_AUDIT_COUNT)"
  else
    ZERO_LOSS="FAIL"; echo "    ZERO AUDIT LOSS: FAIL (vs captured EXPECT_AUDIT_COUNT)" >&2
  fi
else
  echo "    active unreachable and no EXPECT_AUDIT_COUNT set; standby audit_count=${STANDBY_COUNT}"
  echo "    (unplanned failover: worst-case loss is one sync interval; see the sync stamp"
  echo "     ${FLEET_STATE_DIR}/ccp-standby-sync.status for the last replicated count.)"
fi

# --- 3/4: repoint the fleet (rewrite CCP_URL in fleet.env) -------------------
echo "==> [3/4] repointing agents: CCP_URL -> ${NEW_CCP_URL} in ${ENV_FILE}"
if [ "${CUR_CCP_URL}" = "${NEW_CCP_URL}" ]; then
  echo "    CCP_URL is already ${NEW_CCP_URL} (idempotent no-op)"
elif [ "${DRY_RUN:-}" = "1" ]; then
  echo "    DRY_RUN=1: would change 'export CCP_URL=${CUR_CCP_URL}' -> 'export CCP_URL=${NEW_CCP_URL}'"
else
  TMP_ENV="$(mktemp "${ENV_FILE}.XXXXXX")"
  replaced=0
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in
      export\ CCP_URL=*) printf 'export CCP_URL=%s\n' "${NEW_CCP_URL}"; replaced=1 ;;
      *) printf '%s\n' "${line}" ;;
    esac
  done <"${ENV_FILE}" >"${TMP_ENV}"
  [ "${replaced}" = "0" ] && printf 'export CCP_URL=%s\n' "${NEW_CCP_URL}" >>"${TMP_ENV}"
  cp -f "${ENV_FILE}" "${ENV_FILE}.bak"
  mv -f "${TMP_ENV}" "${ENV_FILE}"
  echo "    updated (previous fleet.env saved as ${ENV_FILE}.bak)"
fi

# --- 4/4: re-broadcast (restart agents at the new CCP_URL) -------------------
# Agents read CCP_URL only at bring-up, so repointing them = restarting each
# agent with the new URL. The LOCAL halo-a agent can be restarted here; remote
# agents are printed (never auto-SSH'd) so the operator stays in control.
echo "==> [4/4] re-broadcast: restart each agent against ${NEW_CCP_URL}"
FLEET_BOXES="${FLEET_BOXES:-halo-a}"
cat <<EOF
    On EACH edge box, re-run node-bring-up.sh with the new CCP_URL, e.g.:
      # local (halo-a):
      source ${ENV_FILE}
      BOX_ID=halo-a CCP_URL=${NEW_CCP_URL} FLEET_MODE="\${FLEET_MODE}" \\
        FLEET_SIGNING_KEY="\${FLEET_SIGNING_KEY}" FLEET_TOKEN="\${FLEET_TOKEN}" \\
        ROUTER_PORT="\${ROUTER_PORT}" POLL_INTERVAL="\${POLL_INTERVAL:-3}" \\
        FLEET_STATE_DIR="${FLEET_STATE_DIR}" bash ${SCRIPT_DIR}/node-bring-up.sh
      # each remote box (reuse the shared SSH ControlMaster if you have one):
      ssh <box> "BOX_ID=<id> CCP_URL=${NEW_CCP_URL} FLEET_MODE=<mode> \\
        FLEET_SIGNING_KEY=<key> FLEET_TOKEN=<token> ROUTER_PORT=<port> \\
        FLEET_STATE_DIR=\\\${TMPDIR:-/tmp}/vllm-sr-fleet bash <remote_dir>/node-bring-up.sh"
    Fleet boxes recorded at deploy: ${FLEET_BOXES}
EOF

if [ "${PROMOTE_APPLY:-}" = "1" ] && [ "${DRY_RUN:-}" != "1" ]; then
  echo "    PROMOTE_APPLY=1: restarting the local halo-a agent at ${NEW_CCP_URL}"
  # Re-source the (now-updated) env, then override CCP_URL explicitly.
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  BOX_ID="halo-a" CCP_URL="${NEW_CCP_URL}" FLEET_MODE="${FLEET_MODE:-mock}" \
  FLEET_SIGNING_KEY="${FLEET_SIGNING_KEY:-}" FLEET_TOKEN="${FLEET_TOKEN}" \
  ROUTER_PORT="${ROUTER_PORT:-8080}" POLL_INTERVAL="${POLL_INTERVAL:-3}" \
  FLEET_STATE_DIR="${FLEET_STATE_DIR}" \
    bash "${SCRIPT_DIR}/node-bring-up.sh" || echo "WARN: local agent restart returned non-zero" >&2
fi

echo "=============================================================="
echo "  promotion summary"
echo "    new CCP_URL:      ${NEW_CCP_URL}"
echo "    desired served:   ${served_version}"
echo "    recovery time:    ${RECOVERY_S}s"
echo "    zero audit loss:  ${ZERO_LOSS} (active=${ACTIVE_COUNT:-n/a} standby=${STANDBY_COUNT})"
echo "=============================================================="
[ "${ZERO_LOSS}" = "FAIL" ] && exit 2
exit 0
