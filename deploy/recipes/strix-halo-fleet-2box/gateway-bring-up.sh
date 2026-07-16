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

# STRIX_POC_DIR defaults to the sibling recipe, but can be OVERRIDDEN so the
# orchestrator can ship THIS recipe's scripts to a bare Halo-B (a temp dir) while
# still pointing at the repo's proven strix-halo-poc assets (poc-strix.yaml +
# staged models). That way Halo-B does not have to be checked out on the branch
# that carries strix-halo-fleet-2box. See deploy-fleet-2box.sh (gateway).
STRIX_POC_DIR="${STRIX_POC_DIR:-${SCRIPT_DIR}/../strix-halo-poc}"
SOURCE_CONFIG="${STRIX_POC_DIR}/poc-strix.yaml"
GATEWAY_DIR="${FLEET_STATE_DIR}/gateway"
GATEWAY_CONFIG="${GATEWAY_CONFIG:-${GATEWAY_DIR}/config.yaml}"

NETWORK="vllm-sr-network"
OLLAMA_CONTAINER="ollama"
OLLAMA_PORT="11434"
OLLAMA_VOLUME="ollama"
OLLAMA_IMAGE="ollama/ollama:rocm"
TIER_TAGS=("llama3.2:3b" "qwen2.5:7b" "qwen2.5:14b" "qwen3:14b" "qwen2.5:32b")
DEFAULT_ROUTER_IMAGE="ghcr.io/vllm-project/semantic-router/vllm-sr-rocm:latest"
# R3: honor an OPTIONAL, gitignored versions.env (copy versions.env.example) in
# THIS dir BEFORE resolving the router image below, so a pinned
# VLLM_SR_ROUTER_IMAGE=...@sha256:... is respected even when gateway-bring-up is
# invoked DIRECTLY (run-all-2box.sh / deploy-fleet-2box.sh already source it and
# forward it for the orchestrated path; a direct/standalone run would otherwise
# miss the pin). Absent => unchanged behavior. `set -a` exports the assignments
# so the `vllm-sr serve` child inherits them.
VERSIONS_ENV="${VERSIONS_ENV:-${SCRIPT_DIR}/versions.env}"
if [[ -f "${VERSIONS_ENV}" ]]; then
  echo "==> [gateway] sourcing image pin ${VERSIONS_ENV}"
  set -a
  # shellcheck source=/dev/null
  source "${VERSIONS_ENV}"
  set +a
fi
ROUTER_IMAGE="${VLLM_SR_ROUTER_IMAGE:-${DEFAULT_ROUTER_IMAGE}}"
IMAGE_PULL_POLICY="${VLLM_SR_IMAGE_PULL_POLICY:-ifnotpresent}"

PII_MODEL_DIR="${STRIX_POC_DIR}/models/pii_classifier_modernbert-base_presidio_token_model"
PII_ONNX_MODEL="${PII_MODEL_DIR}/onnx/model.onnx"
ONNX_EXPORT_VENV="${TMPDIR:-/tmp}/vllm-sr-pii-onnx-export-venv"

preflight_router_image() {
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

  # Resolve to the newest PULLABLE image with no flag required. 'ifnotpresent'
  # (the default) uses an already-local copy of the requested ref AS-IS and only
  # pulls when it is ABSENT; 'always' forces a re-pull; 'never' stays local-only.
  # When we do pull and a pinned ref cannot be pulled (e.g. a removed digest ->
  # registry "not found"), we auto-fall-back to :latest, so a stale pin never
  # blocks the run. A still-pullable pin is respected. Whatever we resolve, we
  # then serve EXACTLY that image and tell the CLI not to re-pull it (a re-pull of
  # a stale digest would fail again).
  if [[ "${IMAGE_PULL_POLICY}" == "never" ]]; then
    if docker image inspect "${ROUTER_IMAGE}" >/dev/null 2>&1; then
      echo "    router image present locally: ${ROUTER_IMAGE}"
    elif [[ "${ROUTER_IMAGE}" != "${DEFAULT_ROUTER_IMAGE}" ]] && docker image inspect "${DEFAULT_ROUTER_IMAGE}" >/dev/null 2>&1; then
      echo "    WARNING: '${ROUTER_IMAGE}' absent locally; using local '${DEFAULT_ROUTER_IMAGE}'." >&2
      ROUTER_IMAGE="${DEFAULT_ROUTER_IMAGE}"
    else
      echo "ERROR: router image not present locally and VLLM_SR_IMAGE_PULL_POLICY=never:" >&2
      echo "       ${ROUTER_IMAGE}" >&2
      echo "       Pre-pull it, or rerun with VLLM_SR_IMAGE_PULL_POLICY=always." >&2
      exit 1
    fi
  elif [[ "${IMAGE_PULL_POLICY}" == "ifnotpresent" ]] && docker image inspect "${ROUTER_IMAGE}" >/dev/null 2>&1; then
    # TRUE ifnotpresent: the image is already local, so use it and do NOT re-pull
    # the ~20GB image (the old code always re-pulled here with no progress output,
    # which made the preflight look frozen for minutes).
    echo "    router image already present locally; skipping pull: ${ROUTER_IMAGE}"
  else
    # Pull path: policy=always (force a re-pull) OR ifnotpresent with the image
    # ABSENT. Announce it and let `docker pull`'s progress reach the terminal (on
    # stderr) instead of >/dev/null, so a multi-minute ~20GB pull is visibly
    # progressing rather than looking hung. The if/elif exit-status checks below
    # (incl. the stale-pin auto-fallback to :latest) are otherwise unchanged.
    echo "    pulling router image ${ROUTER_IMAGE} (~20GB ROCm image; the first pull can take several minutes, cached layers are skipped; live progress below)" >&2
    if docker pull "${ROUTER_IMAGE}" >&2; then
      echo "    router image pulled (latest available): ${ROUTER_IMAGE}"
    elif [[ "${ROUTER_IMAGE}" != "${DEFAULT_ROUTER_IMAGE}" ]] && docker pull "${DEFAULT_ROUTER_IMAGE}" >&2; then
      echo "    WARNING: '${ROUTER_IMAGE}' is not pullable (stale pin?); using latest instead:" >&2
      echo "             ${DEFAULT_ROUTER_IMAGE}" >&2
      ROUTER_IMAGE="${DEFAULT_ROUTER_IMAGE}"
    elif docker image inspect "${ROUTER_IMAGE}" >/dev/null 2>&1; then
      echo "    WARNING: could not pull '${ROUTER_IMAGE}'; using the local copy." >&2
    elif docker image inspect "${DEFAULT_ROUTER_IMAGE}" >/dev/null 2>&1; then
      echo "    WARNING: could not pull; using local '${DEFAULT_ROUTER_IMAGE}'." >&2
      ROUTER_IMAGE="${DEFAULT_ROUTER_IMAGE}"
    else
      echo "ERROR: no pullable or local router image found." >&2
      echo "       Tried: ${ROUTER_IMAGE} and ${DEFAULT_ROUTER_IMAGE}" >&2
      exit 1
    fi
  fi

  # Serve exactly the image we resolved; it is now local, so tell the CLI to reuse
  # it (never re-hit a stale-digest pull).
  export VLLM_SR_ROUTER_IMAGE="${ROUTER_IMAGE}"
  IMAGE_PULL_POLICY="ifnotpresent"
}

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

# This script may run over a NON-interactive SSH shell (Halo-B), which does not
# load the conda/venv that provides `vllm-sr` (conda init lives in ~/.bashrc,
# guarded out for non-interactive shells). Resolution order: an explicit
# VLLM_SR_BIN override (deterministic for ANY layout), else probe common conda/
# venv bin dirs INCLUDING named-env bin dirs (vllm-sr is usually in a named env
# like ~/miniconda3/envs/<env>/bin, not base); otherwise fail fast (before the
# slow model pulls) with clear guidance.
if ! command -v vllm-sr >/dev/null 2>&1; then
  if [ -n "${VLLM_SR_BIN:-}" ] && [ -x "${VLLM_SR_BIN%/}/vllm-sr" ]; then
    PATH="${VLLM_SR_BIN%/}:${PATH}"; export PATH
  else
    for _vsr_bin in "${HOME}/miniconda3/bin" "${HOME}/anaconda3/bin" \
                    "${HOME}/miniforge3/bin" "${HOME}/mambaforge/bin" \
                    "${HOME}/.local/bin" "/opt/conda/bin" \
                    "${HOME}"/miniconda3/envs/*/bin "${HOME}"/anaconda3/envs/*/bin \
                    "${HOME}"/miniforge3/envs/*/bin "${HOME}"/mambaforge/envs/*/bin \
                    /opt/conda/envs/*/bin; do
      if [[ -x "${_vsr_bin}/vllm-sr" ]]; then
        PATH="${_vsr_bin}:${PATH}"; export PATH; break
      fi
    done
  fi
fi
# Final fallback: ask the box's own LOGIN+INTERACTIVE shell where vllm-sr is.
# `bash -lic` sources ~/.bash_profile + ~/.bashrc (where conda init lives)
# exactly like an interactive SSH session, so it resolves vllm-sr wherever the
# user actually has it (base OR the default-activated env, any conda location)
# without us guessing paths. This is the general fix for "non-interactive SSH
# does not load conda". Cheap and side-effect-free (it only reads the path).
if ! command -v vllm-sr >/dev/null 2>&1; then
  _login_vsr="$(bash -lic 'command -v vllm-sr' 2>/dev/null | tr -d '[:space:]' || true)"
  if [ -n "${_login_vsr}" ] && [ -x "${_login_vsr}" ]; then
    PATH="$(dirname "${_login_vsr}"):${PATH}"; export PATH
  fi
fi
if ! command -v vllm-sr >/dev/null 2>&1; then
  echo "ERROR: 'vllm-sr' is not installed / not found on this box." >&2
  echo "       (Probed PATH, VLLM_SR_BIN, conda/venv dirs incl. envs/*/bin, and your login shell.)" >&2
  echo "       Install it HERE (the console script lands in ~/.local/bin, which this script auto-detects):" >&2
  echo "         pip install --user -e <semantic-router-repo>/src/vllm-sr" >&2
  echo "       Already installed elsewhere? Point VLLM_SR_BIN at its bin dir and pass it to" >&2
  echo "       deploy-fleet-2box.sh (forwarded to Halo-B), e.g. VLLM_SR_BIN=\$HOME/.local/bin." >&2
  echo "       Or, if this box should NOT run a real gateway, deploy it as a mock edge instead:" >&2
  echo "       run deploy-fleet-2box.sh with HALO_B_MODE=mock (Halo-A stays a real gateway)." >&2
  exit 1
fi

echo "==> [gateway] preflighting the vllm-sr ROCm router image"
preflight_router_image

echo "==> [gateway] ensuring Docker network '${NETWORK}'"
docker network create "${NETWORK}" 2>/dev/null || true

echo "==> [gateway] starting Ollama (ROCm) '${OLLAMA_CONTAINER}'"
if docker ps -a --format '{{.Names}}' | grep -qx "${OLLAMA_CONTAINER}"; then
  docker start "${OLLAMA_CONTAINER}" >/dev/null
else
  # OLLAMA_ORIGINS='*' lets ollama accept forwarded browser Origin headers; without it a
  # browser Origin (browser -> dashboard proxy -> Envoy -> ollama) trips ollama's CORS
  # allow-list and returns an empty-body 403 on Playground cache-misses. Fine for this
  # internal PoC; tighten to a specific origin (e.g. the dashboard URL) if desired.
  docker run -d --name "${OLLAMA_CONTAINER}" --network="${NETWORK}" --restart unless-stopped \
    -p "${OLLAMA_PORT}:${OLLAMA_PORT}" -v "${OLLAMA_VOLUME}:/root/.ollama" \
    --device=/dev/kfd --device=/dev/dri --group-add=video \
    --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
    -e HSA_OVERRIDE_GFX_VERSION=11.5.1 -e OLLAMA_ORIGINS='*' "${OLLAMA_IMAGE}" >/dev/null
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

echo "==> [gateway] freeing the host API port :${ROUTER_PORT} before serve"
# A stale vllm-sr router container (or a leftover fleet mock router) would hold
# the host API port and make 'vllm-sr serve' fail with 'address already in use'.
# Remove the router container (serve recreates it); the per-box mock router is
# already stopped by node-bring-up.sh before this script runs.
docker rm -f vllm-sr-router-container >/dev/null 2>&1 || true
if command -v ss >/dev/null 2>&1 && [ -n "$(ss -ltnH "( sport = :${ROUTER_PORT} )" 2>/dev/null)" ]; then
  echo "WARNING: something still listens on :${ROUTER_PORT}; serve may fail to bind it." >&2
  echo "         Free it first: 'vllm-sr stop' and/or stop the fleet mock router, then re-run." >&2
fi

echo "==> [gateway] serving vllm-sr (platform amd, classifiers pinned to CPU)"
# Provision the demo UI credentials consistently across vLLM-SR Dashboard and
# Grafana. Override these envs before running for non-demo use.
export DASHBOARD_ADMIN_EMAIL="${DASHBOARD_ADMIN_EMAIL:-yingylin@amd.com}"
export DASHBOARD_ADMIN_PASSWORD="${DASHBOARD_ADMIN_PASSWORD:-aupaup123}"
export DASHBOARD_ADMIN_NAME="${DASHBOARD_ADMIN_NAME:-yingylin}"
export GF_SECURITY_ADMIN_USER="${GF_SECURITY_ADMIN_USER:-${DASHBOARD_ADMIN_EMAIL}}"
export GF_SECURITY_ADMIN_PASSWORD="${GF_SECURITY_ADMIN_PASSWORD:-${DASHBOARD_ADMIN_PASSWORD}}"
echo "    dashboard/grafana admin: ${DASHBOARD_ADMIN_EMAIL} (password hidden; override via DASHBOARD_ADMIN_PASSWORD)"
# VLLM_SR_AMD_PRESERVE_CPU=1 is REQUIRED: it reaches the container so that the
# agent-triggered hot-reload keeps classifiers on CPU instead of re-creating
# ROCm ONNX sessions (which would crash the router).
# VLLM_SR_IMAGE_PULL_POLICY (default 'ifnotpresent' = pull only images missing
# locally, so a freshly provisioned box still fetches what it needs; 'always'
# forces a re-pull, 'never' is local-only).
export VLLM_SR_AMD_PRESERVE_CPU=1
# Pin THIS box's dashboard to the local origin-fix build, which strips the browser
# Origin before Envoy/ollama and so avoids the Playground CORS 403. Guarded on the
# tag being present locally so other boxes (without it) fall back to the default
# image and are unaffected. With --image-pull-policy ifnotpresent and the tag
# already local, serve reuses it without a pull.
if docker image inspect ghcr.io/vllm-project/semantic-router/dashboard:origin-fix >/dev/null 2>&1; then
  export VLLM_SR_DASHBOARD_IMAGE=ghcr.io/vllm-project/semantic-router/dashboard:origin-fix
fi
vllm-sr serve --config "${GATEWAY_CONFIG}" \
  --image-pull-policy "${IMAGE_PULL_POLICY}" --platform amd

echo "==> [gateway] waiting for the router config API on :${ROUTER_PORT}"
if fleet_wait_http "http://localhost:${ROUTER_PORT}/config/hash" 60; then
  echo "    gateway router config API is up (GET /config/hash)."
else
  echo "ERROR: router config API never answered on :${ROUTER_PORT}/config/hash." >&2
  echo "       Check: vllm-sr status ; docker logs for the router container." >&2
  exit 1
fi

# Optional: bake VRAM-resident "<tag>-vram" variants (num_gpu 999 + use_mmap false)
# for the big models ALREADY present on this box, so they default to 100% GPU on a
# large VRAM carveout (e.g. Halo-B at 96 GiB, where Ollama's auto estimate otherwise
# CPU-offloads them). Opt-in (default OFF), non-fatal, and it does NOT pull models --
# make-vram-resident-models.sh skips any tag that is not present.
if [[ "${MAKE_VRAM_VARIANTS:-0}" == "1" ]]; then
  echo "==> [gateway] MAKE_VRAM_VARIANTS=1: creating -vram variants for present big models"
  VERIFY=0 bash "${SCRIPT_DIR}/perf/make-vram-resident-models.sh" \
    || echo "    WARNING: make-vram-resident-models.sh returned non-zero (continuing)" >&2
fi
