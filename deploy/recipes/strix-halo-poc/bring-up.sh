#!/usr/bin/env bash
#
# Strix Halo single-box PoC bring-up (approach C: one Ollama server, a real
# model per tier, fully offline). Run this on the Ubuntu Strix Halo box AFTER
# `git pull`. It is the executable counterpart to docs/poc/03-strix-halo-runbook.md
# sections 3, 5, and 7.
#
# What it does:
#   1. Safely provisions the digest-pinned Ollama ROCm runtime with an explicit
#      64K serving context and the five models used by auto-routed decisions.
#   2. Exports the ModernBERT PII detector to ONNX (one-time) when it is missing.
#   3. Serves the router with poc-strix.yaml on the amd platform, keeping the
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
OLLAMA_RUNTIME="${SCRIPT_DIR}/ollama-runtime.sh"

ROUTER_IMAGE="${VLLM_SR_ROUTER_IMAGE:-ghcr.io/vllm-project/semantic-router/vllm-sr-rocm:latest}"
IMAGE_PULL_POLICY="${VLLM_SR_IMAGE_PULL_POLICY:-always}"

case "${1:-}" in
  "")
    ;;
  --runtime-preflight)
    exec bash "${OLLAMA_RUNTIME}" preflight
    ;;
  --runtime-only)
    exec bash "${OLLAMA_RUNTIME}" provision
    ;;
  --runtime-proof)
    exec bash "${OLLAMA_RUNTIME}" prove
    ;;
  *)
    echo "ERROR: unknown argument '$1'." >&2
    echo "       Expected no argument, --runtime-preflight, --runtime-only, or --runtime-proof." >&2
    exit 2
    ;;
esac

# Local HF security model that needs an ONNX export (runbook section 6 / Gate B).
# The ROCm router loads token classifiers via ONNX Runtime, but the published
# ModernBERT presidio detector ships safetensors only, so model.onnx must be
# generated once from the safetensors before serve.
PII_MODEL_DIR="${SCRIPT_DIR}/models/pii_classifier_modernbert-base_presidio_token_model"
PII_ONNX_MODEL="${PII_MODEL_DIR}/onnx/model.onnx"
# One-time export venv kept outside the repo tree so it never pollutes git.
ONNX_EXPORT_VENV="${TMPDIR:-/tmp}/vllm-sr-pii-onnx-export-venv"

preflight_router_image() {
  if ! command -v vllm-sr >/dev/null 2>&1; then
    echo "ERROR: 'vllm-sr' is not installed / not found on PATH." >&2
    echo "       Install/build it first (runbook section 4), then re-run this script." >&2
    exit 1
  fi

  case "${IMAGE_PULL_POLICY}" in
    never|ifnotpresent|always) ;;
    *)
      echo "ERROR: invalid VLLM_SR_IMAGE_PULL_POLICY='${IMAGE_PULL_POLICY}'" >&2
      echo "       Expected one of: never, ifnotpresent, always" >&2
      exit 1
      ;;
  esac

  if [[ "${ROUTER_IMAGE}" == \[* || "${ROUTER_IMAGE}" == *']('* || \
        "${ROUTER_IMAGE}" == http://* || "${ROUTER_IMAGE}" == https://* ]]; then
    echo "ERROR: VLLM_SR_ROUTER_IMAGE does not look like a Docker image reference:" >&2
    echo "       ${ROUTER_IMAGE}" >&2
    echo "       Paste the bare image ref, not a Markdown link or URL, for example:" >&2
    echo "         ghcr.io/vllm-project/semantic-router/vllm-sr-rocm:latest" >&2
    exit 1
  fi

  if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker is not available to this shell." >&2
    echo "       Start Docker / check daemon permissions, then re-run." >&2
    exit 1
  fi

  if docker image inspect "${ROUTER_IMAGE}" >/dev/null 2>&1; then
    echo "    router image present locally: ${ROUTER_IMAGE}"
    return 0
  fi

  if [[ "${IMAGE_PULL_POLICY}" == "never" ]]; then
    echo "ERROR: router image is not present locally, and VLLM_SR_IMAGE_PULL_POLICY=never:" >&2
    echo "       ${ROUTER_IMAGE}" >&2
    echo "       Pre-pull the image, or rerun with VLLM_SR_IMAGE_PULL_POLICY=ifnotpresent." >&2
    if [[ -n "${VLLM_SR_ROUTER_IMAGE:-}" ]]; then
      echo "       If this pinned digest is stale, unset VLLM_SR_ROUTER_IMAGE and use :latest." >&2
    fi
    exit 1
  fi

  echo "    router image missing locally; pulling now (${IMAGE_PULL_POLICY}): ${ROUTER_IMAGE}"
  if ! docker pull "${ROUTER_IMAGE}"; then
    echo "ERROR: unable to pull router image: ${ROUTER_IMAGE}" >&2
    if [[ -n "${VLLM_SR_ROUTER_IMAGE:-}" ]]; then
      echo "       If this pinned digest is stale, unset VLLM_SR_ROUTER_IMAGE and use :latest." >&2
    fi
    exit 1
  fi
}

echo "==> [0/3] Preflighting the existing runtime and vllm-sr router image"
bash "${OLLAMA_RUNTIME}" preflight
preflight_router_image

echo "==> [1/3] Provisioning context-pinned Ollama runtime"
bash "${OLLAMA_RUNTIME}" provision

echo "==> [2/3] Ensuring the ModernBERT PII detector has an exported ONNX model"
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

echo "==> [3/3] Serving the router with $(basename "${CONFIG_PATH}") (platform amd)"
# Provision the demo UI credentials consistently across vLLM-SR Dashboard and
# Grafana. Override these envs before running for non-demo use.
export DASHBOARD_ADMIN_EMAIL="${DASHBOARD_ADMIN_EMAIL:-yingylin@amd.com}"
export DASHBOARD_ADMIN_PASSWORD="${DASHBOARD_ADMIN_PASSWORD:-aupaup123}"
export DASHBOARD_ADMIN_NAME="${DASHBOARD_ADMIN_NAME:-yingylin}"
export GF_SECURITY_ADMIN_USER="${GF_SECURITY_ADMIN_USER:-${DASHBOARD_ADMIN_EMAIL}}"
export GF_SECURITY_ADMIN_PASSWORD="${GF_SECURITY_ADMIN_PASSWORD:-${DASHBOARD_ADMIN_PASSWORD}}"
export VLLM_SR_ROUTER_IMAGE="${ROUTER_IMAGE}"
echo "    dashboard/grafana admin: ${DASHBOARD_ADMIN_EMAIL} (password hidden; override via DASHBOARD_ADMIN_PASSWORD)"
# Keep the mmBERT/embedding classifiers on CPU so the iGPU is reserved for the
# LLM backends (see runbook section 7).
export VLLM_SR_AMD_PRESERVE_CPU=1

vllm-sr serve \
  --config "${CONFIG_PATH}" \
  --image-pull-policy "${IMAGE_PULL_POLICY}" \
  --platform amd

echo "==> Bring-up complete. Verify with: vllm-sr status"
echo "    Then run the smoke test: python smoke_test.py"
