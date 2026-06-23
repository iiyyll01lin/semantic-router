#!/usr/bin/env bash
#
# 2-box Strix Halo PoC: ONE-CLICK orchestrator (run on Halo-A, the edge box).
#
# This is the single command that stands up the whole two-box topology from
# Halo-A. Halo-B is assumed to be BARE: it has no repo checkout and no
# passwordless SSH preconfigured. This script therefore copies the
# self-contained server-bring-up.sh over to Halo-B and runs it there over SSH,
# multiplexing the connection (ControlMaster) so the user is prompted for the
# password at most once.
#
# Flow (also see README.md "One-click" and the plan's mermaid diagram):
#   1. Preflight on Halo-A: docker / ssh / scp / curl present, vllm-sr CLI
#      installed (install from src/vllm-sr if missing), the ModernBERT PII model
#      present (HARD FAIL otherwise -- the gateway will not serve without it),
#      and the required HALO_B_IP / HALO_B_SSH inputs set.
#   2. Open one shared SSH master to Halo-B (one password for all ssh/scp).
#   3. scp server-bring-up.sh to Halo-B and ssh-run it (pulls the big models;
#      it returns only once its local Ollama answers).
#   4. Wait for Halo-B Ollama on the host, then do the make-or-break check:
#      reach HALO_B_IP:11434 from INSIDE a container on vllm-sr-network (the
#      same network the Envoy data-plane uses).
#   5. Start the llm-katan frontier mock (unless SKIP_FRONTIER=1).
#   6. Run client-bring-up.sh with HALO_B_IP exported (it renders the config and
#      serves the gateway, then returns), then poll the gateway on :8899.
#   7. Run smoke_test.py (unless SKIP_SMOKE=1) and print a PASS/FAIL summary,
#      the log locations, and the teardown command.
#
# Inputs (env vars):
#   HALO_B_IP        (required) data-plane Ollama address of Halo-B.
#   HALO_B_SSH       control address user@host; defaults its host to HALO_B_IP.
#   HALO_B_SSH_PORT  optional SSH port for Halo-B.
#   HALO_B_SSH_KEY   optional SSH identity file for Halo-B.
#   SKIP_FRONTIER=1  skip the llm-katan frontier mock.
#   SKIP_SMOKE=1     skip the final cross-box smoke test.
#   ANTHROPIC_API_KEY (optional) enables the real external Anthropic public API
#                    for the frontier/premium tier (anthropic/claude-opus-4.6).
#                    Auto-passed into the gateway container by `vllm-sr serve`.
#                    Requires Halo-A outbound HTTPS egress to api.anthropic.com:443.
#                    If unset, local tiers still work; premium requests fail.
#   DASHBOARD_ADMIN_EMAIL     (optional) demo dashboard admin email.
#                    Default: admin@demo.local (set in client-bring-up.sh).
#   DASHBOARD_ADMIN_PASSWORD  (optional) demo dashboard admin password.
#                    Default: vllmsr-demo. Override for any non-demo use.
#   DASHBOARD_ADMIN_NAME      (optional) demo dashboard admin display name.
#                    Default: Admin. These three are forwarded to the dashboard
#                    via client-bring-up.sh; EnsureBootstrapAdmin is idempotent.
#
# Usage (from anywhere):
#   HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 bash deploy-2box.sh
#
set -euo pipefail

# Resolve this script's directory so sibling scripts/configs work from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

NETWORK="vllm-sr-network"
OLLAMA_PORT="11434"
GATEWAY_URL="http://localhost:8899"

# llm-katan frontier mock (identical to ../strix-halo-poc/cpu-smoke.sh).
KATAN_NAME="llm-katan"
KATAN_IMAGE="ghcr.io/vllm-project/semantic-router/llm-katan:latest"
KATAN_PORT="8000"

# The PII ModernBERT model dir is shared with the single-box recipe (see
# client-bring-up.sh). The gateway loads it at serve time, so it is a hard
# requirement on Halo-A.
PII_MODEL_DIR="${SCRIPT_DIR}/../strix-halo-poc/models/pii_classifier_modernbert-base_presidio_token_model"

# Log + SSH control state outside the repo tree so git is never polluted.
LOG_DIR="${TMPDIR:-/tmp}/vllm-sr-2box-logs"
mkdir -p "${LOG_DIR}"
SERVER_LOG="${LOG_DIR}/server-bring-up.log"
CLIENT_LOG="${LOG_DIR}/client-bring-up.log"
SMOKE_LOG="${LOG_DIR}/smoke-test.log"

SSH_CTRL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vllm-sr-2box-ssh.XXXXXX")"
SSH_CTRL_PATH="${SSH_CTRL_DIR}/cm-%r@%h:%p"

# Declared empty up front so the EXIT trap below is safe under `set -u` even if
# we bail out during preflight, before these are populated.
SSH_BASE_OPTS=()
SSH_PORT_OPTS=()
SCP_PORT_OPTS=()

# Close the SSH master and clean up the control dir on exit (idempotent).
cleanup() {
  if [[ -n "${HALO_B_SSH:-}" ]]; then
    ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" -O exit "${HALO_B_SSH}" \
      >/dev/null 2>&1 || true
  fi
  rm -rf "${SSH_CTRL_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

# --------------------------------------------------------------------------
echo "==> [1/8] Preflight on Halo-A"

if [[ -z "${HALO_B_IP:-}" ]]; then
  echo "ERROR: HALO_B_IP is not set (the data-plane Ollama address of Halo-B)." >&2
  echo "       Re-run with, e.g.:" >&2
  echo "         HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 bash deploy-2box.sh" >&2
  exit 1
fi

# HALO_B_SSH is the control address (user@host). Default its host to HALO_B_IP
# when it is not given (ssh then uses the current user).
HALO_B_SSH="${HALO_B_SSH:-${HALO_B_IP}}"

for bin in docker ssh scp curl; do
  if ! command -v "${bin}" >/dev/null 2>&1; then
    echo "ERROR: required command '${bin}' not found on Halo-A." >&2
    exit 1
  fi
done

# vllm-sr CLI: install from src/vllm-sr if missing (mirrors cpu-smoke.sh).
if ! command -v vllm-sr >/dev/null 2>&1; then
  echo "    vllm-sr not found; installing from src/vllm-sr"
  pip install -e "${REPO_ROOT}/src/vllm-sr"
fi
vllm-sr --version

# PII model: HARD FAIL if missing (the gateway will not serve without it).
if [[ ! -d "${PII_MODEL_DIR}" ]]; then
  echo "ERROR: PII model dir is missing:" >&2
  echo "      ${PII_MODEL_DIR}" >&2
  echo "    The gateway loads the ModernBERT PII classifier at serve time, so this" >&2
  echo "    is a hard requirement. Download it first (see" >&2
  echo "    ../strix-halo-poc/REHEARSAL.md Gate B), then re-run." >&2
  exit 1
fi
echo "    HALO_B_IP=${HALO_B_IP}  HALO_B_SSH=${HALO_B_SSH}"
echo "    preflight OK."

# --------------------------------------------------------------------------
# Shared SSH/scp options. ssh takes -p PORT, scp takes -P PORT, so the port
# flag is kept in separate arrays; everything else (ControlMaster multiplexing
# and the optional identity file) is shared.
SSH_BASE_OPTS=(
  -o ControlMaster=auto
  -o ControlPath="${SSH_CTRL_PATH}"
  -o ControlPersist=2m
)
if [[ -n "${HALO_B_SSH_KEY:-}" ]]; then
  SSH_BASE_OPTS+=(-i "${HALO_B_SSH_KEY}")
fi
if [[ -n "${HALO_B_SSH_PORT:-}" ]]; then
  SSH_PORT_OPTS=(-p "${HALO_B_SSH_PORT}")
  SCP_PORT_OPTS=(-P "${HALO_B_SSH_PORT}")
fi

echo "==> [2/8] Opening SSH master to ${HALO_B_SSH} (you may be prompted once)"
if ! ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" true; then
  echo "ERROR: cannot SSH to ${HALO_B_SSH}." >&2
  echo "       This script needs SSH access to Halo-B. To avoid repeated password" >&2
  echo "       prompts, install your key once with:" >&2
  if [[ -n "${HALO_B_SSH_PORT:-}" ]]; then
    echo "         ssh-copy-id -p ${HALO_B_SSH_PORT} ${HALO_B_SSH}" >&2
  else
    echo "         ssh-copy-id ${HALO_B_SSH}" >&2
  fi
  echo "       Then re-run this script." >&2
  exit 1
fi
echo "    SSH master is up (subsequent ssh/scp calls reuse it, no re-auth)."

# --------------------------------------------------------------------------
echo "==> [3/8] Provisioning Halo-B: copying and running server-bring-up.sh"
REMOTE_DIR="~/.vllm-sr-2box"
ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" "mkdir -p ${REMOTE_DIR}"
scp "${SSH_BASE_OPTS[@]}" "${SCP_PORT_OPTS[@]}" \
  "${SCRIPT_DIR}/server-bring-up.sh" "${HALO_B_SSH}:${REMOTE_DIR}/server-bring-up.sh"
echo "    running server-bring-up.sh on Halo-B (pulls big models; may be slow) ..."
echo "    (remote log -> ${SERVER_LOG})"
ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" \
  "bash ${REMOTE_DIR}/server-bring-up.sh" 2>&1 | tee "${SERVER_LOG}"

# --------------------------------------------------------------------------
echo "==> [4/8] Waiting for Halo-B Ollama on http://${HALO_B_IP}:${OLLAMA_PORT}/api/tags"
b_ready=""
for _ in $(seq 1 30); do
  if curl -fsS "http://${HALO_B_IP}:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
    b_ready="yes"
    echo "    Halo-B Ollama is reachable from the Halo-A host."
    break
  fi
  sleep 2
done
if [[ -z "${b_ready}" ]]; then
  echo "ERROR: Halo-B Ollama did not answer on http://${HALO_B_IP}:${OLLAMA_PORT}/api/tags." >&2
  echo "       Confirm Halo-B is up and port ${OLLAMA_PORT} is open to Halo-A." >&2
  exit 1
fi

# --------------------------------------------------------------------------
echo "==> [5/8] Container-reachability check (make-or-break)"
# Ensure the shared network exists before we test from inside a container on it.
docker network create "${NETWORK}" 2>/dev/null || true
# The Envoy data-plane runs in a container on ${NETWORK}; it -- not the host --
# must reach HALO_B_IP:11434. Verify from inside a throwaway container on the
# same network.
if ! docker run --rm --network "${NETWORK}" curlimages/curl:latest \
  -fsS "http://${HALO_B_IP}:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
  echo "ERROR: a container on '${NETWORK}' cannot reach http://${HALO_B_IP}:${OLLAMA_PORT}." >&2
  echo "       The host can reach it but the data-plane container cannot, so the" >&2
  echo "       gateway's datacenter backends would fail. Remediate:" >&2
  echo "         - open the firewall on Halo-B for TCP ${OLLAMA_PORT} from Halo-A;" >&2
  echo "         - ensure HALO_B_IP is a LAN-routable address (not 127.0.0.1);" >&2
  echo "         - if Halo-B Ollama is bound to a host-only/loopback iface, bind it" >&2
  echo "           to 0.0.0.0 (server-bring-up.sh already does -p 0.0.0.0:${OLLAMA_PORT});" >&2
  echo "         - if the container must traverse the host, add a host-gateway route" >&2
  echo "           or run the data-plane with host networking." >&2
  exit 1
fi
echo "    a container on '${NETWORK}' can reach Halo-B Ollama. Good."

# --------------------------------------------------------------------------
if [[ "${SKIP_FRONTIER:-}" == "1" ]]; then
  echo "==> [6/8] SKIP_FRONTIER=1 set; skipping the llm-katan frontier mock"
else
  echo "==> [6/8] Starting llm-katan frontier mock ('${KATAN_NAME}') on ${NETWORK}:${KATAN_PORT}"
  docker rm -f "${KATAN_NAME}" 2>/dev/null || true
  docker run -d \
    --name "${KATAN_NAME}" \
    --network "${NETWORK}" \
    -p "${KATAN_PORT}:8000" \
    "${KATAN_IMAGE}" \
    llm-katan --model test-model --backend echo --served-model-name test-model --host 0.0.0.0 --port 8000
  echo "    waiting for llm-katan to answer on http://localhost:${KATAN_PORT}/health ..."
  for _ in $(seq 1 30); do
    if curl -fsS "http://localhost:${KATAN_PORT}/health" >/dev/null 2>&1; then
      echo "    llm-katan is up."
      break
    fi
    sleep 2
  done
  curl -fsS "http://localhost:${KATAN_PORT}/v1/models" >/dev/null 2>&1 \
    || echo "    (warning: /v1/models did not respond)"
fi

# --------------------------------------------------------------------------
echo "==> [7/8] Bringing up the gateway on Halo-A via client-bring-up.sh"
echo "    (log -> ${CLIENT_LOG})"
# client-bring-up.sh does the local edge models + PII ONNX export + config
# render, then `vllm-sr serve`, which RETURNS after starting the stack.
HALO_B_IP="${HALO_B_IP}" bash "${SCRIPT_DIR}/client-bring-up.sh" 2>&1 | tee "${CLIENT_LOG}"

echo "    waiting for the gateway listener at ${GATEWAY_URL} ..."
gw_ready=""
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "${GATEWAY_URL}" 2>/dev/null; then
    gw_ready="yes"
    echo "    gateway listener is responding."
    break
  fi
  sleep 5
done
if [[ -z "${gw_ready}" ]]; then
  echo "ERROR: gateway did not become ready at ${GATEWAY_URL}." >&2
  echo "       Inspect: vllm-sr status   (client log: ${CLIENT_LOG})" >&2
  exit 1
fi

# --------------------------------------------------------------------------
SMOKE_RC=0
if [[ "${SKIP_SMOKE:-}" == "1" ]]; then
  echo "==> [8/8] SKIP_SMOKE=1 set; skipping the cross-box smoke test"
else
  echo "==> [8/8] Running smoke_test.py against ${GATEWAY_URL}"
  PY_BIN="python3"
  command -v "${PY_BIN}" >/dev/null 2>&1 || PY_BIN="python"
  set +e
  "${PY_BIN}" "${SCRIPT_DIR}/smoke_test.py" --base-url "${GATEWAY_URL}" 2>&1 | tee "${SMOKE_LOG}"
  SMOKE_RC="${PIPESTATUS[0]}"
  set -e
fi

# --------------------------------------------------------------------------
echo
echo "============================================================"
if [[ "${SKIP_SMOKE:-}" == "1" ]]; then
  echo "2-box deploy: gateway is UP (smoke test skipped)."
elif [[ "${SMOKE_RC}" -eq 0 ]]; then
  echo "2-box deploy: PASS -- gateway is up and cross-box routing verified."
else
  echo "2-box deploy: FAIL -- smoke test reported problems (rc=${SMOKE_RC})."
fi
echo "------------------------------------------------------------"
echo "Logs:"
echo "    server bring-up : ${SERVER_LOG}"
echo "    client bring-up : ${CLIENT_LOG}"
echo "    smoke test      : ${SMOKE_LOG}"
echo "Gateway: ${GATEWAY_URL}   (status: vllm-sr status)"
echo "Teardown when finished:"
echo "    HALO_B_IP=${HALO_B_IP} HALO_B_SSH=${HALO_B_SSH} bash ${SCRIPT_DIR}/teardown-2box.sh"
echo "============================================================"

exit "${SMOKE_RC}"
