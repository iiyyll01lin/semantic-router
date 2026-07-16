#!/usr/bin/env bash
#
# strix-halo-fleet-2box: ONE-CLICK pull-mode fleet config control plane.
#
# Run on Halo-A. Stands up the Central Control Plane (CCP) on Halo-A, an edge
# node (router + pull agent) on Halo-A, and edge node(s) on BARE remote box(es)
# over SSH/scp, then waits until EVERY box has converged to the CCP's desired
# config. Implements PL-0036 phases P1-P4 (fan-out, audit, signing, pull agent).
#
# Default FLEET_MODE=mock runs a stdlib mock router on each box so the whole
# fan-out is verifiable WITHOUT ROCm. Set FLEET_MODE=gateway to point the agents
# at real `vllm-sr serve` routers you started on each box (see README).
#
# Required env:
#   HALO_A_IP    address of THIS box (Halo-A) reachable FROM the remote boxes (CCP URL).
# Classic 2-box (default; no fleet.hosts) also needs:
#   HALO_B_IP    address of Halo-B (informational note).
#   HALO_B_SSH   user@host control address for Halo-B.
# Optional env:
#   HALO_B_SSH_PORT, HALO_B_SSH_KEY, FLEET_MODE (mock), CCP_PORT (9300),
#   ROUTER_PORT (8080), POLL_INTERVAL (3), SKIP_VERIFY=1
#   VLLM_SR_ROUTER_IMAGE     pin the router image (R3; or set it in versions.env)
#   FLEET_SKIP_VALIDATE=1    skip the pre-pull config validation (R2)
#   FLEET_SSH_CONTROL_PATH   reuse an externally-owned SSH ControlMaster (R10;
#                            set by run-all-2box.sh so the whole run authenticates once)
#   CCP_TLS_CERT + CCP_TLS_KEY  serve the CCP over HTTPS; agent URLs become https:// (R5)
#   FLEET_CCP_SCHEME=https      force the agent-facing scheme to https (opt-in override)
#
# N-box scale-out (R7): create fleet.hosts (see fleet.hosts.example) to run more
# than the default two boxes; the provisioning + convergence loop over every box.
# When fleet.hosts is absent the classic 2-box behavior (HALO_B_*) is unchanged.
#
# Usage:
#   HALO_A_IP=192.0.2.10 HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 \
#     bash deploy-fleet-2box.sh
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"

# --- R3: optional image pinning ----------------------------------------------
# Source an OPTIONAL, gitignored versions.env (copy versions.env.example) so
# EVERY box runs the SAME pinned router digest instead of a drifting :latest.
# Absent => unchanged behavior. `set -a` auto-exports the assignments so the
# local Halo-A gateway inherits them; they are also forwarded to remote boxes.
VERSIONS_ENV="${VERSIONS_ENV:-${SCRIPT_DIR}/versions.env}"
if [ -f "${VERSIONS_ENV}" ]; then
  echo "==> [pin] sourcing ${VERSIONS_ENV}"
  set -a
  # shellcheck source=/dev/null
  source "${VERSIONS_ENV}"
  set +a
fi
# Normalize (so `set -u` refs below are always safe) without overriding a value.
export VLLM_SR_ROUTER_IMAGE="${VLLM_SR_ROUTER_IMAGE:-}"
export VLLM_SR_IMAGE_PULL_POLICY="${VLLM_SR_IMAGE_PULL_POLICY:-}"

# --- Optional, OPT-IN security env pass-through (R4/R5; owned by the core agent)
# NEVER required. Names below match what ccp_server.py / fleet_agent.py / fleet_lib.py
# and ccp-bring-up.sh / node-bring-up.sh actually read; override the lists to
# reconcile if the core renames them. Unset => today's HMAC-over-HTTP defaults.
#
#   * AGENT-side  : exported locally (the Halo-A agent inherits them) AND forwarded
#                   to every REMOTE agent over SSH (node-bring-up.sh reads them).
#   * CCP-side    : exported locally ONLY, so the Halo-A CCP inherits them via
#                   ccp-bring-up.sh. The PRIVATE signing seed + TLS server key
#                   NEVER leave Halo-A (they are absent from the forwarded set).
#   * EXTRA agent : non-security opt-in agent knobs (R8 health/rollback) so remote
#                   agents match Halo-A; forwarded like the agent-side security set.
FLEET_SECURITY_AGENT_VARS="${FLEET_SECURITY_AGENT_VARS:-FLEET_SIGN_MODE FLEET_ED25519_PUBLIC FLEET_ED25519_PUBLIC_FILE FLEET_TLS_CA FLEET_TLS_INSECURE FLEET_TLS_CLIENT_CERT FLEET_TLS_CLIENT_KEY FLEET_BUNDLE_MAX_AGE}"
FLEET_SECURITY_CCP_VARS="${FLEET_SECURITY_CCP_VARS:-FLEET_SIGN_MODE FLEET_ED25519_SECRET FLEET_ED25519_SECRET_FILE FLEET_BUNDLE_TS CCP_TLS_CERT CCP_TLS_KEY CCP_TLS_CLIENT_CA CCP_AUDIT_MEMORY_MAX}"
FLEET_AGENT_EXTRA_VARS="${FLEET_AGENT_EXTRA_VARS:-ROUTER_HEALTH_PATH ROUTER_HEALTH_TIMEOUT APPLY_BACKOFF APPLY_BACKOFF_MAX}"
for _sv in ${FLEET_SECURITY_AGENT_VARS} ${FLEET_SECURITY_CCP_VARS} ${FLEET_AGENT_EXTRA_VARS}; do
  # _sv holds the NAME of the var to export (pass-through only if it is set).
  # shellcheck disable=SC2163
  [ -n "${!_sv:-}" ] && export "${_sv}"
done
# Build the (quoted, only-if-set) agent-side env forwarded over SSH to remotes.
remote_agent_env() {
  local out="" v
  for v in ${FLEET_SECURITY_AGENT_VARS} ${FLEET_AGENT_EXTRA_VARS}; do
    [ -n "${!v:-}" ] && out+="${v}=$(printf %q "${!v}") "
  done
  printf '%s' "${out}"
}

FLEET_MODE="${FLEET_MODE:-mock}"
# Per-box mode: each box defaults to FLEET_MODE but can be overridden so a capable
# box runs a REAL gateway while a minimal box runs the lightweight mock edge, e.g.
#   HALO_A_MODE=gateway HALO_B_MODE=mock bash deploy-fleet-2box.sh
# The CCP's desired config is a real gateway config if ANY box is a gateway
# (mock routers accept any bytes and just report their content hash).
HALO_A_MODE="${HALO_A_MODE:-${FLEET_MODE}}"
HALO_B_MODE="${HALO_B_MODE:-${FLEET_MODE}}"

# --- R7: resolve the remote edge boxes ---------------------------------------
# fleet.hosts (optional; see fleet.hosts.example) lists N remote boxes, one per
# line. When it is absent/empty we fall back to the classic single remote box
# from HALO_B_* env, so the default 2-box run is byte-for-byte unchanged.
FLEET_HOSTS_FILE="${FLEET_HOSTS_FILE:-${SCRIPT_DIR}/fleet.hosts}"
BOX_IDS=(); BOX_SSH=(); BOX_IP=(); BOX_MODE=(); BOX_REPO=(); BOX_PORT=(); BOX_KEY=()
FLEET_HOSTS_SOURCE=""
_undash() { case "${1:-}" in ""|-) printf '' ;; *) printf '%s' "$1" ;; esac; }
resolve_remote_boxes() {
  if [ -f "${FLEET_HOSTS_FILE}" ] && grep -qvE '^[[:space:]]*(#.*)?$' "${FLEET_HOSTS_FILE}"; then
    FLEET_HOSTS_SOURCE="${FLEET_HOSTS_FILE}"
    local id ssht ip mode repo port key rest m
    while read -r id ssht ip mode repo port key rest; do
      [ -z "${id:-}" ] && continue
      case "${id}" in \#*) continue ;; esac
      [ "${id}" = "halo-a" ] && { echo "ERROR: fleet.hosts must not list 'halo-a' (this local box is implicit)" >&2; exit 1; }
      [ -z "${ssht:-}" ] && { echo "ERROR: fleet.hosts line for '${id}' has no ssh_target" >&2; exit 1; }
      m="$(_undash "${mode:-}")"
      BOX_IDS+=("${id}"); BOX_SSH+=("${ssht}")
      BOX_IP+=("$(_undash "${ip:-}")")
      BOX_MODE+=("${m:-${FLEET_MODE}}")
      BOX_REPO+=("$(_undash "${repo:-}")")
      BOX_PORT+=("$(_undash "${port:-}")")
      BOX_KEY+=("$(_undash "${key:-}")")
    done < "${FLEET_HOSTS_FILE}"
  else
    FLEET_HOSTS_SOURCE="env (HALO_B_*)"
    if [ -n "${HALO_B_SSH:-}" ]; then
      BOX_IDS+=("halo-b"); BOX_SSH+=("${HALO_B_SSH}")
      BOX_IP+=("${HALO_B_IP:-}"); BOX_MODE+=("${HALO_B_MODE}")
      BOX_REPO+=("${HALO_B_REPO:-}"); BOX_PORT+=("${HALO_B_SSH_PORT:-}")
      BOX_KEY+=("${HALO_B_SSH_KEY:-}")
    fi
  fi
}
resolve_remote_boxes

# desired mode = gateway if Halo-A OR any remote box is a gateway
DESIRED_MODE="mock"
[ "${HALO_A_MODE}" = "gateway" ] && DESIRED_MODE="gateway"
for _m in ${BOX_MODE[@]+"${BOX_MODE[@]}"}; do
  [ "${_m}" = "gateway" ] && DESIRED_MODE="gateway"
done

# all box ids (local halo-a + remotes) as a CSV for convergence waits
ALL_BOXES_CSV="halo-a"
for _id in ${BOX_IDS[@]+"${BOX_IDS[@]}"}; do ALL_BOXES_CSV="${ALL_BOXES_CSV},${_id}"; done

DASHBOARD_ADMIN_EMAIL="${DASHBOARD_ADMIN_EMAIL:-yingylin@amd.com}"
DASHBOARD_ADMIN_PASSWORD="${DASHBOARD_ADMIN_PASSWORD:-aupaup123}"
DASHBOARD_ADMIN_NAME="${DASHBOARD_ADMIN_NAME:-yingylin}"
GF_SECURITY_ADMIN_USER="${GF_SECURITY_ADMIN_USER:-${DASHBOARD_ADMIN_EMAIL}}"
GF_SECURITY_ADMIN_PASSWORD="${GF_SECURITY_ADMIN_PASSWORD:-${DASHBOARD_ADMIN_PASSWORD}}"
export DASHBOARD_ADMIN_EMAIL DASHBOARD_ADMIN_PASSWORD DASHBOARD_ADMIN_NAME
export GF_SECURITY_ADMIN_USER GF_SECURITY_ADMIN_PASSWORD
PYBIN="$(fleet_pybin)"
# ~ and ${TMPDIR} are intentionally NOT expanded locally: they expand on the
# REMOTE shell (inside the ssh command strings below).
# shellcheck disable=SC2088
REMOTE_DIR="~/.vllm-sr-fleet-2box"
REMOTE_STATE="\${TMPDIR:-/tmp}/vllm-sr-fleet"

# --- R10: SSH ControlMaster (authenticate ONCE, reuse across the whole run) ---
# If run-all-2box.sh set up a shared socket, REUSE it (so deploy + demo + log
# collection + teardown all share ONE authenticated connection per host) and let
# run-all own its teardown. Otherwise create our own and close it on exit.
if [ -n "${FLEET_SSH_CONTROL_PATH:-}" ]; then
  SSH_CTRL_PATH="${FLEET_SSH_CONTROL_PATH}"
  SSH_CTRL_DIR=""
  SSH_CTRL_OWNED=0
else
  SSH_CTRL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vllm-sr-fleet-ssh.XXXXXX")"
  SSH_CTRL_PATH="${SSH_CTRL_DIR}/cm-%r@%h:%p"
  SSH_CTRL_OWNED=1
fi
SSH_CM_OPTS=(-o ControlMaster=auto -o ControlPath="${SSH_CTRL_PATH}" -o ControlPersist=10m)

cleanup() {
  if [ "${SSH_CTRL_OWNED:-1}" = "1" ]; then
    local idx opts
    for idx in ${BOX_SSH[@]+"${!BOX_SSH[@]}"}; do
      opts=(-o ControlPath="${SSH_CTRL_PATH}")
      [ -n "${BOX_KEY[$idx]:-}" ] && opts+=(-i "${BOX_KEY[$idx]}")
      [ -n "${BOX_PORT[$idx]:-}" ] && opts+=(-p "${BOX_PORT[$idx]}")
      ssh "${opts[@]}" -O exit "${BOX_SSH[$idx]}" >/dev/null 2>&1 || true
    done
    [ -n "${SSH_CTRL_DIR:-}" ] && rm -rf "${SSH_CTRL_DIR}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "==> [1/6] Preflight on Halo-A"
: "${HALO_A_IP:?set HALO_A_IP (this box address, reachable from the remote boxes)}"
if [ "${#BOX_IDS[@]}" -eq 0 ]; then
  echo "ERROR: no remote edge boxes to provision." >&2
  echo "       Classic 2-box: set HALO_B_SSH (+HALO_B_IP). N-box: create fleet.hosts" >&2
  echo "       (see fleet.hosts.example)." >&2
  exit 1
fi
for bin in "${PYBIN}" ssh scp curl; do
  command -v "${bin}" >/dev/null 2>&1 || { echo "ERROR: '${bin}' not found on Halo-A" >&2; exit 1; }
done

# Generate the per-deployment signing key and use a demo-friendly CCP bearer
# token by default. FLEET_SIGNING_KEY is not a login password; keep it random
# unless explicitly overridden.
FLEET_SIGNING_KEY="${FLEET_SIGNING_KEY:-$("${PYBIN}" -c 'import secrets; print(secrets.token_hex(32))')}"
FLEET_TOKEN="${FLEET_TOKEN:-aupaup123}"
# --- R5: the agent-facing scheme follows TLS. Build https:// URLs when the CCP
# serves TLS (CCP_TLS_CERT+CCP_TLS_KEY set -- exactly how ccp-bring-up.sh decides)
# or when explicitly opted in with FLEET_CCP_SCHEME=https. Default http, so with
# no TLS env the URLs are byte-identical to the pre-TLS flow. (fleet_lib._request
# transparently enables TLS for an https:// URL; the agents/fleetctl then trust
# the cert via FLEET_TLS_CA, or FLEET_TLS_INSECURE=1 for a self-signed one.)
CCP_SCHEME="http"
if [ -n "${CCP_TLS_CERT:-}" ] && [ -n "${CCP_TLS_KEY:-}" ]; then CCP_SCHEME="https"; fi
CCP_SCHEME="${FLEET_CCP_SCHEME:-${CCP_SCHEME}}"
CCP_URL_LOCAL="${CCP_SCHEME}://localhost:${CCP_PORT}"
CCP_URL_REMOTE="${CCP_SCHEME}://${HALO_A_IP}:${CCP_PORT}"

# Persist the deployment env so verify/demo/teardown can reuse it. HALO_B_* keep
# the FIRST remote box for backward-compat; FLEET_BOXES lists every box.
ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
cat >"${ENV_FILE}" <<EOF
export FLEET_SIGNING_KEY=${FLEET_SIGNING_KEY}
export FLEET_TOKEN=${FLEET_TOKEN}
export DASHBOARD_ADMIN_EMAIL=${DASHBOARD_ADMIN_EMAIL}
export DASHBOARD_ADMIN_PASSWORD=${DASHBOARD_ADMIN_PASSWORD}
export DASHBOARD_ADMIN_NAME=${DASHBOARD_ADMIN_NAME}
export GF_SECURITY_ADMIN_USER=${GF_SECURITY_ADMIN_USER}
export GF_SECURITY_ADMIN_PASSWORD=${GF_SECURITY_ADMIN_PASSWORD}
export CCP_URL=${CCP_URL_LOCAL}
export CCP_PORT=${CCP_PORT}
export ROUTER_PORT=${ROUTER_PORT}
export FLEET_MODE=${DESIRED_MODE}
export HALO_A_MODE=${HALO_A_MODE}
export HALO_B_MODE=${BOX_MODE[0]:-${HALO_B_MODE}}
export HALO_A_IP=${HALO_A_IP}
export HALO_B_IP=${BOX_IP[0]:-}
export HALO_B_SSH=${BOX_SSH[0]:-}
export FLEET_BOXES=${ALL_BOXES_CSV}
EOF
echo "    boxes: ${ALL_BOXES_CSV} (halo-a=${HALO_A_MODE}; remotes from ${FLEET_HOSTS_SOURCE}; desired=${DESIRED_MODE})  CCP=${CCP_SCHEME}://:${CCP_PORT}  state=${FLEET_STATE_DIR}"
[ -n "${VLLM_SR_ROUTER_IMAGE}" ] && echo "    router image pin: ${VLLM_SR_ROUTER_IMAGE}"
[ "${CCP_SCHEME}" = "https" ] && echo "    CCP scheme: HTTPS (TLS on; agents must trust the cert via FLEET_TLS_CA, or FLEET_TLS_INSECURE=1 for self-signed)"

# In gateway mode, render the REAL gateway config on Halo-A and serve it as the
# CCP desired (both real gateways converge to it). Gateway mode also needs the
# semantic-router repo + models (strix-halo-poc Gate B) present on remote boxes.
CCP_INIT_CONFIG="${SCRIPT_DIR}/sample-desired-config.yaml"
if [ "${DESIRED_MODE}" = "gateway" ]; then
  # A real gateway is in the fleet, so the CCP desired must be a valid gateway
  # config, rendered locally on Halo-A from its strix-halo-poc assets. (A mock
  # edge just stores the bytes and reports their hash, so it converges too.)
  echo "    rendering the real gateway config for the CCP desired"
  _gw_cfg="${FLEET_STATE_DIR}/gateway/config.yaml"
  RENDER_ONLY=1 FLEET_STATE_DIR="${FLEET_STATE_DIR}" GATEWAY_CONFIG="${_gw_cfg}" \
    bash "${SCRIPT_DIR}/gateway-bring-up.sh"
  CCP_INIT_CONFIG="${_gw_cfg}"

  # --- R2: fail-fast config x image validation BEFORE the ~44GB pull ---------
  # A schema mismatch (e.g. a removed field vs a drifted image) fails here in
  # seconds instead of ~9 minutes into a cold start. Advisory: if no validator
  # is available it warns and proceeds. Bypass with FLEET_SKIP_VALIDATE=1.
  if [ "${FLEET_SKIP_VALIDATE:-0}" != "1" ]; then
    echo "==> [pre-pull] validating rendered config against the pinned image (fail-fast, R2)"
    if bash "${SCRIPT_DIR}/ci-check.sh" --config "${CCP_INIT_CONFIG}" \
         ${VLLM_SR_ROUTER_IMAGE:+--image "${VLLM_SR_ROUTER_IMAGE}"}; then
      echo "    pre-pull validation OK"
    else
      echo "ERROR: config failed validation against the pinned router image (see above)." >&2
      echo "       Fix poc-strix.yaml / the image pin BEFORE the slow model pull." >&2
      echo "       (Bypass with FLEET_SKIP_VALIDATE=1 if you must.)" >&2
      exit 1
    fi
  fi
fi

echo "==> [2/6] Starting CCP on Halo-A"
FLEET_SIGNING_KEY="${FLEET_SIGNING_KEY}" FLEET_TOKEN="${FLEET_TOKEN}" \
CCP_PORT="${CCP_PORT}" FLEET_STATE_DIR="${FLEET_STATE_DIR}" CCP_INIT_CONFIG="${CCP_INIT_CONFIG}" \
  bash "${SCRIPT_DIR}/ccp-bring-up.sh"

echo "==> [3/6] Bringing up the Halo-A edge node (router + agent, mode=${HALO_A_MODE})"
BOX_ID="halo-a" CCP_URL="${CCP_URL_LOCAL}" FLEET_MODE="${HALO_A_MODE}" \
FLEET_SIGNING_KEY="${FLEET_SIGNING_KEY}" FLEET_TOKEN="${FLEET_TOKEN}" \
ROUTER_PORT="${ROUTER_PORT}" POLL_INTERVAL="${POLL_INTERVAL}" FLEET_STATE_DIR="${FLEET_STATE_DIR}" \
  bash "${SCRIPT_DIR}/node-bring-up.sh"

echo "==> [4/6] Provisioning ${#BOX_IDS[@]} remote edge box(es) over SSH"
REMOTE_AGENT_ENV="$(remote_agent_env)"
if [ -n "${REMOTE_AGENT_ENV}" ]; then
  echo "    forwarding optional agent env to remotes:$(for v in ${FLEET_SECURITY_AGENT_VARS} ${FLEET_AGENT_EXTRA_VARS}; do [ -n "${!v:-}" ] && printf ' %s' "${v}"; done)"
fi

# Provision + start ONE remote edge box (identical to the proven Halo-B path).
# All N-box logic lives HERE in the orchestrator; the per-node bring-up scripts
# are shipped unchanged.
bring_up_remote_box() {
  local id="$1" target="$2" mode="$3" repo="$4" port="$5" key="$6"
  local ssh_opts=("${SSH_CM_OPTS[@]}") port_opts=() scp_port_opts=()
  [ -n "${key}" ] && ssh_opts+=(-i "${key}")
  if [ -n "${port}" ]; then port_opts=(-p "${port}"); scp_port_opts=(-P "${port}"); fi

  echo "    [${id}] ${target} (mode=${mode})"
  # First connection establishes the ControlMaster => authenticate ONCE per host.
  if ! ssh "${ssh_opts[@]}" "${port_opts[@]}" "${target}" true; then
    echo "ERROR: cannot SSH to ${target} (box '${id}'). Install your key once:" >&2
    echo "         ssh-copy-id ${port:+-p ${port} }${target}" >&2
    exit 1
  fi

  if [ "${mode}" = "gateway" ]; then
    if [ -z "${repo}" ]; then
      echo "ERROR: box '${id}' is gateway mode but has no repo path." >&2
      echo "       Classic 2-box: set HALO_B_REPO. N-box: fill the fleet.hosts 'repo' column." >&2
      exit 1
    fi
    # Gateway mode SHIPS this recipe's own scripts (incl. provision-halo-b.sh) to
    # a temp dir on the box and points them at the repo's strix-halo-poc assets
    # via STRIX_POC_DIR -- so the box does NOT need this fleet branch checked out.
    local remote_poc="${repo%/}/deploy/recipes/strix-halo-poc"
    local remote_pii="${remote_poc}/models/pii_classifier_modernbert-base_presidio_token_model"
    ssh "${ssh_opts[@]}" "${port_opts[@]}" "${target}" "mkdir -p ${REMOTE_DIR}"
    scp "${ssh_opts[@]}" "${scp_port_opts[@]}" \
      "${SCRIPT_DIR}/fleet_lib.py" "${SCRIPT_DIR}/fleet_agent.py" "${SCRIPT_DIR}/fleet_common.sh" \
      "${SCRIPT_DIR}/node-bring-up.sh" "${SCRIPT_DIR}/gateway-bring-up.sh" \
      "${SCRIPT_DIR}/provision-halo-b.sh" \
      "${target}:${REMOTE_DIR}/"
    # Auto-provision for a REAL gateway (HALO_B_PROVISION=auto by default; set
    # HALO_B_PROVISION=skip to opt out). Idempotent, user-space only.
    if [ "${HALO_B_PROVISION:-auto}" != "skip" ]; then
      echo "    [${id}] provisioning for a real gateway (vllm-sr + PII model; first run may download) ..."
      if ! ssh "${ssh_opts[@]}" "${port_opts[@]}" "${target}" \
           "STRIX_POC_DIR=${remote_poc} HALO_B_REPO=${repo} bash ${REMOTE_DIR}/provision-halo-b.sh"; then
        echo "ERROR: '${id}' provisioning failed (see the message above)." >&2
        echo "       Fix the reported prereq, run it as a mock edge (mode=mock)," >&2
        echo "       or manage it yourself and re-run with HALO_B_PROVISION=skip." >&2
        exit 1
      fi
    else
      # Opted out: still fail fast on missing prereqs BEFORE the slow Ollama pulls.
      if ! ssh "${ssh_opts[@]}" "${port_opts[@]}" "${target}" \
           "test -f ${remote_poc}/poc-strix.yaml && test -d ${remote_pii}"; then
        echo "ERROR: '${id}' is missing strix-halo-poc prereqs under repo=${repo}:" >&2
        echo "         - ${remote_poc}/poc-strix.yaml (the gateway config), and/or" >&2
        echo "         - ${remote_pii} (the staged PII model)" >&2
        echo "       Re-run WITHOUT HALO_B_PROVISION=skip to auto-provision these, or once:" >&2
        echo "         cd ${repo} && bash deploy/recipes/strix-halo-poc/bring-up.sh" >&2
        exit 1
      fi
    fi
    echo "    [${id}] starting gateway node (assets from ${remote_poc}; model pulls may be slow) ..."
    ssh "${ssh_opts[@]}" "${port_opts[@]}" "${target}" \
      "BOX_ID=${id} CCP_URL=${CCP_URL_REMOTE} FLEET_MODE=gateway \
       FLEET_SIGNING_KEY=${FLEET_SIGNING_KEY} FLEET_TOKEN=${FLEET_TOKEN} \
       ROUTER_PORT=${ROUTER_PORT} POLL_INTERVAL=${POLL_INTERVAL} FLEET_STATE_DIR=${REMOTE_STATE} \
       STRIX_POC_DIR=${remote_poc} VLLM_SR_BIN=${VLLM_SR_BIN:-} \
       VLLM_SR_IMAGE_PULL_POLICY=${VLLM_SR_IMAGE_PULL_POLICY:-always} \
       VLLM_SR_ROUTER_IMAGE=${VLLM_SR_ROUTER_IMAGE:-} \
       DASHBOARD_ADMIN_EMAIL=${DASHBOARD_ADMIN_EMAIL} DASHBOARD_ADMIN_PASSWORD=${DASHBOARD_ADMIN_PASSWORD} \
       DASHBOARD_ADMIN_NAME='${DASHBOARD_ADMIN_NAME}' GF_SECURITY_ADMIN_USER=${GF_SECURITY_ADMIN_USER} \
       GF_SECURITY_ADMIN_PASSWORD=${GF_SECURITY_ADMIN_PASSWORD} \
       ${REMOTE_AGENT_ENV}bash ${REMOTE_DIR}/node-bring-up.sh"
  else
    ssh "${ssh_opts[@]}" "${port_opts[@]}" "${target}" "mkdir -p ${REMOTE_DIR}"
    # Ship only the self-contained recipe files a mock edge needs.
    scp "${ssh_opts[@]}" "${scp_port_opts[@]}" \
      "${SCRIPT_DIR}/fleet_lib.py" "${SCRIPT_DIR}/fleet_agent.py" "${SCRIPT_DIR}/mock_router.py" \
      "${SCRIPT_DIR}/fleet_common.sh" "${SCRIPT_DIR}/node-bring-up.sh" \
      "${target}:${REMOTE_DIR}/"
    echo "    [${id}] starting mock edge node ..."
    ssh "${ssh_opts[@]}" "${port_opts[@]}" "${target}" \
      "BOX_ID=${id} CCP_URL=${CCP_URL_REMOTE} FLEET_MODE=${mode} \
       FLEET_SIGNING_KEY=${FLEET_SIGNING_KEY} FLEET_TOKEN=${FLEET_TOKEN} \
       ROUTER_PORT=${ROUTER_PORT} POLL_INTERVAL=${POLL_INTERVAL} FLEET_STATE_DIR=${REMOTE_STATE} \
       ${REMOTE_AGENT_ENV}bash ${REMOTE_DIR}/node-bring-up.sh"
  fi
}

for idx in "${!BOX_IDS[@]}"; do
  bring_up_remote_box "${BOX_IDS[$idx]}" "${BOX_SSH[$idx]}" "${BOX_MODE[$idx]}" \
    "${BOX_REPO[$idx]:-}" "${BOX_PORT[$idx]:-}" "${BOX_KEY[$idx]:-}"
done

echo "==> [5/6] Waiting for all boxes to converge to the CCP desired config"
if CCP_URL="${CCP_URL_LOCAL}" FLEET_TOKEN="${FLEET_TOKEN}" \
   "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" wait-converged --boxes "${ALL_BOXES_CSV}" --timeout 120; then
  echo "    all boxes converged (${ALL_BOXES_CSV})."
else
  echo "ERROR: boxes did not converge in time. Inspect:" >&2
  echo "         CCP log: ${FLEET_STATE_DIR}/ccp.log" >&2
  echo "         agent logs: ${FLEET_STATE_DIR}/halo-a-agent.log ; on remotes: ${REMOTE_STATE}/<box>-agent.log" >&2
  exit 1
fi

echo "==> [6/6] Done."
CCP_URL="${CCP_URL_LOCAL}" FLEET_TOKEN="${FLEET_TOKEN}" "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" status || true
if [ "${SKIP_VERIFY:-}" = "1" ]; then
  echo "    SKIP_VERIFY=1; skipping verify-fleet.sh"
else
  echo "==> Running verify-fleet.sh"
  bash "${SCRIPT_DIR}/verify-fleet.sh" || { echo "verify-fleet.sh FAILED" >&2; exit 1; }
  # R7: verify-fleet.sh exercises the fleet-wide edit/rollback on halo-a + the
  # first remote box; for a >2-box fleet, confirm EVERY box re-converged too.
  if [ "${#BOX_IDS[@]}" -gt 1 ]; then
    echo "==> Confirming all ${#BOX_IDS[@]} remote boxes re-converged (N-box scale-out)"
    CCP_URL="${CCP_URL_LOCAL}" FLEET_TOKEN="${FLEET_TOKEN}" \
      "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" wait-converged --boxes "${ALL_BOXES_CSV}" --timeout 120 \
      || echo "WARNING: not all boxes reported final convergence (see fleetctl status)" >&2
  fi
fi

cat <<EOF

PASS: fleet is up and converged (boxes: ${ALL_BOXES_CSV}; desired=${DESIRED_MODE}).
  Demo:     bash ${SCRIPT_DIR}/demo-fleet.sh
  Re-verify: bash ${SCRIPT_DIR}/verify-fleet.sh
  Teardown: HALO_B_SSH=${BOX_SSH[0]:-} bash ${SCRIPT_DIR}/teardown-fleet-2box.sh
  Env saved: ${ENV_FILE}
EOF
