#!/usr/bin/env bash
#
# Provision THIS box (Halo-B) so it can run a REAL vllm-sr fleet gateway.
#
# The orchestrator (deploy-fleet-2box.sh) ships this script to Halo-B and runs
# it over SSH BEFORE node-bring-up.sh whenever HALO_B_MODE=gateway (unless
# HALO_B_PROVISION=skip). Running ON Halo-B keeps every path/pip/tilde native --
# no fragile multi-shell SSH quoting -- and makes "one-click make Halo-B ready"
# actually hands-off.
#
# It is idempotent and stays entirely in user space (pip --user, no sudo). It
# ensures, under STRIX_POC_DIR (= <HALO_B_REPO>/deploy/recipes/strix-halo-poc):
#   1. poc-strix.yaml           -- committed; if missing the checkout lacks the
#                                  strix-halo-poc recipe, which we CANNOT safely
#                                  fix here, so we fail fast with the exact fix.
#   2. the ModernBERT PII model -- a PUBLIC HF repo (no token). gateway-bring-up
#                                  exports it to ONNX, so the source must be
#                                  present; download it if absent.
#   3. the vllm-sr CLI          -- pip install --user -e <HALO_B_REPO>/src/vllm-sr
#                                  (console script lands in ~/.local/bin, which
#                                  gateway-bring-up.sh auto-detects).
#
# What it deliberately does NOT do: clone the repo, switch git branches, or pull
# the large Ollama tier models (gateway-bring-up.sh pulls those). Those stay a
# one-time manual prep so we never mutate the operator's git tree or guess creds.
#
set -euo pipefail

STRIX_POC_DIR="${STRIX_POC_DIR:?STRIX_POC_DIR must be set (…/deploy/recipes/strix-halo-poc on Halo-B)}"
HALO_B_REPO="${HALO_B_REPO:?HALO_B_REPO must be set (the semantic-router repo path on Halo-B)}"

PII_REPO_ID="LLM-Semantic-Router/pii_classifier_modernbert-base_presidio_token_model"
PII_MODEL_DIR="${STRIX_POC_DIR}/models/pii_classifier_modernbert-base_presidio_token_model"
SOURCE_CONFIG="${STRIX_POC_DIR}/poc-strix.yaml"
GATEWAY_BRANCH="poc/strix-halo-single-box"

echo "==> [provision-halo-b] STRIX_POC_DIR=${STRIX_POC_DIR}"

# Resolve a working pip for this Python. --------------------------------------
_pip() {
  if command -v pip3 >/dev/null 2>&1; then pip3 "$@"
  elif command -v pip >/dev/null 2>&1; then pip "$@"
  else python3 -m pip "$@"; fi
}

# pip install --user, retrying once with --break-system-packages for PEP 668
# (externally-managed) system Pythons. --user still scopes everything to
# ~/.local, so the retry never touches system site-packages.
_pip_user_install() {
  if _pip install --user "$@"; then
    return 0
  fi
  echo "    (pip --user failed; retrying with --break-system-packages for an externally-managed Python)"
  _pip install --user --break-system-packages "$@"
}

# 1. poc-strix.yaml (committed) -----------------------------------------------
if [ ! -f "${SOURCE_CONFIG}" ]; then
  echo "ERROR: ${SOURCE_CONFIG} is missing." >&2
  echo "       Halo-B's checkout does not contain the strix-halo-poc recipe. This is a" >&2
  echo "       committed file, so it cannot be auto-provisioned -- fix the checkout ONCE:" >&2
  echo "         cd ${HALO_B_REPO} && git fetch origin && git checkout ${GATEWAY_BRANCH} && git pull --ff-only" >&2
  echo "       (or run Halo-B as a mock edge instead: HALO_B_MODE=mock)." >&2
  exit 1
fi
echo "    poc-strix.yaml present"

# 2. ModernBERT PII source model (public HF; needed for the ONNX export) -------
if [ -d "${PII_MODEL_DIR}" ] && [ -n "$(ls -A "${PII_MODEL_DIR}" 2>/dev/null)" ]; then
  echo "    PII model dir present"
else
  echo "    PII model dir missing -> downloading public HF repo ${PII_REPO_ID} ..."
  if ! python3 -c 'import huggingface_hub' >/dev/null 2>&1; then
    echo "    installing huggingface_hub (pip --user) ..."
    if ! _pip_user_install --quiet huggingface_hub; then
      echo "ERROR: could not install huggingface_hub on Halo-B (need python3 + pip)." >&2
      echo "       Install it (e.g. 'sudo apt install -y python3-pip'), or pre-stage the model:" >&2
      echo "         hf download ${PII_REPO_ID} --local-dir ${PII_MODEL_DIR}" >&2
      exit 1
    fi
  fi
  mkdir -p "${PII_MODEL_DIR}"
  # snapshot_download reads HF_TOKEN from the env automatically (only needed for
  # a private repo; this one is public, so anonymous download works).
  if ! python3 - "${PII_REPO_ID}" "${PII_MODEL_DIR}" <<'PY'
import sys
from huggingface_hub import snapshot_download

repo_id, local_dir = sys.argv[1], sys.argv[2]
path = snapshot_download(repo_id=repo_id, local_dir=local_dir)
print("    downloaded", repo_id, "->", path)
PY
  then
    echo "ERROR: failed to download the PII model from ${PII_REPO_ID}." >&2
    echo "       Check network egress to huggingface.co from Halo-B, then re-run." >&2
    exit 1
  fi
  if [ -z "$(ls -A "${PII_MODEL_DIR}" 2>/dev/null)" ]; then
    echo "ERROR: PII model download produced no files in ${PII_MODEL_DIR}." >&2
    exit 1
  fi
  echo "    PII model downloaded"
fi

# 3. vllm-sr CLI --------------------------------------------------------------
if command -v vllm-sr >/dev/null 2>&1 || [ -x "${HOME}/.local/bin/vllm-sr" ]; then
  echo "    vllm-sr present"
else
  echo "    vllm-sr not found -> installing (pip install --user -e ${HALO_B_REPO}/src/vllm-sr) ..."
  if ! _pip_user_install -e "${HALO_B_REPO}/src/vllm-sr"; then
    echo "ERROR: failed to install vllm-sr on Halo-B." >&2
    echo "       Ensure python3 + pip exist ('sudo apt install -y python3-pip'), install" >&2
    echo "       vllm-sr manually, or run Halo-B as a mock edge: HALO_B_MODE=mock." >&2
    exit 1
  fi
  if ! command -v vllm-sr >/dev/null 2>&1 && [ ! -x "${HOME}/.local/bin/vllm-sr" ]; then
    echo "ERROR: vllm-sr still not found after install." >&2
    exit 1
  fi
  echo "    vllm-sr installed (~/.local/bin)"
fi

echo "==> [provision-halo-b] Halo-B is gateway-ready"
