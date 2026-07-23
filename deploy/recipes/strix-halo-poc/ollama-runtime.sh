#!/usr/bin/env bash
#
# Safe, context-pinned Ollama runtime management for the Strix Halo PoC.
#
# Commands:
#   preflight    read-only host/container inspection (default)
#   provision    start only when absent/stopped, pull missing models, capture facts
#   prove        one-token load probe, then require ollama ps context evidence
#   print-config print resolved non-secret defaults as JSON
#
# Existing running containers are never restarted, removed, or recreated. A
# configuration mismatch fails closed with remediation guidance.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROOF_SCRIPT="${SCRIPT_DIR}/runtime_context_proof.py"

PINNED_OLLAMA_IMAGE="ollama/ollama:rocm@sha256:4a22dbbce24e7425861020987adb99851282b5af8e433028d1c72c453eed8f75"
OLLAMA_IMAGE="${VLLM_SR_OLLAMA_IMAGE:-${PINNED_OLLAMA_IMAGE}}"
OLLAMA_IMAGE_PULL_POLICY="${VLLM_SR_OLLAMA_IMAGE_PULL_POLICY:-ifnotpresent}"
OLLAMA_CONTEXT_LENGTH="${VLLM_SR_OLLAMA_CONTEXT_LENGTH:-65536}"
OLLAMA_NUM_PARALLEL="${VLLM_SR_OLLAMA_NUM_PARALLEL:-1}"
OLLAMA_MAX_LOADED_MODELS="${VLLM_SR_OLLAMA_MAX_LOADED_MODELS:-1}"
OLLAMA_KEEP_ALIVE="${VLLM_SR_OLLAMA_KEEP_ALIVE:-10m}"
OLLAMA_HSA_OVERRIDE="${VLLM_SR_OLLAMA_HSA_OVERRIDE_GFX_VERSION:-11.5.1}"
ALLOW_EXPERIMENTAL_CONTEXT="${VLLM_SR_ALLOW_EXPERIMENTAL_CONTEXT:-0}"

NETWORK="${VLLM_SR_OLLAMA_NETWORK:-vllm-sr-network}"
OLLAMA_CONTAINER="${VLLM_SR_OLLAMA_CONTAINER:-ollama}"
OLLAMA_PORT="${VLLM_SR_OLLAMA_PORT:-11434}"
OLLAMA_VOLUME="${VLLM_SR_OLLAMA_VOLUME:-ollama}"
OLLAMA_BASE_URL="${VLLM_SR_OLLAMA_BASE_URL:-http://127.0.0.1:${OLLAMA_PORT}}"

PRIMARY_MODEL="gemma4:26b-a4b-it-q8_0"
# Exactly the Ollama-backed models reachable from current auto-routed decisions.
# Explicit-only QAT/31B/120B profiles remain opt-in to avoid surprise downloads.
DEFAULT_OLLAMA_MODELS="${PRIMARY_MODEL} gemma4:26b qwen2.5:7b qwen2.5:14b qwen3:14b"
OLLAMA_MODELS="${VLLM_SR_OLLAMA_MODELS:-${DEFAULT_OLLAMA_MODELS}}"
read -r -a MODEL_TAGS <<<"${OLLAMA_MODELS}"

if REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null)"; then
  DEFAULT_PROVENANCE_DIR="${REPO_ROOT}/.agent-harness/experiments/runtime-context-proof"
else
  DEFAULT_PROVENANCE_DIR="${SCRIPT_DIR}/runtime-context-proof"
fi
PROVENANCE_DIR="${VLLM_SR_PROVENANCE_DIR:-${DEFAULT_PROVENANCE_DIR}}"

ACTION="${1:-preflight}"
if [[ $# -gt 1 ]]; then
  echo "ERROR: expected one command: preflight, provision, prove, or print-config" >&2
  exit 2
fi

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' python3
  elif command -v python >/dev/null 2>&1; then
    printf '%s\n' python
  else
    echo "ERROR: Python 3 is required for provenance capture." >&2
    return 1
  fi
}

PY_BIN="$(find_python)"

validate_settings() {
  case "${ACTION}" in
    preflight|provision|prove|print-config) ;;
    *)
      echo "ERROR: unknown command '${ACTION}'." >&2
      echo "       Expected: preflight, provision, prove, or print-config" >&2
      exit 2
      ;;
  esac

  case "${OLLAMA_IMAGE_PULL_POLICY}" in
    never|ifnotpresent|always) ;;
    *)
      echo "ERROR: VLLM_SR_OLLAMA_IMAGE_PULL_POLICY must be never, ifnotpresent, or always." >&2
      exit 2
      ;;
  esac

  if [[ "${OLLAMA_IMAGE}" != *@sha256:* ]]; then
    echo "ERROR: VLLM_SR_OLLAMA_IMAGE must be digest-pinned with @sha256:." >&2
    echo "       Resolved value: ${OLLAMA_IMAGE}" >&2
    exit 2
  fi
  if [[ ! "${OLLAMA_CONTEXT_LENGTH}" =~ ^[0-9]+$ ]]; then
    echo "ERROR: VLLM_SR_OLLAMA_CONTEXT_LENGTH must be a positive integer." >&2
    exit 2
  fi
  if (( OLLAMA_CONTEXT_LENGTH < 65536 )); then
    echo "ERROR: the agent serving policy requires at least 65536 context tokens." >&2
    exit 2
  fi
  if (( OLLAMA_CONTEXT_LENGTH > 131072 )); then
    echo "ERROR: context ${OLLAMA_CONTEXT_LENGTH} exceeds this phase's 131072 experimental ceiling." >&2
    exit 2
  fi
  if (( OLLAMA_CONTEXT_LENGTH > 65536 )) && [[ "${ALLOW_EXPERIMENTAL_CONTEXT}" != "1" ]]; then
    echo "ERROR: contexts above 65536 remain experimental in this phase." >&2
    echo "       Set VLLM_SR_ALLOW_EXPERIMENTAL_CONTEXT=1 only for an explicit 128K trial." >&2
    exit 2
  fi
  if [[ ! "${OLLAMA_NUM_PARALLEL}" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: VLLM_SR_OLLAMA_NUM_PARALLEL must be a positive integer." >&2
    exit 2
  fi
  if [[ ! "${OLLAMA_MAX_LOADED_MODELS}" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: VLLM_SR_OLLAMA_MAX_LOADED_MODELS must be a positive integer." >&2
    exit 2
  fi
  if [[ ! "${OLLAMA_PORT}" =~ ^[1-9][0-9]*$ ]] || (( OLLAMA_PORT > 65535 )); then
    echo "ERROR: VLLM_SR_OLLAMA_PORT must be a valid TCP port." >&2
    exit 2
  fi
  if [[ ${#MODEL_TAGS[@]} -eq 0 ]]; then
    echo "ERROR: VLLM_SR_OLLAMA_MODELS resolved to an empty model list." >&2
    exit 2
  fi

  local primary_present=0 tag
  for tag in "${MODEL_TAGS[@]}"; do
    [[ "${tag}" == "${PRIMARY_MODEL}" ]] && primary_present=1
  done
  if [[ "${primary_present}" != "1" ]]; then
    echo "ERROR: VLLM_SR_OLLAMA_MODELS must include primary model ${PRIMARY_MODEL}." >&2
    exit 2
  fi
}

print_config() {
  "${PY_BIN}" - \
    "${OLLAMA_IMAGE}" \
    "${OLLAMA_CONTEXT_LENGTH}" \
    "${OLLAMA_NUM_PARALLEL}" \
    "${OLLAMA_MAX_LOADED_MODELS}" \
    "${PRIMARY_MODEL}" \
    "${OLLAMA_MODELS}" <<'PY'
import json
import sys

print(json.dumps({
    "image": sys.argv[1],
    "context_length": int(sys.argv[2]),
    "num_parallel": int(sys.argv[3]),
    "max_loaded_models": int(sys.argv[4]),
    "primary_model": sys.argv[5],
    "models": sys.argv[6].split(),
}, sort_keys=True))
PY
}

container_exists() {
  docker container inspect "${OLLAMA_CONTAINER}" >/dev/null 2>&1
}

container_running() {
  [[ "$(docker inspect --format '{{.State.Running}}' "${OLLAMA_CONTAINER}")" == "true" ]]
}

container_env_value() {
  local key="$1"
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${OLLAMA_CONTAINER}" \
    | awk -F= -v wanted="${key}" '
        $1 == wanted {
          print substr($0, length($1) + 2)
          exit
        }
      '
}

container_has_network() {
  docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
    "${OLLAMA_CONTAINER}" | awk -v wanted="${NETWORK}" '$0 == wanted { found=1 } END { exit !found }'
}

container_has_port() {
  docker port "${OLLAMA_CONTAINER}" "11434/tcp" 2>/dev/null \
    | awk -F: -v wanted="${OLLAMA_PORT}" '$NF == wanted { found=1 } END { exit !found }'
}

port_is_listening() {
  "${PY_BIN}" - "${OLLAMA_PORT}" <<'PY'
import socket
import sys

with socket.socket() as sock:
    sock.settimeout(0.3)
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

preflight_host() {
  echo "==> Read-only Ollama runtime preflight"
  command -v docker >/dev/null 2>&1 || {
    echo "ERROR: Docker is not installed or not on PATH." >&2
    return 1
  }
  command -v curl >/dev/null 2>&1 || {
    echo "ERROR: curl is required for the Ollama health preflight." >&2
    return 1
  }
  docker info >/dev/null 2>&1 || {
    echo "ERROR: Docker is unavailable to this shell." >&2
    return 1
  }
  [[ -e /dev/kfd ]] || {
    echo "ERROR: /dev/kfd is missing; ROCm passthrough is unavailable." >&2
    return 1
  }
  [[ -e /dev/dri/renderD128 ]] || {
    echo "ERROR: /dev/dri/renderD128 is missing; GPU render access is unavailable." >&2
    return 1
  }

  echo "    expected image: ${OLLAMA_IMAGE}"
  echo "    context policy: ${OLLAMA_CONTEXT_LENGTH} tokens, parallel=${OLLAMA_NUM_PARALLEL}"
  echo "    primary model: ${PRIMARY_MODEL}"
  if docker image inspect "${OLLAMA_IMAGE}" >/dev/null 2>&1; then
    echo "    pinned image present locally: yes"
  else
    echo "    pinned image present locally: no (provision may pull it)"
  fi

  if docker network inspect "${NETWORK}" >/dev/null 2>&1; then
    local driver
    driver="$(docker network inspect --format '{{.Driver}}' "${NETWORK}")"
    if [[ "${driver}" != "bridge" ]]; then
      echo "ERROR: existing network '${NETWORK}' uses driver '${driver}', expected bridge." >&2
      return 1
    fi
    echo "    network '${NETWORK}': existing bridge"
  else
    echo "    network '${NETWORK}': absent (provision may create it)"
  fi

  if ! container_exists; then
    if port_is_listening; then
      echo "ERROR: port ${OLLAMA_PORT} is already in use without container '${OLLAMA_CONTAINER}'." >&2
      echo "       Inspect the existing service; this script will not overwrite it." >&2
      return 1
    fi
    echo "    container '${OLLAMA_CONTAINER}': absent; port ${OLLAMA_PORT} is free"
    return 0
  fi

  local actual_image actual_context actual_parallel actual_max_loaded
  actual_image="$(docker inspect --format '{{.Config.Image}}' "${OLLAMA_CONTAINER}")"
  actual_context="$(container_env_value OLLAMA_CONTEXT_LENGTH)"
  actual_parallel="$(container_env_value OLLAMA_NUM_PARALLEL)"
  actual_max_loaded="$(container_env_value OLLAMA_MAX_LOADED_MODELS)"

  if [[ "${actual_image}" != "${OLLAMA_IMAGE}" ]]; then
    echo "ERROR: existing container '${OLLAMA_CONTAINER}' uses a different image." >&2
    echo "       existing: ${actual_image}" >&2
    echo "       expected: ${OLLAMA_IMAGE}" >&2
    echo "       It was not restarted or recreated. Resolve the in-use stack explicitly." >&2
    return 1
  fi
  if [[ "${actual_context}" != "${OLLAMA_CONTEXT_LENGTH}" ]]; then
    echo "ERROR: existing container context is '${actual_context:-unset}', expected ${OLLAMA_CONTEXT_LENGTH}." >&2
    echo "       It was not restarted or recreated. Resolve the in-use stack explicitly." >&2
    return 1
  fi
  if [[ "${actual_parallel}" != "${OLLAMA_NUM_PARALLEL}" ]]; then
    echo "ERROR: existing container parallel slots are '${actual_parallel:-unset}', expected ${OLLAMA_NUM_PARALLEL}." >&2
    return 1
  fi
  if [[ "${actual_max_loaded}" != "${OLLAMA_MAX_LOADED_MODELS}" ]]; then
    echo "ERROR: existing container max-loaded-models is '${actual_max_loaded:-unset}', expected ${OLLAMA_MAX_LOADED_MODELS}." >&2
    return 1
  fi
  if ! container_has_network; then
    echo "ERROR: existing container is not attached to '${NETWORK}'; refusing to mutate it." >&2
    return 1
  fi
  if ! container_has_port; then
    echo "ERROR: existing container does not publish host port ${OLLAMA_PORT}; refusing to mutate it." >&2
    return 1
  fi

  if container_running; then
    if ! curl -fsS --max-time 5 "${OLLAMA_BASE_URL}/api/version" >/dev/null; then
      echo "ERROR: matching container is running but its version API is unhealthy." >&2
      return 1
    fi
    echo "    container '${OLLAMA_CONTAINER}': running, matching, healthy (left untouched)"
  else
    echo "    container '${OLLAMA_CONTAINER}': stopped, configuration matches"
  fi
}

ensure_image() {
  local present=0
  docker image inspect "${OLLAMA_IMAGE}" >/dev/null 2>&1 && present=1
  case "${OLLAMA_IMAGE_PULL_POLICY}" in
    never)
      if [[ "${present}" != "1" ]]; then
        echo "ERROR: pinned image is absent and pull policy is never: ${OLLAMA_IMAGE}" >&2
        return 1
      fi
      ;;
    ifnotpresent)
      if [[ "${present}" != "1" ]]; then
        echo "==> Pulling pinned Ollama image"
        docker pull "${OLLAMA_IMAGE}"
      fi
      ;;
    always)
      echo "==> Refreshing pinned Ollama image"
      docker pull "${OLLAMA_IMAGE}"
      ;;
  esac
}

ensure_network() {
  if ! docker network inspect "${NETWORK}" >/dev/null 2>&1; then
    echo "==> Creating Docker network '${NETWORK}'"
    docker network create "${NETWORK}" >/dev/null
  fi
}

start_runtime() {
  if container_exists; then
    if container_running; then
      echo "==> Reusing matching running container '${OLLAMA_CONTAINER}' without restart"
      return 0
    fi
    echo "==> Starting matching stopped container '${OLLAMA_CONTAINER}'"
    docker start "${OLLAMA_CONTAINER}" >/dev/null
    return 0
  fi

  local kfd_gid render_gid
  kfd_gid="$(stat -c '%g' /dev/kfd)"
  render_gid="$(stat -c '%g' /dev/dri/renderD128)"
  local -a group_args=("--group-add=${kfd_gid}")
  if [[ "${render_gid}" != "${kfd_gid}" ]]; then
    group_args+=("--group-add=${render_gid}")
  fi

  echo "==> Creating context-pinned Ollama container '${OLLAMA_CONTAINER}'"
  docker run -d \
    --name "${OLLAMA_CONTAINER}" \
    --network="${NETWORK}" \
    --restart unless-stopped \
    -p "${OLLAMA_PORT}:11434" \
    -v "${OLLAMA_VOLUME}:/root/.ollama" \
    --device=/dev/kfd \
    --device=/dev/dri \
    "${group_args[@]}" \
    --cap-add=SYS_PTRACE \
    --security-opt seccomp=unconfined \
    -e "HSA_OVERRIDE_GFX_VERSION=${OLLAMA_HSA_OVERRIDE}" \
    -e "OLLAMA_CONTEXT_LENGTH=${OLLAMA_CONTEXT_LENGTH}" \
    -e "OLLAMA_NUM_PARALLEL=${OLLAMA_NUM_PARALLEL}" \
    -e "OLLAMA_MAX_LOADED_MODELS=${OLLAMA_MAX_LOADED_MODELS}" \
    -e "OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE}" \
    -e OLLAMA_HOST=0.0.0.0:11434 \
    "${OLLAMA_IMAGE}" >/dev/null
}

wait_runtime() {
  echo "==> Waiting for Ollama version API"
  local attempt
  for ((attempt = 1; attempt <= 60; attempt++)); do
    if curl -fsS --max-time 5 "${OLLAMA_BASE_URL}/api/version" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "ERROR: Ollama did not become healthy at ${OLLAMA_BASE_URL}." >&2
  echo "       Inspect without restarting: docker logs ${OLLAMA_CONTAINER}" >&2
  return 1
}

provision_models() {
  echo "==> Ensuring current auto-routed Ollama models are present"
  local tag
  for tag in "${MODEL_TAGS[@]}"; do
    if docker exec "${OLLAMA_CONTAINER}" ollama show "${tag}" >/dev/null 2>&1; then
      echo "    present (not refreshed): ${tag}"
      continue
    fi
    echo "    pulling missing model: ${tag}"
    docker exec "${OLLAMA_CONTAINER}" ollama pull "${tag}"
  done
}

capture_provenance() {
  local mode="${1:-configured}"
  local timestamp output
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  output="${PROVENANCE_DIR}/${timestamp}-${mode}.json"
  local -a args=(
    "${PY_BIN}" "${PROOF_SCRIPT}"
    --base-url "${OLLAMA_BASE_URL}"
    --container "${OLLAMA_CONTAINER}"
    --model "${PRIMARY_MODEL}"
    --expected-image "${OLLAMA_IMAGE}"
    --expected-context "${OLLAMA_CONTEXT_LENGTH}"
    --expected-parallel "${OLLAMA_NUM_PARALLEL}"
    --output "${output}"
  )
  if [[ "${ALLOW_EXPERIMENTAL_CONTEXT}" == "1" ]]; then
    args+=(--allow-experimental-context)
  fi
  if [[ "${mode}" == "loaded" ]]; then
    args+=(--load-probe --require-loaded)
  fi

  echo "==> Capturing secret-safe runtime provenance (${mode})"
  "${args[@]}" >/dev/null
  echo "    provenance: ${output}"
}

validate_settings

case "${ACTION}" in
  print-config)
    print_config
    ;;
  preflight)
    preflight_host
    ;;
  provision)
    preflight_host
    ensure_image
    ensure_network
    start_runtime
    wait_runtime
    provision_models
    capture_provenance configured
    ;;
  prove)
    preflight_host
    if ! container_exists || ! container_running; then
      echo "ERROR: prove requires a running matching container; run provision first." >&2
      exit 1
    fi
    if ! docker exec "${OLLAMA_CONTAINER}" ollama show "${PRIMARY_MODEL}" >/dev/null 2>&1; then
      echo "ERROR: primary model is absent; run provision first: ${PRIMARY_MODEL}" >&2
      exit 1
    fi
    capture_provenance loaded
    ;;
esac
