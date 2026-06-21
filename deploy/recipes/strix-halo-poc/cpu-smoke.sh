#!/usr/bin/env bash
# Local CPU end-to-end smoke for the Strix Halo PoC router -- no GPU required.
#
# This brings up a single llm-katan echo backend (no model download), then runs
# the full router stack (vllm-sr serve, default CPU image) against cpu-smoke.yaml
# and fires smoke_test.py at the listener. It proves the pipeline runs end to
# end: classify -> decide -> route -> security. It is for dev validation ONLY;
# the real Strix Halo run still uses poc-strix.yaml + Ollama (see bring-up.sh).
#
# Usage (from repo root):
#   bash deploy/recipes/strix-halo-poc/cpu-smoke.sh
#
# Requires: Docker (Linux-container mode), Python 3, pip. Do NOT pass
# --platform amd here; this is the CPU path.
set -euo pipefail

# Resolve paths relative to this script so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
CONFIG="${SCRIPT_DIR}/cpu-smoke.yaml"

NETWORK="vllm-sr-network"
KATAN_NAME="llm-katan"
KATAN_IMAGE="ghcr.io/vllm-project/semantic-router/llm-katan:latest"
KATAN_PORT="8000"
ROUTER_URL="http://localhost:8899"

echo "==> [1/6] Validating ${CONFIG}"
python "${SCRIPT_DIR}/validate_poc_config.py" "${CONFIG}"

echo "==> [2/6] Ensuring docker network '${NETWORK}' exists"
# Ignore the error if the network already exists.
docker network create "${NETWORK}" 2>/dev/null || echo "    network '${NETWORK}' already present"

echo "==> [3/6] Starting llm-katan echo backend ('${KATAN_NAME}') on ${NETWORK}:${KATAN_PORT}"
# Remove any stale container with the same name, then start a fresh one. The
# echo backend serves an OpenAI-compatible API with no model download.
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
curl -fsS "http://localhost:${KATAN_PORT}/v1/models" || echo "    (warning: /v1/models did not respond)"

echo "==> [4/6] Ensuring vllm-sr CLI is installed"
if ! command -v vllm-sr >/dev/null 2>&1; then
  echo "    vllm-sr not found; installing from src/vllm-sr"
  pip install -e "${REPO_ROOT}/src/vllm-sr"
fi
vllm-sr --version

echo "==> [5/6] Starting router: vllm-sr serve --config cpu-smoke.yaml --minimal (CPU, default image)"
# No --platform amd: this uses the default CPU router/envoy images and lets
# Docker pull them. Classifier load can take a few minutes the first time.
vllm-sr serve --config "${CONFIG}" --minimal

echo "    waiting for the router listener at ${ROUTER_URL} ..."
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "${ROUTER_URL}" 2>/dev/null; then
    echo "    router listener is responding."
    break
  fi
  sleep 5
done

echo "==> [6/6] Running smoke_test.py against ${ROUTER_URL}"
python "${SCRIPT_DIR}/smoke_test.py" --base-url "${ROUTER_URL}"

echo
echo "Smoke run complete."
echo "Teardown when finished:"
echo "    vllm-sr stop"
echo "    docker rm -f ${KATAN_NAME}"
echo "  (the '${NETWORK}' network can be left in place for the next run)"
