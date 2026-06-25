#!/usr/bin/env bash
#
# 2-box Strix Halo PoC: CLIENT (edge gateway) bring-up.
#
# Run this on Halo-A, the CLIENT/EDGE box. It co-locates the LLM Gateway (Envoy +
# semantic router) with the small/cheap models, so routine requests are answered
# locally (0 network hops) and only hard requests escalate to Halo-B (1 hop).
# This is the executable counterpart of poc-client-edge.yaml.
#
# What it does:
#   1. Requires HALO_B_IP (the routable address of the Halo-B datacenter box).
#   2. Creates the shared Docker network `vllm-sr-network`.
#   3. Starts the local `ollama/ollama:rocm` with AMD GPU passthrough and pulls
#      ONLY the small/edge models: llama3.2:3b, qwen2.5:7b.
#   4. Exports the ModernBERT PII detector to ONNX (one-time) when it is missing.
#   5. Renders a runtime copy of poc-client-edge.yaml with HALO_B_IP substituted.
#   6. Serves the gateway with the rendered config on the amd platform, keeping
#      the built-in classifiers on CPU (VLLM_SR_AMD_PRESERVE_CPU=1).
#
# Prerequisites (see ../strix-halo-poc/REHEARSAL.md): Ubuntu x86_64, ROCm for
# gfx1151, Docker with /dev/kfd + /dev/dri passthrough, the user in the
# video/render groups, and the `vllm-sr` CLI installed/built.
#
# Run Halo-B first: bash server-bring-up.sh (on Halo-B), then on Halo-A:
#   export HALO_B_IP=192.0.2.20    # the real Halo-B address
#   bash client-bring-up.sh
#
set -euo pipefail

# Resolve this script's directory so the config paths work from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_TEMPLATE="${SCRIPT_DIR}/poc-client-edge.yaml"
RENDER_DIR="${SCRIPT_DIR}/.vllm-sr-rendered"
RENDERED_CONFIG="${RENDER_DIR}/poc-client-edge.yaml"

NETWORK="vllm-sr-network"
OLLAMA_CONTAINER="ollama"
OLLAMA_PORT="11434"
OLLAMA_VOLUME="ollama"
OLLAMA_IMAGE="ollama/ollama:rocm"

# Only the small/edge models run locally on Halo-A (the big tier lives on Halo-B).
EDGE_TAGS=(
  "llama3.2:3b"   # SIMPLE / default -> qwen/qwen3.5-rocm
  "qwen2.5:7b"    # MEDIUM           -> google/gemini-2.5-flash-lite
)

# The PII ModernBERT model dir is shared with the existing single-box recipe so
# we do not duplicate the (large) model download. The router loads token
# classifiers via ONNX Runtime, but the published ModernBERT presidio detector
# ships safetensors only, so model.onnx must be generated once before serve.
# The shared single-box models dir is mounted into the gateway (see the symlink
# step below): `vllm-sr serve` mounts `<config_dir>/models` -> `/app/models`, and
# the config_dir is the rendered dir, so we point `${RENDER_DIR}/models` at this
# pre-staged tree to supply the presidio PII model and avoid the broken alias
# auto-download.
SHARED_MODELS_DIR="${SCRIPT_DIR}/../strix-halo-poc/models"
PII_MODEL_DIR="${SHARED_MODELS_DIR}/pii_classifier_modernbert-base_presidio_token_model"
PII_ONNX_MODEL="${PII_MODEL_DIR}/onnx/model.onnx"
PII_MAPPING_FILE="${PII_MODEL_DIR}/pii_type_mapping.json"
# One-time export venv kept outside the repo tree so it never pollutes git.
ONNX_EXPORT_VENV="${TMPDIR:-/tmp}/vllm-sr-pii-onnx-export-venv"

echo "==> [0/6] Checking required HALO_B_IP environment variable"
if [[ -z "${HALO_B_IP:-}" ]]; then
  echo "ERROR: HALO_B_IP is not set." >&2
  echo "       The datacenter backends in poc-client-edge.yaml escalate to Halo-B" >&2
  echo "       at HALO_B_IP:11434, so this box must know the real Halo-B address." >&2
  echo "       Set it to the routable IP/host of the Halo-B box and re-run:" >&2
  echo "         export HALO_B_IP=192.0.2.20" >&2
  echo "         bash client-bring-up.sh" >&2
  exit 1
fi
echo "    HALO_B_IP=${HALO_B_IP}"

echo "==> [1/6] Ensuring Docker network '${NETWORK}' exists"
docker network create "${NETWORK}" 2>/dev/null || true

echo "==> [2/6] Starting local Ollama (ROCm) container '${OLLAMA_CONTAINER}' on ${NETWORK}"
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

echo "    waiting for local Ollama to answer on http://localhost:${OLLAMA_PORT}/api/tags ..."
ollama_ready=""
for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
    ollama_ready="yes"
    echo "    local Ollama is up."
    break
  fi
  sleep 2
done
if [[ -z "${ollama_ready}" ]]; then
  echo "ERROR: local Ollama did not become ready on http://localhost:${OLLAMA_PORT}/api/tags." >&2
  echo "       Check the container logs: docker logs ${OLLAMA_CONTAINER}" >&2
  exit 1
fi

echo "==> [3/6] Pulling edge tier models into the local '${OLLAMA_CONTAINER}' container"
for tag in "${EDGE_TAGS[@]}"; do
  echo "    pulling ${tag}"
  docker exec "${OLLAMA_CONTAINER}" ollama pull "${tag}"
done

echo "==> [4/6] Ensuring the ModernBERT PII detector has an exported ONNX model"
# Idempotent: skip when model.onnx already exists; export it once otherwise. We
# call the venv binaries directly (no `source activate`) so the step is safe
# under `set -euo pipefail`.
if [[ -f "${PII_ONNX_MODEL}" ]]; then
  echo "    onnx/model.onnx already present; skipping export"
elif [[ ! -d "${PII_MODEL_DIR}" ]]; then
  echo "ERROR: PII model dir is missing:" >&2
  echo "      ${PII_MODEL_DIR}" >&2
  echo "    The gateway loads the ModernBERT PII classifier at serve time, so this" >&2
  echo "    is a hard requirement -- the router will not start without it." >&2
  echo "    Download it first (see ../strix-halo-poc/REHEARSAL.md Gate B), then re-run." >&2
  exit 1
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

# Hard preflight: the router fatals at startup ("failed to read PII mapping
# file: ...pii_type_mapping.json: no such file or directory") if the presidio
# detector is incomplete. Verify BOTH the exported ONNX graph and the PII type
# mapping are present in the pre-staged dir before we mount it.
if [[ ! -f "${PII_MAPPING_FILE}" || ! -f "${PII_ONNX_MODEL}" ]]; then
  echo "ERROR: the presidio PII model in the shared single-box models dir is incomplete." >&2
  echo "       Required files (both must exist):" >&2
  echo "         ${PII_MAPPING_FILE}" >&2
  echo "         ${PII_ONNX_MODEL}" >&2
  [[ -f "${PII_MAPPING_FILE}" ]] || echo "       MISSING: pii_type_mapping.json" >&2
  [[ -f "${PII_ONNX_MODEL}" ]] || echo "       MISSING: onnx/model.onnx" >&2
  echo "       The gateway mounts this dir read-through and the router reads the PII" >&2
  echo "       mapping at startup, so it will not serve without both files." >&2
  echo "       Run the single-box download (Gate B) + bring-up [4/5] ONNX export first:" >&2
  echo "         see ../strix-halo-poc/REHEARSAL.md" >&2
  exit 1
fi

echo "==> [5/6] Rendering runtime config with HALO_B_IP=${HALO_B_IP}"
# The committed poc-client-edge.yaml keeps a literal HALO_B_IP placeholder so the
# remote backends are obvious. We render a runtime copy here so the committed
# file is never mutated. The rendered dir is gitignored.
mkdir -p "${RENDER_DIR}"

# `vllm-sr serve` mounts `<config_dir>/models` -> `/app/models`, and the
# config_dir here is the rendered dir. Point `${RENDER_DIR}/models` at the
# pre-staged single-box models tree so the router uses the presidio PII model
# (with pii_type_mapping.json + onnx/model.onnx) instead of auto-downloading the
# registry-alias repo that lacks the mapping file. If a previous failed run left
# a real directory there (e.g. a partial auto-download), remove it first so the
# symlink can be (re)created cleanly.
if [[ -d "${RENDER_DIR}/models" && ! -L "${RENDER_DIR}/models" ]]; then
  echo "    removing stale real models dir at ${RENDER_DIR}/models (likely a failed auto-download)"
  rm -rf "${RENDER_DIR}/models"
fi
ln -sfn "../../strix-halo-poc/models" "${RENDER_DIR}/models"
echo "    models dir symlinked: ${RENDER_DIR}/models -> ../../strix-halo-poc/models (shared pre-staged single-box dir)"

# The committed yaml also keeps a literal ANTHROPIC_MODEL_ID placeholder for the
# real id sent to the Anthropic public API (the logical name
# anthropic/claude-opus-4.6 used by routing/decisions is unaffected). Default to
# the pinned Opus id; override via the ANTHROPIC_MODEL_ID env to pin another.
ANTHROPIC_MODEL_ID="${ANTHROPIC_MODEL_ID:-claude-opus-4-20250514}"
# AMD premium tier (amd/claude-opus-4.8) auth: the Azure-APIM
# Ocp-Apim-Subscription-Key, rendered from the AMD_OCP_APIM_KEY env var so the
# secret is NEVER committed (the committed yaml holds only the
# __AMD_OCP_APIM_KEY__ placeholder; this rendered copy lives under the gitignored
# .vllm-sr-rendered/). Non-fatal when unset, like ANTHROPIC_API_KEY below: every
# other tier still works and only amd/claude-opus-4.8 requests fail. `|` is the
# sed delimiter so url/base64-style key characters (e.g. `/`) do not break it.
AMD_OCP_APIM_KEY="${AMD_OCP_APIM_KEY:-}"
if [ -z "${AMD_OCP_APIM_KEY}" ]; then
  echo "WARNING: AMD_OCP_APIM_KEY is not set."
  echo "    The amd/claude-opus-4.8 tier (AMD Anthropic gateway) will FAIL without it."
  echo "    Other tiers still work. Enable it with: export AMD_OCP_APIM_KEY=<subscription-key>"
fi
sed -e "s/HALO_B_IP/${HALO_B_IP}/g" \
  -e "s/ANTHROPIC_MODEL_ID/${ANTHROPIC_MODEL_ID}/g" \
  -e "s|__AMD_OCP_APIM_KEY__|${AMD_OCP_APIM_KEY}|g" \
  "${CONFIG_TEMPLATE}" > "${RENDERED_CONFIG}"
for placeholder in HALO_B_IP ANTHROPIC_MODEL_ID; do
  if grep -q "${placeholder}" "${RENDERED_CONFIG}"; then
    echo "ERROR: ${placeholder} placeholder still present after rendering ${RENDERED_CONFIG}" >&2
    exit 1
  fi
done
echo "    rendered: ${RENDERED_CONFIG} (ANTHROPIC_MODEL_ID=${ANTHROPIC_MODEL_ID})"

# The FRONTIER/PREMIUM tier (anthropic/claude-opus-4.6) now calls the real
# external Anthropic public API instead of a local mock. ANTHROPIC_API_KEY is an
# auto-passthrough env in `vllm-sr serve`, so we only need it exported on the
# host here. This check is non-fatal: the local tiers (small/datacenter) still
# work without it; only premium/frontier requests would fail.
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "WARNING: ANTHROPIC_API_KEY is not set."
  echo "    The frontier/premium tier (anthropic/claude-opus-4.6) will FAIL"
  echo "    without it -- it now calls the real Anthropic public API."
  echo "    Halo-A also needs outbound HTTPS egress to api.anthropic.com:443."
  echo "    Local tiers (small -> Halo-A Ollama, datacenter -> Halo-B Ollama)"
  echo "    still work. To enable premium: export ANTHROPIC_API_KEY=sk-ant-..."
fi

# Provision a demo dashboard admin so the observability UI (status / monitoring
# / tracing) is viewable without a manual first-run bootstrap. These are
# auto-passthrough envs in `vllm-sr serve`, and the dashboard's
# EnsureBootstrapAdmin is idempotent (it skips creation if the account already
# exists), so this is safe to run against an already-bootstrapped DB -- no
# volume wipe needed. Override any of them via the environment for non-demo use.
export DASHBOARD_ADMIN_EMAIL="${DASHBOARD_ADMIN_EMAIL:-admin@demo.local}"
export DASHBOARD_ADMIN_PASSWORD="${DASHBOARD_ADMIN_PASSWORD:-vllmsr-demo}"
export DASHBOARD_ADMIN_NAME="${DASHBOARD_ADMIN_NAME:-Admin}"
echo "    dashboard admin provisioned: ${DASHBOARD_ADMIN_EMAIL} (password hidden; override via DASHBOARD_ADMIN_PASSWORD)"

echo "==> [6/6] Serving the gateway with the rendered config (platform amd)"
# Keep the mmBERT/embedding classifiers on CPU so the iGPU is reserved for the
# local LLM backends.
export VLLM_SR_AMD_PRESERVE_CPU=1

vllm-sr serve \
  --config "${RENDERED_CONFIG}" \
  --image-pull-policy never \
  --platform amd

echo "==> Client bring-up complete. Verify with: vllm-sr status"
echo "    Then run the cross-box smoke test: python smoke_test.py"
