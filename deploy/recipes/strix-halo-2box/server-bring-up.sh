#!/usr/bin/env bash
#
# 2-box Strix Halo PoC: SERVER (datacenter) bring-up.
#
# Run this on Halo-B, the box that PLAYS the Instinct datacenter. It serves ONLY
# the big/datacenter tier models as a plain Ollama endpoint. There is NO router
# here: no vllm-sr serve, no PII/ONNX export, no Envoy. Halo-B is just a remote
# Ollama that the gateway on Halo-A escalates hard requests to (1 network hop).
#
# What it does:
#   1. Creates the shared Docker network `vllm-sr-network`.
#   2. Starts `ollama/ollama:rocm` with AMD GPU passthrough, bound to
#      0.0.0.0:11434 so the gateway on Halo-A can reach it over the network.
#   3. Pulls ONLY the big tier models: qwen2.5:14b, qwen3:14b, qwen2.5:32b.
#
# Prerequisites (same AMD/ROCm prereqs as ../strix-halo-poc/bring-up.sh):
# Ubuntu x86_64, ROCm for gfx1151, Docker with /dev/kfd + /dev/dri passthrough,
# and the user in the video/render groups. The `vllm-sr` CLI is NOT required on
# this box.
#
# NETWORKING: Halo-B must expose port 11434 to Halo-A (open the firewall / ensure
# the boxes are on the same routable network). The gateway config on Halo-A
# points its datacenter backends at HALO_B_IP:11434 (a routable IP, NOT the
# docker-network DNS name `ollama`). See README.md (NETWORKING).
#
set -euo pipefail

NETWORK="vllm-sr-network"
OLLAMA_CONTAINER="ollama"
OLLAMA_PORT="11434"
OLLAMA_VOLUME="ollama"
OLLAMA_IMAGE="ollama/ollama:rocm"

# The big/datacenter tier models only (the small/edge models live on Halo-A).
DATACENTER_TAGS=(
  "qwen2.5:14b"   # COMPLEX  -> google/gemini-3.1-pro
  "qwen3:14b"     # REASONING -> openai/gpt5.4
  "qwen2.5:32b"   # PREMIUM (largest local model)
)

echo "==> [1/3] Ensuring Docker network '${NETWORK}' exists"
docker network create "${NETWORK}" 2>/dev/null || true

echo "==> [2/3] Starting Ollama (ROCm) container '${OLLAMA_CONTAINER}' on 0.0.0.0:${OLLAMA_PORT}"
if docker ps -a --format '{{.Names}}' | grep -qx "${OLLAMA_CONTAINER}"; then
  echo "    container '${OLLAMA_CONTAINER}' already exists; (re)starting it"
  docker start "${OLLAMA_CONTAINER}"
else
  docker run -d \
    --name "${OLLAMA_CONTAINER}" \
    --network="${NETWORK}" \
    --restart unless-stopped \
    -p "0.0.0.0:${OLLAMA_PORT}:${OLLAMA_PORT}" \
    -v "${OLLAMA_VOLUME}:/root/.ollama" \
    --device=/dev/kfd \
    --device=/dev/dri \
    --group-add=video \
    --cap-add=SYS_PTRACE \
    --security-opt seccomp=unconfined \
    -e HSA_OVERRIDE_GFX_VERSION=11.5.1 \
    "${OLLAMA_IMAGE}"
fi

echo "==> [3/3] Pulling datacenter tier models into the '${OLLAMA_CONTAINER}' container"
for tag in "${DATACENTER_TAGS[@]}"; do
  echo "    pulling ${tag}"
  docker exec "${OLLAMA_CONTAINER}" ollama pull "${tag}"
done

# Readiness poll: when this script is driven over SSH by deploy-2box.sh on
# Halo-A, the caller waits for it to return. Only return once Ollama actually
# answers on the local endpoint, so the orchestrator never races ahead of a
# not-yet-listening backend.
echo "    waiting for Ollama to answer on http://localhost:${OLLAMA_PORT}/api/tags ..."
ollama_ready=""
for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
    ollama_ready="yes"
    echo "    Ollama is up."
    break
  fi
  sleep 2
done
if [[ -z "${ollama_ready}" ]]; then
  echo "ERROR: Ollama did not become ready on http://localhost:${OLLAMA_PORT}/api/tags." >&2
  echo "       Check the container logs: docker logs ${OLLAMA_CONTAINER}" >&2
  exit 1
fi

echo "==> Server bring-up complete (Halo-B = datacenter Ollama, no router)."
echo "    Verify locally:        curl http://localhost:11434/api/tags"
echo "    Verify from Halo-A:    curl http://<HALO_B_IP>:11434/api/tags"
echo "    Then on Halo-A, set HALO_B_IP and run: bash client-bring-up.sh"
