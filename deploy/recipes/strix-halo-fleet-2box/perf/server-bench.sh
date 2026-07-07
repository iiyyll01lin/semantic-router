#!/usr/bin/env bash
#
# server-bench.sh -- TEST 2: performance difference of DIFFERENT inference servers
# bundled with vllm-sr on one Strix Halo box.
#
# With the vllm-sr stack co-resident (the "bundled" context), it brings up each
# inference server in turn on the same box + same base model, and records the
# server's throughput / TTFT / resource footprint. Optionally it also repoints the
# router at the server and measures the end-to-end through-router path.
#
# Servers (each independently toggled; any that will not build/run on gfx1151 is
# SKIPPED with a recorded reason, never failing the whole run):
#   ollama    - the recipe's own server (llama.cpp-based, ROCm)   [always local]
#   llamacpp  - llama.cpp llama-server (ROCm, OpenAI API)
#   lemonade  - AMD Lemonade Server (OpenAI API)
#   vllm      - vLLM ROCm (gfx1151 support is EXPERIMENTAL)
#
# APPLES-TO-APPLES CAVEAT: the servers load DIFFERENT model artifacts/quantizations
# of the same base model. Each server's quant is recorded in the report's `quant`
# column; treat cross-server deltas as "this server + this quant on this box", not
# a pure engine benchmark. Pin identical artifacts via the *_MODEL / *_QUANT env if
# you need strict parity.
#
# Per-server env (SRV in {OLLAMA,LLAMACPP,LEMONADE,VLLM}); all optional:
#   <SRV>_ENABLE      1/0 include this server                 (default 1)
#   <SRV>_API         ollama|openai                           (per-server default)
#   <SRV>_DIRECT_URL  host URL the probe hits directly        (per-server default)
#   <SRV>_MODEL       model arg the server serves             (per-server default)
#   <SRV>_ROUTER_NET  host:port the ROUTER reaches it at      (per-server default)
#   <SRV>_QUANT       quantization label for the report       (per-server default)
#   <SRV>_UP_CMD      command to start it (idempotent)        (per-server default)
#   <SRV>_READY_URL   health URL to poll                      (per-server default)
#   <SRV>_TEARDOWN    1 => stop the container afterward       (default 1; ollama 0)
#
# Shared env:
#   ROUTER_URL/ROUTER_CONFIG_URL/GATEWAY_CONFIG  router listener/config/file
#   SERVER_BENCH_ROUTER  1 => also do the through-router reconfig probe (default 0)
#   ROUTER_ALIAS         model card to repoint for the router probe
#                        (default google/gemini-2.5-flash-lite)
#   MAX_TOKENS/PROMPT_TOKENS/RUNS/CONCURRENCY  probe shape (default 128/256/3/1)
#   SERVERS              which to run (default "ollama llamacpp lemonade vllm")
#   BOX / OUT            box label / output JSON path
#
# Usage (stack already up):
#   bash server-bench.sh
#   SERVERS="ollama llamacpp" SERVER_BENCH_ROUTER=1 bash server-bench.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=/dev/null
source "${RECIPE_DIR}/fleet_common.sh"
PY_BIN="$(fleet_pybin)"

ROUTER_URL="${ROUTER_URL:-http://localhost:8899/v1}"
ROUTER_CONFIG_URL="${ROUTER_CONFIG_URL:-http://localhost:8080/config/hash}"
GATEWAY_CONFIG="${GATEWAY_CONFIG:-${FLEET_STATE_DIR}/gateway/config.yaml}"
SERVER_BENCH_ROUTER="${SERVER_BENCH_ROUTER:-0}"
ROUTER_ALIAS="${ROUTER_ALIAS:-google/gemini-2.5-flash-lite}"
MAX_TOKENS="${MAX_TOKENS:-128}"
PROMPT_TOKENS="${PROMPT_TOKENS:-256}"
RUNS="${RUNS:-3}"
CONCURRENCY="${CONCURRENCY:-1}"
NETWORK="${NETWORK:-vllm-sr-network}"
SERVERS="${SERVERS:-ollama llamacpp lemonade vllm}"
BOX="${BOX:-$(hostname 2>/dev/null || echo box)}"
OUT="${OUT:-${SCRIPT_DIR}/server-${BOX}.json}"
COMMON_MODEL_HINT="${COMMON_MODEL_HINT:-qwen2.5-7b}"

# --- per-server defaults (all overridable via env) ------------------------- #
: "${OLLAMA_ENABLE:=1}"    ; : "${OLLAMA_API:=ollama}"
: "${OLLAMA_DIRECT_URL:=http://localhost:11434}" ; : "${OLLAMA_MODEL:=qwen2.5:7b}"
: "${OLLAMA_ROUTER_NET:=ollama:11434}" ; : "${OLLAMA_QUANT:=Q4_0 (ollama default)}"
: "${OLLAMA_UP_CMD:=docker exec ollama ollama pull qwen2.5:7b}"
: "${OLLAMA_READY_URL:=http://localhost:11434/api/tags}" ; : "${OLLAMA_TEARDOWN:=0}"

: "${LLAMACPP_ENABLE:=1}"  ; : "${LLAMACPP_API:=openai}"
: "${LLAMACPP_DIRECT_URL:=http://localhost:8081/v1}" ; : "${LLAMACPP_MODEL:=qwen2.5-7b}"
: "${LLAMACPP_ROUTER_NET:=llama-server:8080}" ; : "${LLAMACPP_QUANT:=Q4_K_M}"
: "${LLAMACPP_HF:=bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M}"
: "${LLAMACPP_IMAGE:=ghcr.io/ggml-org/llama.cpp:server-rocm}"
: "${LLAMACPP_UP_CMD:=docker run -d --name llama-server --network ${NETWORK} --restart unless-stopped -p 8081:8080 --device=/dev/kfd --device=/dev/dri --group-add=video --cap-add=SYS_PTRACE --security-opt seccomp=unconfined -e HSA_OVERRIDE_GFX_VERSION=11.5.1 ${LLAMACPP_IMAGE} -hf ${LLAMACPP_HF} --host 0.0.0.0 --port 8080 -ngl 999}"
: "${LLAMACPP_READY_URL:=http://localhost:8081/health}" ; : "${LLAMACPP_TEARDOWN:=1}"

: "${LEMONADE_ENABLE:=1}"  ; : "${LEMONADE_API:=openai}"
: "${LEMONADE_DIRECT_URL:=http://localhost:8000/api/v1}" ; : "${LEMONADE_MODEL:=Qwen2.5-7B-Instruct-GGUF}"
: "${LEMONADE_ROUTER_NET:=host.docker.internal:8000}" ; : "${LEMONADE_QUANT:=Q4_0 (lemonade registry)}"
: "${LEMONADE_UP_CMD:=lemonade-server serve --port 8000}"
: "${LEMONADE_READY_URL:=http://localhost:8000/api/v1/models}" ; : "${LEMONADE_TEARDOWN:=1}"

: "${VLLM_ENABLE:=1}"      ; : "${VLLM_API:=openai}"
: "${VLLM_DIRECT_URL:=http://localhost:8002/v1}" ; : "${VLLM_MODEL:=Qwen/Qwen2.5-7B-Instruct}"
: "${VLLM_ROUTER_NET:=vllm:8000}" ; : "${VLLM_QUANT:=fp16 (or awq)}"
: "${VLLM_IMAGE:=rocm/vllm-dev:main}"
: "${VLLM_UP_CMD:=docker run -d --name vllm --network ${NETWORK} --restart unless-stopped -p 8002:8000 --device=/dev/kfd --device=/dev/dri --group-add=video --cap-add=SYS_PTRACE --security-opt seccomp=unconfined --shm-size 8g -e HSA_OVERRIDE_GFX_VERSION=11.5.1 ${VLLM_IMAGE} vllm serve ${VLLM_MODEL} --host 0.0.0.0 --port 8000}"
: "${VLLM_READY_URL:=http://localhost:8002/v1/models}" ; : "${VLLM_TEARDOWN:=1}"

WORK="$(mktemp -d "${FLEET_STATE_DIR}/server-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
LOG_DIR="$(dirname "${OUT}")/server-logs"
mkdir -p "${LOG_DIR}"
echo "==> [server-bench] box=${BOX}  servers='${SERVERS}'  router-path=${SERVER_BENCH_ROUTER}"
echo "    common base model: ${COMMON_MODEL_HINT} (quant differs per server -- see report)"
# Test 2 measures each server WHILE the vllm-sr stack is co-resident ("bundled").
# If the router API is down the stack is not up, so footprint/router-overhead will
# not reflect bundling -- warn loudly rather than report a misleading number.
if ! curl -fsS "${ROUTER_CONFIG_URL}" >/dev/null 2>&1; then
  echo "    WARNING: vllm-sr router not answering at ${ROUTER_CONFIG_URL}; the stack" >&2
  echo "             looks DOWN, so these numbers are NOT 'bundled'. Bring the gateway" >&2
  echo "             up first for a true Test 2 (see gateway-bring-up.sh)." >&2
fi

svar() { local name="$1_$2"; echo "${!name}"; }   # svar OLLAMA API -> $OLLAMA_API

# Map a server key to its container name (for logs + teardown of skipped servers).
container_of() { case "$1" in llamacpp) echo llama-server;; vllm) echo vllm;; lemonade) echo lemonade;; *) echo "$1";; esac; }

wait_url() { local url="$1" tries="${2:-90}" i; for ((i=0;i<tries;i++)); do curl -fsS "${url}" >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }

# measure_direct SRV -> writes ${WORK}/<srv>.json + <srv>-res.json
measure_direct() {
  local srv="$1" lc; lc="$(echo "${srv}" | tr 'A-Z' 'a-z')"
  local nd="${WORK}/${lc}.ndjson" pid="${WORK}/${lc}.pid"
  "${PY_BIN}" "${SCRIPT_DIR}/resource_sampler.py" start --out "${nd}" --pidfile "${pid}" --interval 1 >/dev/null 2>&1 || true
  "${PY_BIN}" "${SCRIPT_DIR}/tokrate_probe.py" \
    --backend-url "$(svar "${srv}" DIRECT_URL)" --api "$(svar "${srv}" API)" \
    --model "$(svar "${srv}" MODEL)" --max-tokens "${MAX_TOKENS}" --prompt-tokens "${PROMPT_TOKENS}" \
    --runs "${RUNS}" --concurrency "${CONCURRENCY}" --label "${lc}-direct" \
    --out "${WORK}/${lc}.json" >/dev/null 2>&1 || true
  "${PY_BIN}" "${SCRIPT_DIR}/resource_sampler.py" stop --pidfile "${pid}" --in "${nd}" \
    --out "${WORK}/${lc}-res.json" >/dev/null 2>&1 || true
}

# measure_router SRV : repoint ROUTER_ALIAS at this server, hot-reload, probe. Best-effort.
measure_router() {
  local srv="$1" lc; lc="$(echo "${srv}" | tr 'A-Z' 'a-z')"
  [[ "${SERVER_BENCH_ROUTER}" == "1" ]] || return 0
  [[ -f "${GATEWAY_CONFIG}" ]] || { echo "    (router path skipped: no ${GATEWAY_CONFIG})"; return 0; }
  cp -f "${GATEWAY_CONFIG}" "${WORK}/${lc}-config.bak"
  if ! "${PY_BIN}" "${SCRIPT_DIR}/repoint_backend.py" --config "${GATEWAY_CONFIG}" \
        --alias "${ROUTER_ALIAS}" --endpoint "$(svar "${srv}" ROUTER_NET)" \
        --model "$(svar "${srv}" MODEL)" >/dev/null 2>&1; then
    echo "    (router path skipped: repoint failed)"; cp -f "${WORK}/${lc}-config.bak" "${GATEWAY_CONFIG}"; return 0
  fi
  wait_url "${ROUTER_CONFIG_URL}" 30 || true   # let fsnotify hot-reload settle
  sleep 3
  "${PY_BIN}" "${SCRIPT_DIR}/tokrate_probe.py" \
    --backend-url "${ROUTER_URL}" --api openai --model "${ROUTER_ALIAS}" \
    --max-tokens "${MAX_TOKENS}" --prompt-tokens "${PROMPT_TOKENS}" \
    --runs "${RUNS}" --concurrency "${CONCURRENCY}" --label "${lc}-router" \
    --out "${WORK}/${lc}-router.json" >/dev/null 2>&1 || true
  cp -f "${WORK}/${lc}-config.bak" "${GATEWAY_CONFIG}"   # restore original backend
  sleep 2
}

# --- run each server ------------------------------------------------------- #
for srv_lc in ${SERVERS}; do
  SRV="$(echo "${srv_lc}" | tr 'a-z' 'A-Z')"
  lc="${srv_lc}"
  meta="${WORK}/${lc}.meta.json"
  if [[ "$(svar "${SRV}" ENABLE)" != "1" ]]; then
    echo "{\"server\":\"${lc}\",\"status\":\"disabled\"}" >"${meta}"; continue
  fi
  echo "==> [server-bench] ${lc}: bring up"
  if ! eval "$(svar "${SRV}" UP_CMD)" >"${WORK}/${lc}-up.log" 2>&1; then
    echo "    SKIP ${lc}: bring-up command failed (see server-logs/${lc}-up.log)"
    cp -f "${WORK}/${lc}-up.log" "${LOG_DIR}/${lc}-up.log" 2>/dev/null || true
    echo "{\"server\":\"${lc}\",\"status\":\"skipped\",\"reason\":\"bring-up failed\",\"quant\":\"$(svar "${SRV}" QUANT)\"}" >"${meta}"
    continue
  fi
  if ! wait_url "$(svar "${SRV}" READY_URL)" 120; then
    echo "    SKIP ${lc}: never became ready at $(svar "${SRV}" READY_URL) (see server-logs/${lc}-*.log)"
    cname="$(container_of "${lc}")"
    cp -f "${WORK}/${lc}-up.log" "${LOG_DIR}/${lc}-up.log" 2>/dev/null || true
    docker logs --tail 150 "${cname}" >"${LOG_DIR}/${lc}-container.log" 2>&1 || true
    echo "{\"server\":\"${lc}\",\"status\":\"skipped\",\"reason\":\"not ready\",\"quant\":\"$(svar "${SRV}" QUANT)\"}" >"${meta}"
    [[ "$(svar "${SRV}" TEARDOWN)" == "1" ]] && docker rm -f "${cname}" >/dev/null 2>&1 || true
    continue
  fi
  echo "==> [server-bench] ${lc}: measure direct"
  measure_direct "${SRV}"
  echo "==> [server-bench] ${lc}: measure through-router (${SERVER_BENCH_ROUTER})"
  measure_router "${SRV}"
  echo "{\"server\":\"${lc}\",\"status\":\"measured\",\"api\":\"$(svar "${SRV}" API)\",\"model\":\"$(svar "${SRV}" MODEL)\",\"quant\":\"$(svar "${SRV}" QUANT)\"}" >"${meta}"
  if [[ "$(svar "${SRV}" TEARDOWN)" == "1" ]]; then
    echo "    teardown ${lc}"
    # container name is the server's own; ollama is never torn down (TEARDOWN=0).
    case "${lc}" in
      llamacpp) docker rm -f llama-server >/dev/null 2>&1 || true ;;
      vllm)     docker rm -f vllm >/dev/null 2>&1 || true ;;
      lemonade) pkill -f "lemonade-server" >/dev/null 2>&1 || true ;;
    esac
  fi
done

# --- assemble the comparison ----------------------------------------------- #
echo "==> [server-bench] assembling ${OUT}"
"${PY_BIN}" - "${WORK}" "${OUT}" "${BOX}" "${MAX_TOKENS}" "${PROMPT_TOKENS}" "${RUNS}" \
  "${CONCURRENCY}" "${COMMON_MODEL_HINT}" "${SERVERS}" <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone

(work, out_path, box, max_tokens, prompt_tokens, runs, concurrency,
 common_model, servers_spec) = sys.argv[1:10]


def load(name):
    try:
        with open(os.path.join(work, name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def agg(p):
    return (p or {}).get("aggregate") or {}


def res_stack_mem(res):
    total = 0
    for cname, rec in (res.get("containers") or {}).items():
        low = cname.lower()
        if any(n in low for n in ("router", "envoy", "dashboard", "grafana", "prometheus")):
            mm = (rec or {}).get("mem_used_b") or {}
            if mm.get("max"):
                total += mm["max"]
    return total or None


rows = []
for lc in servers_spec.split():
    meta = load(lc + ".meta.json")
    if meta.get("status") not in ("measured",):
        rows.append({"server": lc, "status": meta.get("status", "unknown"),
                     "reason": meta.get("reason"), "quant": meta.get("quant")})
        continue
    direct = load(lc + ".json")
    router = load(lc + "-router.json")
    res = load(lc + "-res.json")
    da, ra = agg(direct), agg(router)
    peak_vram = ((res.get("gpu") or {}).get("vram_used_b") or {}).get("max")
    peak_gtt = ((res.get("gpu") or {}).get("gtt_used_b") or {}).get("max")
    rows.append({
        "server": lc,
        "status": "measured",
        "api": meta.get("api"),
        "model": meta.get("model"),
        "quant": meta.get("quant"),
        "direct_decode_tps": da.get("decode_tps_median"),
        "direct_ttft_ms": da.get("ttft_ms_mean"),
        "direct_success_rate": da.get("success_rate"),
        "router_decode_tps": ra.get("decode_tps_median"),
        "router_ttft_ms": ra.get("ttft_ms_mean"),
        "router_overhead_pct": (
            (da.get("decode_tps_median") - ra.get("decode_tps_median")) / da.get("decode_tps_median") * 100.0
            if da.get("decode_tps_median") and ra.get("decode_tps_median") else None),
        "peak_vram_used_b": peak_vram,
        "peak_gtt_used_b": peak_gtt,
        "stack_container_mem_b": res_stack_mem(res),
    })

# Deltas vs the ollama baseline row (if measured).
baseline = next((r for r in rows if r["server"] == "ollama" and r.get("status") == "measured"), None)
if baseline and baseline.get("direct_decode_tps"):
    b = baseline["direct_decode_tps"]
    for r in rows:
        if r.get("direct_decode_tps"):
            r["decode_tps_vs_ollama_pct"] = (r["direct_decode_tps"] - b) / b * 100.0

report = {
    "schema": "server-bench/v1",
    "box": box,
    "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "common_base_model": common_model,
    "parity_note": "servers load different quantizations of the same base model; see per-row `quant`.",
    "shape": {"max_tokens": int(max_tokens), "prompt_tokens": int(prompt_tokens),
              "runs": int(runs), "concurrency": int(concurrency)},
    "servers": rows,
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2, sort_keys=True)
    fh.write("\n")

print("== inference-server comparison (box=%s, base=%s) ==" % (box, common_model))
print("%-10s %-8s %10s %10s %10s %s" % ("server", "status", "tps", "ttft_ms", "vs_ollama", "quant"))
for r in rows:
    if r.get("status") != "measured":
        print("%-10s %-8s %10s %10s %10s %s" % (r["server"], r.get("status"), "-", "-", "-", r.get("reason") or ""))
        continue
    print("%-10s %-8s %10s %10s %10s %s" % (
        r["server"], "ok",
        "%.1f" % r["direct_decode_tps"] if r.get("direct_decode_tps") else "-",
        "%.0f" % r["direct_ttft_ms"] if r.get("direct_ttft_ms") else "-",
        "%+.1f%%" % r["decode_tps_vs_ollama_pct"] if r.get("decode_tps_vs_ollama_pct") is not None else "-",
        r.get("quant") or ""))
print("(written to %s)" % out_path)
PYEOF

echo "==> [server-bench] done -> ${OUT}"
