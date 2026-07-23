#!/usr/bin/env bash
#
# 2-box Strix Halo PoC: ONE-CLICK teardown (run on Halo-A, the edge box).
#
# Reverses deploy-2box.sh: stops the gateway and the frontier mock locally, and
# removes the Ollama container on Halo-B over SSH. It deliberately LEAVES the
# shared vllm-sr-network in place so the next deploy is fast (deploy-2box.sh
# recreates it idempotently anyway).
#
# It reuses the same SSH option handling and env vars as deploy-2box.sh, so the
# teardown command printed at the end of a deploy can be copy-pasted verbatim.
#
# Inputs (env vars):
#   HALO_B_IP            data-plane address of Halo-B (used to derive the SSH
#                        host when HALO_B_SSH is not set).
#   HALO_B_SSH           control address user@host; defaults its host to
#                        HALO_B_IP. If neither is set, the remote step is
#                        skipped (local-only teardown).
#   HALO_B_SSH_PORT      optional SSH port for Halo-B.
#   HALO_B_SSH_KEY       optional SSH identity file for Halo-B.
#   STOP_LOCAL_OLLAMA=1  also stop the local Ollama container on Halo-A
#                        (off by default so cached edge models are preserved).
#
# Usage (from anywhere):
#   HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 bash teardown-2box.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NETWORK="vllm-sr-network"
KATAN_NAME="llm-katan"
OLLAMA_CONTAINER="ollama"

SSH_CTRL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vllm-sr-2box-ssh.XXXXXX")"
SSH_CTRL_PATH="${SSH_CTRL_DIR}/cm-%r@%h:%p"

SSH_BASE_OPTS=()
SSH_PORT_OPTS=()

cleanup() {
  rm -rf "${SSH_CTRL_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> [1/3] Stopping the gateway on Halo-A (vllm-sr stop)"
if command -v vllm-sr >/dev/null 2>&1; then
  vllm-sr stop || echo "    (vllm-sr stop reported nothing to stop)"
else
  echo "    vllm-sr CLI not found; skipping 'vllm-sr stop'."
fi

echo "==> [2/3] Removing local frontier mock and (optionally) local Ollama"
docker rm -f "${KATAN_NAME}" 2>/dev/null && echo "    removed ${KATAN_NAME}" \
  || echo "    ${KATAN_NAME} not present."
if [[ "${STOP_LOCAL_OLLAMA:-}" == "1" ]]; then
  docker rm -f "${OLLAMA_CONTAINER}" 2>/dev/null && echo "    removed local ${OLLAMA_CONTAINER}" \
    || echo "    local ${OLLAMA_CONTAINER} not present."
else
  echo "    keeping local ${OLLAMA_CONTAINER} (set STOP_LOCAL_OLLAMA=1 to remove it)."
fi

echo "==> [3/3] Removing the Ollama container on Halo-B"
HALO_B_SSH="${HALO_B_SSH:-${HALO_B_IP:-}}"
if [[ -z "${HALO_B_SSH}" ]]; then
  echo "    HALO_B_SSH/HALO_B_IP not set; skipping the remote teardown."
else
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
  fi
  echo "    ssh ${HALO_B_SSH}: docker rm -f ${OLLAMA_CONTAINER}"
  if ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" \
    "docker rm -f ${OLLAMA_CONTAINER}" 2>/dev/null; then
    echo "    removed ${OLLAMA_CONTAINER} on Halo-B."
  else
    echo "    could not remove ${OLLAMA_CONTAINER} on Halo-B (already gone or unreachable)."
  fi
fi

echo
echo "Teardown complete. The '${NETWORK}' network is left in place for the next deploy."
echo "Re-deploy with: HALO_B_IP=... HALO_B_SSH=user@halob bash ${SCRIPT_DIR}/deploy-2box.sh"
