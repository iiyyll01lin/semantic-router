#!/usr/bin/env bash
#
# ttft_sweetspot_driver.sh -- Task A: interactive-latency sweet-spot sweep for the
# MXFP4 gpt-oss-120b GGUF on Halo-B via llama.cpp (llama-server ROCm), resident.
#
# For N in {1,2,4,8}: (re)start llama-server with --parallel N (-ngl 999, resident,
# cached GGUF => no re-download), then probe with tokrate_probe.py at client
# concurrency 1 and N. Records raw probe JSON per (N, concurrency); a local
# assembler computes decode/aggregate tok/s + TTFT p50/p95.
#
# Runs under nohup so it survives the controller's SSH turn dropping. Never edits
# repo/docs, never commits. Self-contained; stdlib-only probe.
set -uo pipefail

PERF=/home/test001/gemma-bench/strix-halo-fleet-2box/perf
OUT=/home/test001/ttft-sweetspot
LOGS="${OUT}/logs"
mkdir -p "${OUT}" "${LOGS}"

IMG=ghcr.io/ggml-org/llama.cpp:server-rocm
NET=vllm-sr-network
PORT=8081
CTR=llama-server
HF=ggml-org/gpt-oss-120b-GGUF
export TOKRATE_DEADLINE=120

stamp() { date -u +%FT%TZ; }
say()   { echo "[$(stamp)] $*"; }

up() { # parallel -> bring up llama-server resident, wait for /health
  local par="$1"
  docker rm -f "${CTR}" >/dev/null 2>&1 || true
  # NOTE: no --restart policy on purpose: a crash/OOM must stay visible, not be
  # silently restarted mid-measurement. Exact task-spec args otherwise.
  docker run -d --name "${CTR}" --network "${NET}" \
    -p "${PORT}:8080" --device=/dev/kfd --device=/dev/dri --group-add=video \
    --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
    -e HSA_OVERRIDE_GFX_VERSION=11.5.1 -e LLAMA_CACHE=/root/.cache/llama.cpp \
    -v llamacpp-cache:/root/.cache/llama.cpp \
    "${IMG}" -hf "${HF}" --host 0.0.0.0 --port 8080 \
    -ngl 999 --jinja --no-mmap --parallel "${par}" \
    >"${LOGS}/up-p${par}.log" 2>&1 || { say "docker run failed p=${par}"; return 1; }
  # Wait for health (covers resident load of the ~59 GiB GGUF from warm cache).
  local i
  for i in $(seq 1 300); do
    if curl -fsS --max-time 5 "http://localhost:${PORT}/health" >/dev/null 2>&1; then
      say "llama-server healthy p=${par} after $((i*2))s"
      return 0
    fi
    if ! docker ps --format '{{.Names}}' | grep -q "^${CTR}$"; then
      say "container ${CTR} exited during load p=${par}"
      docker logs --tail 80 "${CTR}" >>"${LOGS}/up-p${par}.log" 2>&1 || true
      return 1
    fi
    sleep 2
  done
  say "TIMEOUT waiting for health p=${par}"
  return 1
}

MODELID=""
resolve_model() {
  MODELID="$(curl -fsS --max-time 10 "http://localhost:${PORT}/v1/models" 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["data"][0]["id"])' 2>/dev/null)"
  [ -z "${MODELID}" ] && MODELID="gpt-oss-120b"
  echo "${MODELID}" >"${OUT}/served-model-id.txt"
  say "served model id = ${MODELID}"
}

probe() { # parallel concurrency runs warmup
  local par="$1" conc="$2" runs="$3" warm="$4"
  say "probe p=${par} concurrency=${conc} runs=${runs}"
  TOKRATE_DEADLINE=120 python3 "${PERF}/tokrate_probe.py" \
    --backend-url "http://localhost:${PORT}/v1" --api openai --model "${MODELID}" \
    --max-tokens 128 --prompt-tokens 256 --runs "${runs}" --concurrency "${conc}" \
    --warmup "${warm}" --label "p${par}-c${conc}" \
    --out "${OUT}/p${par}-c${conc}.json" >"${LOGS}/probe-p${par}-c${conc}.log" 2>&1 \
    && say "probe OK p=${par} c=${conc}" || say "probe FAILED p=${par} c=${conc} (see log)"
}

say "SWEEP_START pid=$$ ngl=999 no-mmap resident cached-GGUF"
for N in 1 2 4 8; do
  say "=== bring up --parallel ${N} ==="
  if ! up "${N}"; then
    say "BRINGUP_FAILED N=${N} (skip-with-reason, continue)"
    continue
  fi
  docker logs --tail 40 "${CTR}" >"${LOGS}/serverlog-p${N}.log" 2>&1 || true
  resolve_model
  # concurrency 1 (latency-optimal single stream); 8 samples for TTFT percentiles
  probe "${N}" 1 8 1
  # concurrency = N (server-slot-matched operating point); ~16 samples
  if [ "${N}" -ne 1 ]; then
    case "${N}" in 2) R=8;; 4) R=4;; 8) R=2;; *) R=2;; esac
    probe "${N}" "${N}" "${R}" 0
  fi
done

say "=== teardown llama-server ==="
docker rm -f "${CTR}" >/dev/null 2>&1 || true
say "SWEEP_DONE"
