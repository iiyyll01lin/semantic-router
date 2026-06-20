#!/usr/bin/env bash
#
# Strix Halo single-box PoC bring-up (approach C: one Ollama server, a real
# model per tier, fully offline). Run this on the Ubuntu Strix Halo box AFTER
# `git pull`. It is the executable counterpart to docs/poc/03-strix-halo-runbook.md
# sections 3, 5, and 7.
#
# What it does:
#   1. Creates the shared Docker network `vllm-sr-network` (router default).
#   2. Starts `ollama/ollama:rocm` with AMD GPU passthrough, named `ollama`.
#   3. Pulls the five tier models into the container.
#   4. Serves the router with poc-strix.yaml on the amd platform, keeping the
#      built-in classifiers on CPU (VLLM_SR_AMD_PRESERVE_CPU=1).
#
# Prerequisites (see runbook section 1): Ubuntu x86_64, ROCm for gfx1151,
# Docker with /dev/kfd + /dev/dri passthrough, the user in the video/render
# groups, and the `vllm-sr` CLI installed/built (runbook section 4).
#
set -euo pipefail

# Resolve this script's directory so the config path works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${SCRIPT_DIR}/poc-strix.yaml"

NETWORK="vllm-sr-network"
OLLAMA_CONTAINER="ollama"
OLLAMA_PORT="11434"
OLLAMA_VOLUME="ollama"
OLLAMA_IMAGE="ollama/ollama:rocm"

# One genuinely different model per tier (runbook section 2.2 / 5.1).
TIER_TAGS=(
  "llama3.2:3b"   # SIMPLE  / default model
  "qwen2.5:7b"    # MEDIUM
  "qwen2.5:14b"   # COMPLEX
  "qwen3:14b"     # REASONING
  "qwen2.5:32b"   # PREMIUM (offline, largest local model)
)

echo "==> [1/4] Ensuring Docker network '${NETWORK}' exists"
docker network create "${NETWORK}" 2>/dev/null || true

echo "==> [2/4] Starting Ollama (ROCm) container '${OLLAMA_CONTAINER}' on ${NETWORK}"
if docker ps -a --format '{{.Names}}' | grep -qx "${OLLAMA_CONTAINER}"; then
  echo "    container '${OLLAMA_CONTAINER}' already exists; (re)starting it"
  docker start "${OLLAMA_CONTAINER}"
else
  docker run -d \
    --name "${OLLAMA_CONTAINER}" \
    --network="${NETWORK}" \
    --restart unless-stopped \
    -p "${OLLAMA_PORT}:${OLLAMA_PORT}" \
    -v "${OLLAMA_VOLUME}:/root/.ollama" \
    --device=/dev/kfd \
    --device=/dev/dri \
    --group-add=video \
    --cap-add=SYS_PTRACE \
    --security-opt seccomp=unconfined \
    -e HSA_OVERRIDE_GFX_VERSION=11.5.1 \
    "${OLLAMA_IMAGE}"
fi

echo "==> [3/4] Pulling tier models into the '${OLLAMA_CONTAINER}' container"
for tag in "${TIER_TAGS[@]}"; do
  echo "    pulling ${tag}"
  docker exec "${OLLAMA_CONTAINER}" ollama pull "${tag}"
done

echo "==> [4/4] Serving the router with $(basename "${CONFIG_PATH}") (platform amd)"
# Keep the mmBERT/embedding classifiers on CPU so the iGPU is reserved for the
# LLM backends (see runbook section 7).
export VLLM_SR_AMD_PRESERVE_CPU=1

vllm-sr serve \
  --config "${CONFIG_PATH}" \
  --image-pull-policy never \
  --platform amd

echo "==> Bring-up complete. Verify with: vllm-sr status"
echo "    Then run the smoke test: python smoke_test.py"
