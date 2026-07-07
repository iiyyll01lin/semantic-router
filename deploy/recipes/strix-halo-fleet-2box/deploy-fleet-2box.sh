#!/usr/bin/env bash
#
# strix-halo-fleet-2box: ONE-CLICK pull-mode fleet config control plane.
#
# Run on Halo-A. Stands up the Central Control Plane (CCP) on Halo-A, an edge
# node (router + pull agent) on Halo-A, and an edge node on a BARE Halo-B over
# SSH/scp, then waits until BOTH boxes have converged to the CCP's desired
# config. Implements PL-0036 phases P1-P4 (fan-out, audit, signing, pull agent).
#
# Default FLEET_MODE=mock runs a stdlib mock router on each box so the whole
# fan-out is verifiable WITHOUT ROCm. Set FLEET_MODE=gateway to point the agents
# at real `vllm-sr serve` routers you started on each box (see README).
#
# Required env:
#   HALO_A_IP    address of THIS box (Halo-A) reachable FROM Halo-B (CCP URL).
#   HALO_B_IP    address of Halo-B (used for the convergence reachability note).
#   HALO_B_SSH   user@host control address for Halo-B.
# Optional env:
#   HALO_B_SSH_PORT, HALO_B_SSH_KEY, FLEET_MODE (mock), CCP_PORT (9300),
#   ROUTER_PORT (8080), POLL_INTERVAL (3), SKIP_VERIFY=1
#
# Usage:
#   HALO_A_IP=192.0.2.10 HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 \
#     bash deploy-fleet-2box.sh
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"

FLEET_MODE="${FLEET_MODE:-mock}"
# Per-box mode: each box defaults to FLEET_MODE but can be overridden so a capable
# box runs a REAL gateway while a minimal box runs the lightweight mock edge, e.g.
#   HALO_A_MODE=gateway HALO_B_MODE=mock bash deploy-fleet-2box.sh
# The CCP's desired config is a real gateway config if EITHER box is a gateway
# (mock routers accept any bytes and just report their content hash).
HALO_A_MODE="${HALO_A_MODE:-${FLEET_MODE}}"
HALO_B_MODE="${HALO_B_MODE:-${FLEET_MODE}}"
if [ "${HALO_A_MODE}" = "gateway" ] || [ "${HALO_B_MODE}" = "gateway" ]; then
  DESIRED_MODE="gateway"
else
  DESIRED_MODE="mock"
fi
DASHBOARD_ADMIN_EMAIL="${DASHBOARD_ADMIN_EMAIL:-yingylin@amd.com}"
DASHBOARD_ADMIN_PASSWORD="${DASHBOARD_ADMIN_PASSWORD:-aupaup123}"
DASHBOARD_ADMIN_NAME="${DASHBOARD_ADMIN_NAME:-yingylin}"
GF_SECURITY_ADMIN_USER="${GF_SECURITY_ADMIN_USER:-${DASHBOARD_ADMIN_EMAIL}}"
GF_SECURITY_ADMIN_PASSWORD="${GF_SECURITY_ADMIN_PASSWORD:-${DASHBOARD_ADMIN_PASSWORD}}"
export DASHBOARD_ADMIN_EMAIL DASHBOARD_ADMIN_PASSWORD DASHBOARD_ADMIN_NAME
export GF_SECURITY_ADMIN_USER GF_SECURITY_ADMIN_PASSWORD
PYBIN="$(fleet_pybin)"
REMOTE_DIR="~/.vllm-sr-fleet-2box"
REMOTE_STATE="\${TMPDIR:-/tmp}/vllm-sr-fleet"

SSH_CTRL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vllm-sr-fleet-ssh.XXXXXX")"
SSH_CTRL_PATH="${SSH_CTRL_DIR}/cm-%r@%h:%p"
SSH_BASE_OPTS=()
SSH_PORT_OPTS=()
SCP_PORT_OPTS=()

cleanup() {
  if [ -n "${HALO_B_SSH:-}" ] && [ "${#SSH_BASE_OPTS[@]}" -gt 0 ]; then
    ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" -O exit "${HALO_B_SSH}" >/dev/null 2>&1 || true
  fi
  rm -rf "${SSH_CTRL_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> [1/6] Preflight on Halo-A"
: "${HALO_A_IP:?set HALO_A_IP (this box address, reachable from Halo-B)}"
: "${HALO_B_IP:?set HALO_B_IP}"
: "${HALO_B_SSH:?set HALO_B_SSH (user@host for Halo-B)}"
for bin in "${PYBIN}" ssh scp curl; do
  command -v "${bin}" >/dev/null 2>&1 || { echo "ERROR: '${bin}' not found on Halo-A" >&2; exit 1; }
done

# Generate the per-deployment signing key and use a demo-friendly CCP bearer
# token by default. FLEET_SIGNING_KEY is not a login password; keep it random
# unless explicitly overridden.
FLEET_SIGNING_KEY="${FLEET_SIGNING_KEY:-$("${PYBIN}" -c 'import secrets; print(secrets.token_hex(32))')}"
FLEET_TOKEN="${FLEET_TOKEN:-aupaup123}"
CCP_URL_LOCAL="http://localhost:${CCP_PORT}"
CCP_URL_REMOTE="http://${HALO_A_IP}:${CCP_PORT}"

# Persist the deployment env so verify/demo/teardown can reuse it.
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
export HALO_B_MODE=${HALO_B_MODE}
export HALO_A_IP=${HALO_A_IP}
export HALO_B_IP=${HALO_B_IP}
export HALO_B_SSH=${HALO_B_SSH}
EOF
echo "    modes: halo-a=${HALO_A_MODE} halo-b=${HALO_B_MODE} (desired=${DESIRED_MODE})  CCP=:${CCP_PORT}  state=${FLEET_STATE_DIR}"

# In gateway mode, render the REAL gateway config on Halo-A and serve it as the
# CCP desired (both real gateways converge to it). Gateway mode also needs the
# semantic-router repo + models (strix-halo-poc Gate B) present on Halo-B.
CCP_INIT_CONFIG="${SCRIPT_DIR}/sample-desired-config.yaml"
if [ "${HALO_B_MODE}" = "gateway" ]; then
  : "${HALO_B_REPO:?HALO_B_MODE=gateway needs HALO_B_REPO (path to the semantic-router repo on Halo-B)}"
fi
if [ "${DESIRED_MODE}" = "gateway" ]; then
  # A real gateway is in the fleet, so the CCP desired must be a valid gateway
  # config, rendered locally on Halo-A from its strix-halo-poc assets. (A mock
  # edge just stores the bytes and reports their hash, so it converges too.)
  echo "    rendering the real gateway config for the CCP desired"
  RENDER_ONLY=1 FLEET_STATE_DIR="${FLEET_STATE_DIR}" \
  GATEWAY_CONFIG="${FLEET_STATE_DIR}/gateway/config.yaml" \
    bash "${SCRIPT_DIR}/gateway-bring-up.sh"
  CCP_INIT_CONFIG="${FLEET_STATE_DIR}/gateway/config.yaml"
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

echo "==> [4/6] Provisioning Halo-B over SSH (mode=${HALO_B_MODE})"
SSH_BASE_OPTS=(-o ControlMaster=auto -o ControlPath="${SSH_CTRL_PATH}" -o ControlPersist=2m)
[ -n "${HALO_B_SSH_KEY:-}" ] && SSH_BASE_OPTS+=(-i "${HALO_B_SSH_KEY}")
if [ -n "${HALO_B_SSH_PORT:-}" ]; then
  SSH_PORT_OPTS=(-p "${HALO_B_SSH_PORT}")
  SCP_PORT_OPTS=(-P "${HALO_B_SSH_PORT}")
fi
if ! ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" true; then
  echo "ERROR: cannot SSH to ${HALO_B_SSH}. Install your key once:" >&2
  echo "         ssh-copy-id ${HALO_B_SSH_PORT:+-p ${HALO_B_SSH_PORT}} ${HALO_B_SSH}" >&2
  exit 1
fi
if [ "${HALO_B_MODE}" = "gateway" ]; then
  # Gateway mode SHIPS this recipe's own scripts (incl. provision-halo-b.sh) to a
  # temp dir on Halo-B and points them at the repo's strix-halo-poc assets via
  # STRIX_POC_DIR -- so Halo-B does NOT need this fleet branch checked out. The
  # shipped provisioner then makes Halo-B gateway-ready in one shot: it installs
  # vllm-sr if missing and downloads the (public) PII source model if absent.
  REMOTE_POC="${HALO_B_REPO%/}/deploy/recipes/strix-halo-poc"
  REMOTE_PII_DIR="${REMOTE_POC}/models/pii_classifier_modernbert-base_presidio_token_model"
  ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" "mkdir -p ${REMOTE_DIR}"
  # Ship the self-contained gateway scripts (no mock_router.py); they target the
  # repo's strix-halo-poc via STRIX_POC_DIR below, and provision-halo-b.sh runs
  # natively on Halo-B (no fragile multi-shell SSH quoting).
  scp "${SSH_BASE_OPTS[@]}" "${SCP_PORT_OPTS[@]}" \
    "${SCRIPT_DIR}/fleet_lib.py" "${SCRIPT_DIR}/fleet_agent.py" "${SCRIPT_DIR}/fleet_common.sh" \
    "${SCRIPT_DIR}/node-bring-up.sh" "${SCRIPT_DIR}/gateway-bring-up.sh" \
    "${SCRIPT_DIR}/provision-halo-b.sh" \
    "${HALO_B_SSH}:${REMOTE_DIR}/"
  # Auto-provision Halo-B for a REAL gateway (HALO_B_PROVISION=auto by default;
  # set HALO_B_PROVISION=skip to opt out). The provisioner is idempotent and
  # user-space only (pip --user, no sudo): it installs vllm-sr if missing and
  # downloads the public PII source model if absent. It fails fast with the exact
  # fix if poc-strix.yaml is absent (a checkout problem it must not auto-fix).
  if [ "${HALO_B_PROVISION:-auto}" != "skip" ]; then
    echo "    provisioning Halo-B for a real gateway (vllm-sr + PII model; first run may download) ..."
    if ! ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" \
         "STRIX_POC_DIR=${REMOTE_POC} HALO_B_REPO=${HALO_B_REPO} bash ${REMOTE_DIR}/provision-halo-b.sh"; then
      echo "ERROR: Halo-B provisioning failed (see the message above)." >&2
      echo "       Fix the reported prereq on Halo-B, or run it as a mock edge: HALO_B_MODE=mock" >&2
      echo "       (or manage Halo-B yourself and re-run with HALO_B_PROVISION=skip)." >&2
      exit 1
    fi
  else
    # Provisioning opted out: still fail fast on missing prereqs BEFORE the slow
    # (~40GB) Ollama pulls, reporting exactly which asset is absent.
    if ! ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" \
         "test -f ${REMOTE_POC}/poc-strix.yaml && test -d ${REMOTE_PII_DIR}"; then
      echo "ERROR: Halo-B is missing strix-halo-poc prereqs under HALO_B_REPO=${HALO_B_REPO}:" >&2
      echo "         - ${REMOTE_POC}/poc-strix.yaml (the gateway config), and/or" >&2
      echo "         - ${REMOTE_PII_DIR} (the staged PII model)" >&2
      echo "       You set HALO_B_PROVISION=skip; re-run WITHOUT it to auto-provision these," >&2
      echo "       or set them up on Halo-B once: cd ${HALO_B_REPO} && bash deploy/recipes/strix-halo-poc/bring-up.sh" >&2
      exit 1
    fi
  fi
  echo "    starting Halo-B gateway node (assets from ${REMOTE_POC}; model pulls may be slow) ..."
  ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" \
    "BOX_ID=halo-b CCP_URL=${CCP_URL_REMOTE} FLEET_MODE=gateway \
     FLEET_SIGNING_KEY=${FLEET_SIGNING_KEY} FLEET_TOKEN=${FLEET_TOKEN} \
     ROUTER_PORT=${ROUTER_PORT} POLL_INTERVAL=${POLL_INTERVAL} FLEET_STATE_DIR=${REMOTE_STATE} \
     STRIX_POC_DIR=${REMOTE_POC} VLLM_SR_BIN=${VLLM_SR_BIN:-} \
    VLLM_SR_IMAGE_PULL_POLICY=${VLLM_SR_IMAGE_PULL_POLICY:-always} \
     VLLM_SR_ROUTER_IMAGE=${VLLM_SR_ROUTER_IMAGE:-} \
    DASHBOARD_ADMIN_EMAIL=${DASHBOARD_ADMIN_EMAIL} DASHBOARD_ADMIN_PASSWORD=${DASHBOARD_ADMIN_PASSWORD} \
    DASHBOARD_ADMIN_NAME='${DASHBOARD_ADMIN_NAME}' GF_SECURITY_ADMIN_USER=${GF_SECURITY_ADMIN_USER} \
    GF_SECURITY_ADMIN_PASSWORD=${GF_SECURITY_ADMIN_PASSWORD} \
     bash ${REMOTE_DIR}/node-bring-up.sh"
else
  ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" "mkdir -p ${REMOTE_DIR}"
  # Ship only the self-contained recipe files Halo-B needs (stdlib python + scripts).
  scp "${SSH_BASE_OPTS[@]}" "${SCP_PORT_OPTS[@]}" \
    "${SCRIPT_DIR}/fleet_lib.py" "${SCRIPT_DIR}/fleet_agent.py" "${SCRIPT_DIR}/mock_router.py" \
    "${SCRIPT_DIR}/fleet_common.sh" "${SCRIPT_DIR}/node-bring-up.sh" \
    "${HALO_B_SSH}:${REMOTE_DIR}/"
  echo "    starting Halo-B edge node ..."
  ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" \
    "BOX_ID=halo-b CCP_URL=${CCP_URL_REMOTE} FLEET_MODE=${HALO_B_MODE} \
     FLEET_SIGNING_KEY=${FLEET_SIGNING_KEY} FLEET_TOKEN=${FLEET_TOKEN} \
     ROUTER_PORT=${ROUTER_PORT} POLL_INTERVAL=${POLL_INTERVAL} FLEET_STATE_DIR=${REMOTE_STATE} \
     bash ${REMOTE_DIR}/node-bring-up.sh"
fi

echo "==> [5/6] Waiting for both boxes to converge to the CCP desired config"
if CCP_URL="${CCP_URL_LOCAL}" FLEET_TOKEN="${FLEET_TOKEN}" \
   "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" wait-converged --boxes halo-a,halo-b --timeout 120; then
  echo "    both boxes converged."
else
  echo "ERROR: boxes did not converge in time. Inspect:" >&2
  echo "         CCP log: ${FLEET_STATE_DIR}/ccp.log" >&2
  echo "         agent logs: ${FLEET_STATE_DIR}/halo-a-agent.log ; on Halo-B: ${REMOTE_STATE}/halo-b-agent.log" >&2
  exit 1
fi

echo "==> [6/6] Done."
CCP_URL="${CCP_URL_LOCAL}" FLEET_TOKEN="${FLEET_TOKEN}" "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" status || true
if [ "${SKIP_VERIFY:-}" = "1" ]; then
  echo "    SKIP_VERIFY=1; skipping verify-fleet.sh"
else
  echo "==> Running verify-fleet.sh"
  bash "${SCRIPT_DIR}/verify-fleet.sh" || { echo "verify-fleet.sh FAILED" >&2; exit 1; }
fi

cat <<EOF

PASS: fleet is up and converged (halo-a=${HALO_A_MODE}, halo-b=${HALO_B_MODE}).
  Demo:     bash ${SCRIPT_DIR}/demo-fleet.sh
  Re-verify: bash ${SCRIPT_DIR}/verify-fleet.sh
  Teardown: HALO_B_SSH=${HALO_B_SSH} bash ${SCRIPT_DIR}/teardown-fleet-2box.sh
  Env saved: ${ENV_FILE}
EOF
