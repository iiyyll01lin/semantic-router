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
#   4. Exports the ModernBERT PII detector to ONNX (one-time) when it is missing.
#   5. Serves the router with poc-strix.yaml on the amd platform, keeping the
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

# Local HF security model that needs an ONNX export (runbook section 6 / Gate B).
# The ROCm router loads token classifiers via ONNX Runtime, but the published
# ModernBERT presidio detector ships safetensors only, so model.onnx must be
# generated once from the safetensors before serve.
PII_MODEL_DIR="${SCRIPT_DIR}/models/pii_classifier_modernbert-base_presidio_token_model"
PII_ONNX_MODEL="${PII_MODEL_DIR}/onnx/model.onnx"
# One-time export venv kept outside the repo tree so it never pollutes git.
ONNX_EXPORT_VENV="${TMPDIR:-/tmp}/vllm-sr-pii-onnx-export-venv"

echo "==> [1/5] Ensuring Docker network '${NETWORK}' exists"
docker network create "${NETWORK}" 2>/dev/null || true

echo "==> [2/5] Starting Ollama (ROCm) container '${OLLAMA_CONTAINER}' on ${NETWORK}"
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

echo "==> [3/5] Pulling tier models into the '${OLLAMA_CONTAINER}' container"
for tag in "${TIER_TAGS[@]}"; do
  echo "    pulling ${tag}"
  docker exec "${OLLAMA_CONTAINER}" ollama pull "${tag}"
done

echo "==> [4/5] Ensuring the ModernBERT PII detector has an exported ONNX model"
# Idempotent: skip when model.onnx already exists; export it once otherwise. We
# call the venv binaries directly (no `source activate`) so the step is safe
# under `set -euo pipefail`.
if [[ -f "${PII_ONNX_MODEL}" ]]; then
  echo "    onnx/model.onnx already present; skipping export"
elif [[ ! -d "${PII_MODEL_DIR}" ]]; then
  echo "    WARNING: PII model dir is missing:" >&2
  echo "      ${PII_MODEL_DIR}" >&2
  echo "    Download it first (see REHEARSAL.md Gate B), then re-run this script." >&2
else
  echo "    exporting ONNX from safetensors via optimum (one-time, may take a while)"
  PY_BIN="python3"
  command -v "${PY_BIN}" >/dev/null 2>&1 || PY_BIN="python"
  if [[ ! -x "${ONNX_EXPORT_VENV}/bin/python" ]]; then
    "${PY_BIN}" -m venv "${ONNX_EXPORT_VENV}"
  fi
  "${ONNX_EXPORT_VENV}/bin/pip" install --quiet --upgrade pip
  "${ONNX_EXPORT_VENV}/bin/pip" install --quiet \
    "transformers>=4.48" "optimum[onnxruntime]" onnx torch
  "${ONNX_EXPORT_VENV}/bin/python" - "${PII_MODEL_DIR}" <<'PY'
import sys

from optimum.onnxruntime import ORTModelForTokenClassification
from transformers import AutoTokenizer

src = sys.argv[1]
out = src + "/onnx"
ORTModelForTokenClassification.from_pretrained(src, export=True).save_pretrained(out)
AutoTokenizer.from_pretrained(src).save_pretrained(out)
print("    exported ONNX to", out)
PY
  if [[ ! -f "${PII_ONNX_MODEL}" ]]; then
    echo "    ERROR: export finished but ${PII_ONNX_MODEL} is still missing" >&2
    exit 1
  fi
  echo "    onnx/model.onnx exported successfully"
fi

echo "==> [5/5] Serving the router with $(basename "${CONFIG_PATH}") (platform amd)"
# Keep the mmBERT/embedding classifiers on CPU so the iGPU is reserved for the
# LLM backends (see runbook section 7).
export VLLM_SR_AMD_PRESERVE_CPU=1

vllm-sr serve \
  --config "${CONFIG_PATH}" \
  --image-pull-policy never \
  --platform amd

echo "==> Bring-up complete. Verify with: vllm-sr status"
echo "    Then run the smoke test: python smoke_test.py"
