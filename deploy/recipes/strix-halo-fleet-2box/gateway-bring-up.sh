#!/usr/bin/env bash
#
# Bring up a REAL self-contained vllm-sr gateway on THIS box for the fleet PoC
# (FLEET_MODE=gateway). It mirrors the proven single-box recipe
# ../strix-halo-poc/bring-up.sh: local Ollama (ROCm) + tier models + the
# ModernBERT PII ONNX export, then `vllm-sr serve` on the amd platform with the
# built-in classifiers pinned to CPU (VLLM_SR_AMD_PRESERVE_CPU=1 -- required so
# that agent-triggered hot-reloads do not re-create ROCm ONNX sessions and crash
# the router).
#
# The gateway is served from a RENDERED copy of ../strix-halo-poc/poc-strix.yaml
# plus a fleet marker line, written to ${GATEWAY_CONFIG}. The fleet agent then
# manages THAT file: GET /config/hash reads it (it is the bind-mounted source
# config), and an external write triggers the router's fsnotify hot-reload.
#
# Env:
#   FLEET_STATE_DIR  (default ${TMPDIR:-/tmp}/vllm-sr-fleet)
#   GATEWAY_CONFIG   (default ${FLEET_STATE_DIR}/gateway/config.yaml) -- the file the agent manages
#   RENDER_ONLY=1    only render ${GATEWAY_CONFIG} (no Ollama/serve); used by the
#                    orchestrator to seed the CCP desired config before serving.
#   ROUTER_PORT      (default 8080) host port for the router config API.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"

STRIX_POC_DIR="${SCRIPT_DIR}/../strix-halo-poc"
SOURCE_CONFIG="${STRIX_POC_DIR}/poc-strix.yaml"
GATEWAY_DIR="${FLEET_STATE_DIR}/gateway"
GATEWAY_CONFIG="${GATEWAY_CONFIG:-${GATEWAY_DIR}/config.yaml}"

NETWORK="vllm-sr-network"
OLLAMA_CONTAINER="ollama"
OLLAMA_PORT="11434"
OLLAMA_VOLUME="ollama"
OLLAMA_IMAGE="ollama/ollama:rocm"
TIER_TAGS=("llama3.2:3b" "qwen2.5:7b" "qwen2.5:14b" "qwen3:14b" "qwen2.5:32b")

PII_MODEL_DIR="${STRIX_POC_DIR}/models/pii_classifier_modernbert-base_presidio_token_model"
PII_ONNX_MODEL="${PII_MODEL_DIR}/onnx/model.onnx"
ONNX_EXPORT_VENV="${TMPDIR:-/tmp}/vllm-sr-pii-onnx-export-venv"

# --- Render the gateway config the agent will manage ----------------------
# Deterministic across boxes: same committed poc-strix.yaml + same marker line,
# so both boxes' source-config hash matches the CCP's desired-bundle hash.
render_config() {
  if [[ ! -f "${SOURCE_CONFIG}" ]]; then
    echo "ERROR: missing ${SOURCE_CONFIG}. Gateway mode reuses the strix-halo-poc config." >&2
    exit 1
  fi
  mkdir -p "${GATEWAY_DIR}"
  {
    cat "${SOURCE_CONFIG}"
    printf '\n# fleet-rule-marker: rule-set A (edit me once at the CCP)\n'
  } >"${GATEWAY_CONFIG}"
  # vllm-sr serve mounts <config_dir>/models -> /app/models; point it at the
  # pre-staged strix-halo-poc models (PII detector + any local assets).
  if [[ -d "${GATEWAY_DIR}/models" && ! -L "${GATEWAY_DIR}/models" ]]; then
    rm -rf "${GATEWAY_DIR}/models"
  fi
  ln -sfn "${STRIX_POC_DIR}/models" "${GATEWAY_DIR}/models"
  echo "    rendered gateway config -> ${GATEWAY_CONFIG} (models -> strix-halo-poc/models)"
}

echo "==> [gateway] rendering config"
render_config
if [[ "${RENDER_ONLY:-}" == "1" ]]; then
  echo "    RENDER_ONLY=1; done (config at ${GATEWAY_CONFIG})"
  exit 0
fi

echo "==> [gateway] ensuring Docker network '${NETWORK}'"
docker network create "${NETWORK}" 2>/dev/null || true

echo "==> [gateway] starting Ollama (ROCm) '${OLLAMA_CONTAINER}'"
if docker ps -a --format '{{.Names}}' | grep -qx "${OLLAMA_CONTAINER}"; then
  docker start "${OLLAMA_CONTAINER}" >/dev/null
else
  docker run -d --name "${OLLAMA_CONTAINER}" --network="${NETWORK}" --restart unless-stopped \
    -p "${OLLAMA_PORT}:${OLLAMA_PORT}" -v "${OLLAMA_VOLUME}:/root/.ollama" \
    --device=/dev/kfd --device=/dev/dri --group-add=video \
    --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
    -e HSA_OVERRIDE_GFX_VERSION=11.5.1 "${OLLAMA_IMAGE}" >/dev/null
fi
if ! fleet_wait_http "http://localhost:${OLLAMA_PORT}/api/tags" 30; then
  echo "ERROR: local Ollama did not come up on :${OLLAMA_PORT}" >&2
  exit 1
fi

echo "==> [gateway] pulling tier models (idempotent)"
for tag in "${TIER_TAGS[@]}"; do
  echo "    pulling ${tag}"
  docker exec "${OLLAMA_CONTAINER}" ollama pull "${tag}"
done

echo "==> [gateway] ensuring the ModernBERT PII ONNX model"
if [[ -f "${PII_ONNX_MODEL}" ]]; then
  echo "    onnx/model.onnx present; skipping export"
elif [[ ! -d "${PII_MODEL_DIR}" ]]; then
  echo "ERROR: PII model dir missing: ${PII_MODEL_DIR}" >&2
  echo "       Run the strix-halo-poc Gate B download on this box first (REHEARSAL.md)." >&2
  exit 1
else
  PY_BIN="$(fleet_pybin)"
  [[ -x "${ONNX_EXPORT_VENV}/bin/python" ]] || "${PY_BIN}" -m venv "${ONNX_EXPORT_VENV}"
  "${ONNX_EXPORT_VENV}/bin/pip" install --quiet --upgrade pip
  "${ONNX_EXPORT_VENV}/bin/pip" install --quiet "transformers>=4.48" "optimum[onnxruntime]" onnx torch
  "${ONNX_EXPORT_VENV}/bin/python" - "${PII_MODEL_DIR}" <<'PY'
import sys
from optimum.onnxruntime import ORTModelForTokenClassification
from transformers import AutoTokenizer
src = sys.argv[1]; out = src + "/onnx"
ORTModelForTokenClassification.from_pretrained(src, export=True).save_pretrained(out)
AutoTokenizer.from_pretrained(src).save_pretrained(out)
PY
  [[ -f "${PII_ONNX_MODEL}" ]] || { echo "ERROR: ONNX export failed" >&2; exit 1; }
fi

echo "==> [gateway] serving vllm-sr (platform amd, classifiers pinned to CPU)"
# VLLM_SR_AMD_PRESERVE_CPU=1 is REQUIRED: it reaches the container so that the
# agent-triggered hot-reload keeps classifiers on CPU instead of re-creating
# ROCm ONNX sessions (which would crash the router).
export VLLM_SR_AMD_PRESERVE_CPU=1
vllm-sr serve --config "${GATEWAY_CONFIG}" --image-pull-policy never --platform amd

echo "==> [gateway] waiting for the router config API on :${ROUTER_PORT}"
if fleet_wait_http "http://localhost:${ROUTER_PORT}/config/hash" 60; then
  echo "    gateway router config API is up (GET /config/hash)."
else
  echo "ERROR: router config API never answered on :${ROUTER_PORT}/config/hash." >&2
  echo "       Check: vllm-sr status ; docker logs for the router container." >&2
  exit 1
fi
