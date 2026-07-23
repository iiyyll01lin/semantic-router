#!/usr/bin/env bash
#
# bestcfg-matrix.sh -- single-run "configuration COMBINATION matrix" benchmark for
# the flagship model on Halo-B (96 GiB carveout, headless, full vllm-sr stack
# co-resident). Instead of stitching the best single number from several unrelated
# runs (the old perf-report §11.3 approach), it runs every candidate config as ONE
# end-to-end profile and picks a winner with a single fixed rule.
#
# THREE backend axes -> 8 cells:
#   server     : ollama | llamacpp (llama-server ROCm, OpenAI API)
#   residency  : resident (100% GPU) | auto (server layer-estimate -> CPU-offload)
#   NUM_PARALLEL: 1 | 8  (backend decode slots)
#     * ollama resident = the `-vram` variant (num_gpu 999 + use_mmap false; see
#       make-vram-resident-models.sh); ollama auto = the plain tag (the 96 GiB
#       CPU-offload trap). NUM_PARALLEL via container env OLLAMA_NUM_PARALLEL.
#     * llamacpp resident = `-ngl 999`; llamacpp auto = partial `-ngl` (forces CPU
#       offload for contrast). NUM_PARALLEL via `--parallel`.
# Each cell is probed at client concurrency c1 AND c8 (single-stream decode tok/s,
# aggregate tok/s, TTFT p50/p95), wrapped in resource_sampler.py (peak VRAM/GTT/RAM
# to verify true residency), plus one sustained decode through the power sampler.
#
# Semantic cache (0.92 + exact-repeat) is an OVERLAY on each server's WINNING cell
# only (it changes repeat-query TTFT, not decode/throughput) -- it never
# re-multiplies the 8 cells. It runs through the router path (repoint + hot-reload,
# reusing repoint_backend.py + cache-sweep.sh mechanics).
#
# Winner rule (fixed; implemented in bestcfg_matrix.py):
#   1. Cell must be `loaded` AND single-stream decode >= OOM_MIN_TPS (usable floor).
#   2. Primary: highest c8 AGGREGATE tok/s among cells with TTFT p95 @ c8 <= gate (2s).
#   3. Tie-break: tok/s per W -> TTFT p50 @ c1 -> single-stream decode.
#   4. Cache overlay is reported separately, not in the backend ranking.
#
# SKIP-WITH-REASON philosophy: any cell that will not load (e.g. llama.cpp cannot
# place the MXFP4 120B on gfx1151) is recorded skipped/load-fail and the matrix
# continues -- one bad cell never fails the whole run.
#
# ============================ REPRODUCE ON HALO-B ============================
#   # Prereqs: full vllm-sr stack up (gateway-bring-up.sh), 96 GiB carveout,
#   # headless; ollama has gpt-oss:120b pulled; the `-vram` variant built:
#   TAGS="gpt-oss:120b" VERIFY=0 bash perf/make-vram-resident-models.sh
#
#   # Full matrix + cache overlay (both servers), write rollup + table:
#   bash perf/bestcfg-matrix.sh
#
#   # ollama only (skip llama.cpp bring-up entirely):
#   SERVERS="ollama" bash perf/bestcfg-matrix.sh
#
#   # llama.cpp with the non-parity fallback GGUF if the 120B will not load:
#   LLAMACPP_ALLOW_FALLBACK=1 bash perf/bestcfg-matrix.sh
#
#   # Offline self-test (NO ROCm / Docker / hardware -- mock backends only):
#   SELFTEST=1 bash perf/bestcfg-matrix.sh
# ===========================================================================
#
# Env knobs (all optional; defaults suit Halo-B):
#   MODEL_FLAGSHIP        flagship model label                (gpt-oss:120b)
#   SERVERS               which servers to run          (default "ollama llamacpp")
#   CELLS                 explicit cell-id list to run  (default: all 8)
#   OLLAMA_RESIDENT_TAG   -vram variant tag            (gpt-oss:120b-vram)
#   OLLAMA_AUTO_TAG       plain tag                    (gpt-oss:120b)
#   OLLAMA_URL/CONTAINER/IMAGE/PORT/VOLUME/NETWORK  ollama container spec
#   OLLAMA_RECREATE       1 => recreate ollama to set OLLAMA_NUM_PARALLEL (default 1)
#   LLAMACPP_HF           gpt-oss-120b GGUF for -hf   (ggml-org/gpt-oss-120b-GGUF)
#   LLAMACPP_FALLBACK_HF  non-parity fallback GGUF (a 70B-Q4) if the 120B won't load
#   LLAMACPP_ALLOW_FALLBACK 1 => use the fallback GGUF on 120B load-fail (default 0)
#   LLAMACPP_NGL_AUTO     partial -ngl for the auto (CPU-offload) cells (default 20)
#   LLAMACPP_IMAGE/PORT/MODEL/CONTAINER  llama-server container spec
#   LLAMACPP_CACHE_VOLUME persistent HF cache volume so the ~60 GiB GGUF is pulled
#                         ONCE (probe) and reused by every cell    (llamacpp-cache)
#   LLAMACPP_PROBE_TRIES  /health wait tries for the initial probe (covers the first
#                         ~60 GiB download; cells reuse the warm cache) (900 ~=30min)
#   MAX_TOKENS/PROMPT_TOKENS   decode/prompt budget for tok/TTFT (default 128/256)
#   RUNS_C1               sequential single-stream runs @ c1     (default 3)
#   CONCURRENCY_HI        the "c8" concurrency level             (default 8)
#   POWER_MAX_TOKENS      sustained decode length for power      (default 1400)
#   OOM_MIN_TPS           usable single-stream floor tok/s       (default 3)
#   TTFT_GATE_MS          TTFT p95 @ c8 gate for the winner      (default 2000)
#   CACHE_OVERLAY         1 => run the cache off/on overlay       (default 1)
#   CACHE_THRESHOLD       semantic-cache similarity threshold     (default 0.92)
#   ROUTER_URL/ROUTER_CONFIG_URL/GATEWAY_CONFIG/ROUTER_CONTAINER/ROUTER_ALIAS
#   BOX / OUT             box label / rollup JSON path
#   SELFTEST              1 => mock backends, no docker/ROCm/hardware (default 0)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=/dev/null
source "${RECIPE_DIR}/fleet_common.sh"
PY_BIN="$(fleet_pybin)"
# Make bestcfg_matrix.py importable by the inline `python3 -c` helpers below.
export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

SELFTEST="${SELFTEST:-0}"
MODEL_FLAGSHIP="${MODEL_FLAGSHIP:-gpt-oss:120b}"
SERVERS="${SERVERS:-ollama llamacpp}"

NETWORK="${NETWORK:-vllm-sr-network}"
OLLAMA_RESIDENT_TAG="${OLLAMA_RESIDENT_TAG:-gpt-oss:120b-vram}"
OLLAMA_AUTO_TAG="${OLLAMA_AUTO_TAG:-gpt-oss:120b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-ollama}"
OLLAMA_IMAGE="${OLLAMA_IMAGE:-ollama/ollama:rocm}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
OLLAMA_VOLUME="${OLLAMA_VOLUME:-ollama}"
OLLAMA_RECREATE="${OLLAMA_RECREATE:-1}"

LLAMACPP_HF="${LLAMACPP_HF:-ggml-org/gpt-oss-120b-GGUF}"
LLAMACPP_FALLBACK_HF="${LLAMACPP_FALLBACK_HF:-bartowski/Meta-Llama-3.1-70B-Instruct-GGUF:Q4_K_M}"
LLAMACPP_ALLOW_FALLBACK="${LLAMACPP_ALLOW_FALLBACK:-0}"
LLAMACPP_NGL_AUTO="${LLAMACPP_NGL_AUTO:-20}"
LLAMACPP_IMAGE="${LLAMACPP_IMAGE:-ghcr.io/ggml-org/llama.cpp:server-rocm}"
LLAMACPP_PORT="${LLAMACPP_PORT:-8081}"
LLAMACPP_MODEL="${LLAMACPP_MODEL:-gpt-oss-120b}"
LLAMACPP_CONTAINER="${LLAMACPP_CONTAINER:-llama-server}"
# Persistent HF cache: mount this named volume at /root/.cache/llama.cpp so the
# ~60 GiB GGUF that `-hf` pulls is downloaded ONCE (probe) and reused by the probe
# plus every cell -- previously each `docker run` re-pulled it, which on a near-full
# disk could never finish and skipped all llamacpp cells (a disk artifact).
LLAMACPP_CACHE_VOLUME="${LLAMACPP_CACHE_VOLUME:-llamacpp-cache}"
LLAMACPP_DIRECT_URL="${LLAMACPP_DIRECT_URL:-http://localhost:${LLAMACPP_PORT}/v1}"
LLAMACPP_READY_URL="${LLAMACPP_READY_URL:-http://localhost:${LLAMACPP_PORT}/health}"
# /health wait tries for the INITIAL probe bring-up: generous enough to also cover
# the first ~60 GiB download (900 tries x 2s ~= 30 min). Warm cell bring-ups keep
# the shorter default wait inside llamacpp_up().
LLAMACPP_PROBE_TRIES="${LLAMACPP_PROBE_TRIES:-900}"
LLAMACPP_ROUTER_NET="${LLAMACPP_ROUTER_NET:-llama-server:8080}"

MAX_TOKENS="${MAX_TOKENS:-128}"
PROMPT_TOKENS="${PROMPT_TOKENS:-256}"
RUNS_C1="${RUNS_C1:-3}"
CONCURRENCY_HI="${CONCURRENCY_HI:-8}"
POWER_MAX_TOKENS="${POWER_MAX_TOKENS:-1400}"
OOM_MIN_TPS="${OOM_MIN_TPS:-3}"
TTFT_GATE_MS="${TTFT_GATE_MS:-2000}"

CACHE_OVERLAY="${CACHE_OVERLAY:-1}"
CACHE_THRESHOLD="${CACHE_THRESHOLD:-0.92}"
ROUTER_URL="${ROUTER_URL:-http://localhost:8899/v1}"
ROUTER_CONFIG_URL="${ROUTER_CONFIG_URL:-http://localhost:8080/config/hash}"
ROUTER_CONTAINER="${ROUTER_CONTAINER:-vllm-sr-router-container}"
ROUTER_ALIAS="${ROUTER_ALIAS:-google/gemini-2.5-flash-lite}"

# Resolve the config the router actually watches (same logic as cache-sweep.sh).
GATEWAY_DIR="${GATEWAY_DIR:-${FLEET_STATE_DIR}/gateway}"
if [[ -z "${GATEWAY_CONFIG:-}" ]]; then
  if [[ -f "${GATEWAY_DIR}/.vllm-sr/runtime-config.yaml" ]]; then
    GATEWAY_CONFIG="${GATEWAY_DIR}/.vllm-sr/runtime-config.yaml"
  else
    GATEWAY_CONFIG="${GATEWAY_DIR}/config.yaml"
  fi
fi

BOX="${BOX:-$(hostname 2>/dev/null || echo box)}"
[[ -z "${OUT:-}" ]] && OUT_DEFAULTED=1
OUT="${OUT:-${SCRIPT_DIR}/quant-frontier/bestcfg-matrix-${BOX}.json}"

WORK="$(mktemp -d "${FLEET_STATE_DIR}/bestcfg-matrix-XXXXXX")"

# --- SELFTEST scaffolding -------------------------------------------------- #
MOCK_PID=""
SELFTEST_CONFIG=""
OLLAMA_PARALLEL_CUR="__unset__"
cleanup() {
  [[ -n "${MOCK_PID}" ]] && kill "${MOCK_PID}" >/dev/null 2>&1 || true
  # Only restore the ollama container if WE actually recreated it (never touch an
  # ollama we never modified -- the stack depends on it).
  if [[ "${OLLAMA_PARALLEL_CUR}" != "__unset__" && "${SELFTEST}" != "1" ]]; then
    recreate_ollama "" || true   # restore server-default OLLAMA_NUM_PARALLEL
  fi
  rm -rf "${WORK}"
}
trap cleanup EXIT

echo "==> [bestcfg-matrix] box=${BOX}  model=${MODEL_FLAGSHIP}  servers='${SERVERS}'  selftest=${SELFTEST}"

if [[ "${SELFTEST}" == "1" ]]; then
  echo "==> [bestcfg-matrix] SELFTEST: starting mock backend (no docker/ROCm)"
  "${PY_BIN}" "${SCRIPT_DIR}/bestcfg_matrix.py" mock-serve --portfile "${WORK}/mock.port" \
    >"${WORK}/mock.log" 2>&1 &
  MOCK_PID=$!
  for _ in $(seq 1 50); do [[ -s "${WORK}/mock.port" ]] && break; sleep 0.1; done
  MOCK_PORT="$(cat "${WORK}/mock.port" 2>/dev/null || echo 0)"
  [[ "${MOCK_PORT}" != "0" ]] || { echo "ERROR: mock backend did not start" >&2; exit 1; }
  MOCK_BASE="http://127.0.0.1:${MOCK_PORT}"
  OLLAMA_URL="${MOCK_BASE}"
  LLAMACPP_DIRECT_URL="${MOCK_BASE}/v1"
  LLAMACPP_READY_URL="${MOCK_BASE}/health"
  ROUTER_URL="${MOCK_BASE}/v1"
  SELFTEST_CONFIG="${WORK}/selftest-config.yaml"
  cat >"${SELFTEST_CONFIG}" <<'YAML'
providers:
    models:
        - name: google/gemini-2.5-flash-lite
          backend_refs:
            - name: ollama_local
              endpoint: ollama:11434
              protocol: http
          external_model_ids:
            vllm: gpt-oss:120b
routing:
    decisions:
        - name: default
          plugins:
            - type: reasoning
    modelCards:
        - name: google/gemini-2.5-flash-lite
          quality_score: 0.68
stores:
    semantic_cache:
        enabled: true
        similarity_threshold: 0.85
YAML
  GATEWAY_CONFIG="${SELFTEST_CONFIG}"
  # Never dirty the repo with self-test output -- write into the temp work dir
  # unless the caller pinned an explicit OUT.
  [[ "${OUT_DEFAULTED:-0}" == "1" ]] && OUT="${WORK}/bestcfg-matrix-selftest.json"
fi

mkdir -p "$(dirname "${OUT}")"
LOG_DIR="$(dirname "${OUT}")/bestcfg-matrix-logs"
mkdir -p "${LOG_DIR}"
echo "    out=${OUT}"

# =========================================================================== #
# helpers
# =========================================================================== #
wait_url() { local url="$1" tries="${2:-90}" i; for ((i=0;i<tries;i++)); do curl -fsS "${url}" >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }

json_str() {  # emit a JSON string literal (or null) for a shell value
  if [[ -z "${1:-}" ]]; then echo null; else printf '%s' "$1" | "${PY_BIN}" -c 'import json,sys;print(json.dumps(sys.stdin.read()))'; fi
}

write_load_meta() {  # cid server residency parallel model load_result reason gpu_frac
  local cid="$1" server="$2" residency="$3" parallel="$4" model="$5" lr="$6" reason="$7" frac="${8:-}"
  local reason_json frac_json
  reason_json="$(json_str "${reason}")"
  frac_json="${frac:-null}"
  cat >"${WORK}/${cid}.load.json" <<JSON
{"cell_id":"${cid}","server":"${server}","residency":"${residency}","num_parallel":${parallel},"model":"${model}","load_result":"${lr}","reason":${reason_json},"gpu_resident_frac":${frac_json}}
JSON
}

# recreate_ollama PARALLEL : recreate the ollama container with (or without, if
# PARALLEL empty) OLLAMA_NUM_PARALLEL set. Models persist on the named volume; only
# the in-memory model is dropped. No-op in SELFTEST or when OLLAMA_RECREATE!=1.
recreate_ollama() {
  local parallel="$1"
  [[ "${SELFTEST}" == "1" || "${OLLAMA_RECREATE}" != "1" ]] && return 0
  local args=(-d --name "${OLLAMA_CONTAINER}" --network="${NETWORK}" --restart unless-stopped
    -p "${OLLAMA_PORT}:${OLLAMA_PORT}" -v "${OLLAMA_VOLUME}:/root/.ollama"
    --device=/dev/kfd --device=/dev/dri --group-add=video
    --cap-add=SYS_PTRACE --security-opt seccomp=unconfined
    -e HSA_OVERRIDE_GFX_VERSION=11.5.1)
  [[ -n "${parallel}" ]] && args+=(-e "OLLAMA_NUM_PARALLEL=${parallel}")
  docker rm -f "${OLLAMA_CONTAINER}" >/dev/null 2>&1 || true
  docker run "${args[@]}" "${OLLAMA_IMAGE}" >/dev/null 2>&1 || return 1
  wait_url "${OLLAMA_URL}/api/tags" 30 || return 1
}

ensure_ollama_parallel() {
  local parallel="$1"
  [[ "${OLLAMA_PARALLEL_CUR}" == "${parallel}" ]] && return 0
  echo "    [ollama] setting OLLAMA_NUM_PARALLEL=${parallel} (container recreate)"
  if recreate_ollama "${parallel}"; then
    OLLAMA_PARALLEL_CUR="${parallel}"
    return 0
  fi
  echo "    WARNING: ollama recreate for NUM_PARALLEL=${parallel} failed" >&2
  return 1
}

ollama_resident_frac() {  # echo size_vram/size (2dp) for a loaded tag, else empty
  curl -fsS --max-time 15 "${OLLAMA_URL}/api/ps" 2>/dev/null | "${PY_BIN}" -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
t=sys.argv[1]
r=next((m for m in d.get("models",[]) if m.get("name")==t), {}) or {}
sz=r.get("size") or 0; sv=r.get("size_vram") or 0
print("%.3f"%(sv/sz) if sz else "")' "$1" 2>/dev/null || true
}

# run_probes CID BACKEND_URL API MODEL NO_MMAP : sampler + tokrate c1/c8 + power.
run_probes() {
  local cid="$1" url="$2" api="$3" model="$4" no_mmap="$5"
  local nd="${WORK}/${cid}.ndjson" pid="${WORK}/${cid}.pid"
  "${PY_BIN}" "${SCRIPT_DIR}/resource_sampler.py" start --out "${nd}" --pidfile "${pid}" --interval 1 >/dev/null 2>&1 || true
  "${PY_BIN}" "${SCRIPT_DIR}/tokrate_probe.py" \
    --backend-url "${url}" --api "${api}" --model "${model}" \
    --max-tokens "${MAX_TOKENS}" --prompt-tokens "${PROMPT_TOKENS}" \
    --runs "${RUNS_C1}" --concurrency 1 --warmup 1 --label "${cid}-c1" \
    --out "${WORK}/${cid}.c1.json" >/dev/null 2>&1 || true
  "${PY_BIN}" "${SCRIPT_DIR}/tokrate_probe.py" \
    --backend-url "${url}" --api "${api}" --model "${model}" \
    --max-tokens "${MAX_TOKENS}" --prompt-tokens "${PROMPT_TOKENS}" \
    --runs 1 --concurrency "${CONCURRENCY_HI}" --warmup 0 --label "${cid}-c8" \
    --out "${WORK}/${cid}.c8.json" >/dev/null 2>&1 || true
  "${PY_BIN}" "${SCRIPT_DIR}/resource_sampler.py" stop --pidfile "${pid}" --in "${nd}" \
    --out "${WORK}/${cid}.res.json" >/dev/null 2>&1 || true
  run_power "${cid}" "${url}" "${api}" "${model}" "${no_mmap}"
}

# run_power CID URL API MODEL NO_MMAP : sustained decode through the power sampler.
# Built against the power_sampler.py CLI CONTRACT:
#   power_sampler.py --api {ollama|openai} --backend-url URL --model NAME \
#       --max-tokens N [--no-mmap] --runs 1 --out FILE.json
# In SELFTEST we stub a contract-shaped JSON (no rocm-smi / no real decode).
run_power() {
  local cid="$1" url="$2" api="$3" model="$4" no_mmap="$5"
  local out="${WORK}/${cid}.power.json"
  if [[ "${SELFTEST}" == "1" ]]; then
    cat >"${out}" <<JSON
{"api":"${api}","model":"${model}","idle_w":10.0,"load_w_mean":100.0,"load_w_peak":110.0,"decode_tps":36.5,"tok_per_watt_load":0.365,"tok_per_watt_net_idle":0.405}
JSON
    return 0
  fi
  local mmap_flag=(); [[ "${no_mmap}" == "1" ]] && mmap_flag=(--no-mmap)
  if ! "${PY_BIN}" "${SCRIPT_DIR}/power_sampler.py" --api "${api}" --backend-url "${url}" \
        --model "${model}" --max-tokens "${POWER_MAX_TOKENS}" ${mmap_flag[@]+"${mmap_flag[@]}"} \
        --runs 1 --out "${out}" >"${LOG_DIR}/${cid}-power.log" 2>&1; then
    echo "    (power sampler unavailable/failed for ${cid}; recorded null -- see logs)"
    rm -f "${out}" 2>/dev/null || true
  fi
}

# =========================================================================== #
# ollama cells
# =========================================================================== #
ollama_cell() {  # residency parallel
  local residency="$1" parallel="$2"
  local cid tag no_mmap
  cid="$("${PY_BIN}" -c "import bestcfg_matrix as b;print(b.cell_id('ollama','${residency}',${parallel}))" 2>/dev/null || echo "ollama-${residency}-p${parallel}")"
  if [[ "${residency}" == "resident" ]]; then tag="${OLLAMA_RESIDENT_TAG}"; no_mmap=1; else tag="${OLLAMA_AUTO_TAG}"; no_mmap=0; fi
  echo "==> [bestcfg-matrix] cell ${cid} (tag=${tag})"

  if [[ "${SELFTEST}" != "1" ]]; then
    if ! ensure_ollama_parallel "${parallel}"; then
      write_load_meta "${cid}" ollama "${residency}" "${parallel}" "${tag}" load-fail "ollama NUM_PARALLEL=${parallel} recreate failed"
      return 0
    fi
    if ! wait_url "${OLLAMA_URL}/api/tags" 30; then
      write_load_meta "${cid}" ollama "${residency}" "${parallel}" "${tag}" load-fail "ollama not ready at ${OLLAMA_URL}"
      return 0
    fi
    if [[ "${residency}" == "resident" ]] && ! curl -fsS "${OLLAMA_URL}/api/tags" 2>/dev/null | grep -q "\"${tag}\""; then
      echo "    ${OLLAMA_RESIDENT_TAG} not present; attempting make-vram-resident-models.sh (create only)"
      TAGS="${OLLAMA_AUTO_TAG}" SUFFIX="-vram" VERIFY=0 bash "${SCRIPT_DIR}/make-vram-resident-models.sh" >"${LOG_DIR}/${cid}-mkvram.log" 2>&1 || true
    fi
    # Warm-load the model (loads weights into the carveout).
    if ! curl -fsS --max-time 900 "${OLLAMA_URL}/api/generate" \
         -d "{\"model\":\"${tag}\",\"prompt\":\"ok\",\"stream\":false,\"options\":{\"num_predict\":1}}" >/dev/null 2>&1; then
      write_load_meta "${cid}" ollama "${residency}" "${parallel}" "${tag}" load-fail "warm load failed (model missing / OOM)"
      return 0
    fi
  fi

  local frac=""; frac="$(ollama_resident_frac "${tag}")"
  run_probes "${cid}" "${OLLAMA_URL}" ollama "${tag}" "${no_mmap}"
  write_load_meta "${cid}" ollama "${residency}" "${parallel}" "${tag}" ok "" "${frac:-null}"
  # Unload so the next cell starts from a clean carveout (best effort).
  if [[ "${SELFTEST}" != "1" ]]; then
    curl -fsS --max-time 30 "${OLLAMA_URL}/api/generate" -d "{\"model\":\"${tag}\",\"keep_alive\":0}" >/dev/null 2>&1 || true
  fi
}

# =========================================================================== #
# llamacpp cells (with the load/health PROBE first)
# =========================================================================== #
LLAMACPP_OK=0
LLAMACPP_ACTIVE_HF=""
LLAMACPP_PARITY="parity"

llamacpp_up() {  # ngl parallel hf [tries] : (re)create llama-server; wait for /health
  local ngl="$1" parallel="$2" hf="$3" tries="${4:-150}"
  docker rm -f "${LLAMACPP_CONTAINER}" >/dev/null 2>&1 || true
  # -e LLAMA_CACHE + the persistent cache volume => the -hf GGUF is downloaded ONCE
  # and reused across the probe and every cell (see LLAMACPP_CACHE_VOLUME). The probe
  # passes the generous LLAMACPP_PROBE_TRIES so the first ~60 GiB download can finish;
  # warm cell bring-ups fall back to the shorter default wait.
  docker run -d --name "${LLAMACPP_CONTAINER}" --network "${NETWORK}" --restart unless-stopped \
    -p "${LLAMACPP_PORT}:8080" --device=/dev/kfd --device=/dev/dri --group-add=video \
    --cap-add=SYS_PTRACE --security-opt seccomp=unconfined -e HSA_OVERRIDE_GFX_VERSION=11.5.1 \
    -e LLAMA_CACHE=/root/.cache/llama.cpp \
    -v "${LLAMACPP_CACHE_VOLUME}:/root/.cache/llama.cpp" \
    "${LLAMACPP_IMAGE}" -hf "${hf}" --host 0.0.0.0 --port 8080 \
    -ngl "${ngl}" --parallel "${parallel}" --jinja --no-mmap \
    >"${LOG_DIR}/llamacpp-up.log" 2>&1 || return 1
  wait_url "${LLAMACPP_READY_URL}" "${tries}" || return 1
}

# Capture llama.cpp failure evidence into ONE log so the NEXT run can tell the real
# cause apart: download/disk-fail (see df -h /) vs `invalid device function` (gfx1151
# MXFP4 kernel gap) vs OOM (see container logs). Never swallowed; best effort.
llamacpp_diag() {  # logfile
  local log="$1"
  {
    echo "===== $(date -u +%FT%TZ) docker logs --tail 400 ${LLAMACPP_CONTAINER} ====="
    docker logs --tail 400 "${LLAMACPP_CONTAINER}" 2>&1 || echo "(container/logs unavailable)"
    echo "===== df -h / (disk at failure time) ====="
    df -h / 2>&1 || true
    echo "===== docker ps -a (llama) ====="
    docker ps -a 2>&1 | grep -i llama || echo "(no llama containers)"
  } >"${log}" 2>&1 || true
}

llamacpp_probe() {  # decide if llama.cpp can serve the flagship GGUF at all
  if [[ "${SELFTEST}" == "1" ]]; then LLAMACPP_OK=1; LLAMACPP_ACTIVE_HF="${LLAMACPP_HF}"; return 0; fi
  echo "==> [bestcfg-matrix] llama.cpp load/health probe: ${LLAMACPP_HF} (-ngl 999, gfx1151)"
  echo "    (first call also downloads the ~60 GiB GGUF into volume '${LLAMACPP_CACHE_VOLUME}'; waiting up to ${LLAMACPP_PROBE_TRIES} tries ~$((LLAMACPP_PROBE_TRIES * 2 / 60)) min)"
  if llamacpp_up 999 1 "${LLAMACPP_HF}" "${LLAMACPP_PROBE_TRIES}"; then
    LLAMACPP_OK=1; LLAMACPP_ACTIVE_HF="${LLAMACPP_HF}"; LLAMACPP_PARITY="parity"
    echo "    llama.cpp served ${LLAMACPP_HF} OK (GGUF now cached; cells reuse it)"
    return 0
  fi
  echo "    llama.cpp could NOT load ${LLAMACPP_HF} on gfx1151 (see ${LOG_DIR}/llamacpp-probe.log)"
  llamacpp_diag "${LOG_DIR}/llamacpp-probe.log"
  if [[ "${LLAMACPP_ALLOW_FALLBACK}" == "1" ]]; then
    echo "==> [bestcfg-matrix] llama.cpp fallback probe: ${LLAMACPP_FALLBACK_HF} (NON-PARITY)"
    if llamacpp_up 999 1 "${LLAMACPP_FALLBACK_HF}" "${LLAMACPP_PROBE_TRIES}"; then
      LLAMACPP_OK=1; LLAMACPP_ACTIVE_HF="${LLAMACPP_FALLBACK_HF}"; LLAMACPP_PARITY="fallback-non-parity"
      echo "    fallback GGUF served OK (clearly labeled non-parity vs gpt-oss:120b)"
      return 0
    fi
    echo "    fallback GGUF also failed (see ${LOG_DIR}/llamacpp-probe-fallback.log)"
    llamacpp_diag "${LOG_DIR}/llamacpp-probe-fallback.log"
  fi
  docker rm -f "${LLAMACPP_CONTAINER}" >/dev/null 2>&1 || true
  LLAMACPP_OK=0
  return 0
}

llamacpp_cell() {  # residency parallel
  local residency="$1" parallel="$2" cid ngl reason
  cid="$("${PY_BIN}" -c "import bestcfg_matrix as b;print(b.cell_id('llamacpp','${residency}',${parallel}))" 2>/dev/null || echo "llamacpp-${residency}-p${parallel}")"
  if [[ "${residency}" == "resident" ]]; then ngl=999; else ngl="${LLAMACPP_NGL_AUTO}"; fi
  local model="${LLAMACPP_MODEL}"
  [[ "${LLAMACPP_PARITY}" != "parity" ]] && model="${LLAMACPP_MODEL} (${LLAMACPP_PARITY}:${LLAMACPP_ACTIVE_HF})"

  if [[ "${LLAMACPP_OK}" != "1" ]]; then
    reason="llama.cpp cannot serve ${LLAMACPP_HF} on gfx1151/ROCm (MXFP4 120B load probe failed)"
    write_load_meta "${cid}" llamacpp "${residency}" "${parallel}" "${model}" skipped "${reason}"
    echo "    SKIP ${cid}: ${reason}"
    return 0
  fi
  echo "==> [bestcfg-matrix] cell ${cid} (-ngl ${ngl} --parallel ${parallel}, ${LLAMACPP_PARITY})"
  if [[ "${SELFTEST}" != "1" ]]; then
    if ! llamacpp_up "${ngl}" "${parallel}" "${LLAMACPP_ACTIVE_HF}"; then
      write_load_meta "${cid}" llamacpp "${residency}" "${parallel}" "${model}" load-fail "llama-server did not become healthy (-ngl ${ngl} --parallel ${parallel})"
      llamacpp_diag "${LOG_DIR}/${cid}.log"
      return 0
    fi
  fi
  run_probes "${cid}" "${LLAMACPP_DIRECT_URL}" openai "${model}" 0
  write_load_meta "${cid}" llamacpp "${residency}" "${parallel}" "${model}" ok ""
}

# =========================================================================== #
# run the matrix (server -> parallel -> residency: minimizes ollama recreates)
# =========================================================================== #
want_cell() {  # cid -> 0 if it should run (per CELLS filter)
  [[ -z "${CELLS:-}" ]] && return 0
  [[ " ${CELLS} " == *" $1 "* ]] && return 0 || return 1
}

for server in ${SERVERS}; do
  case "${server}" in
    ollama)
      for parallel in 1 "${CONCURRENCY_HI}"; do
        for residency in resident auto; do
          cid="ollama-${residency}-p${parallel}"
          want_cell "${cid}" || continue
          ollama_cell "${residency}" "${parallel}"
        done
      done
      ;;
    llamacpp)
      llamacpp_probe
      for parallel in 1 "${CONCURRENCY_HI}"; do
        for residency in resident auto; do
          cid="llamacpp-${residency}-p${parallel}"
          want_cell "${cid}" || continue
          llamacpp_cell "${residency}" "${parallel}"
        done
      done
      # NOTE: the llama-server teardown is intentionally DEFERRED to AFTER the cache
      # overlay phase (see "Deferred llama-server teardown" below), NOT here. If
      # llamacpp is a per-server winner, the overlay repoints the router at its live
      # backend to record the cache numbers -- tearing it down here (the old bug)
      # left the overlay measuring against an absent backend (footnote 7 in §11.4).
      ;;
    *) echo "    (unknown server '${server}' -- skipped)" ;;
  esac
done

# =========================================================================== #
# first assemble -> learn per-server winners -> cache overlay -> reassemble
# =========================================================================== #
assemble() {
  "${PY_BIN}" "${SCRIPT_DIR}/bestcfg_matrix.py" assemble --work "${WORK}" --out "${OUT}" \
    --box "${BOX}" --model-flagship "${MODEL_FLAGSHIP}" --oom-min-tps "${OOM_MIN_TPS}" \
    --ttft-gate-ms "${TTFT_GATE_MS}" --max-tokens "${MAX_TOKENS}" \
    --prompt-tokens "${PROMPT_TOKENS}" --concurrency-hi "${CONCURRENCY_HI}"
}

echo "==> [bestcfg-matrix] assembling (pre-overlay) ${OUT}"
assemble >/dev/null

if [[ "${CACHE_OVERLAY}" == "1" ]]; then
  if [[ "${SELFTEST}" != "1" ]] && ! curl -fsS "${ROUTER_CONFIG_URL}" >/dev/null 2>&1; then
    echo "    (cache overlay skipped: router not answering at ${ROUTER_CONFIG_URL})"
  elif [[ "${SELFTEST}" != "1" && ! -f "${GATEWAY_CONFIG}" ]]; then
    echo "    (cache overlay skipped: no gateway config at ${GATEWAY_CONFIG})"
  else
    reload_timeout=45; reload_settle=3
    [[ "${SELFTEST}" == "1" ]] && { reload_timeout=0; reload_settle=0; }
    for server in ${SERVERS}; do
      win_cid="$("${PY_BIN}" -c "import json;d=json.load(open('${OUT}'));print((d.get('scoring',{}).get('per_server_winner',{}).get('${server}',{}) or {}).get('cell_id') or '')" 2>/dev/null || echo "")"
      [[ -z "${win_cid}" ]] && { echo "    (cache overlay: no winning ${server} cell -- skipped)"; continue; }
      echo "==> [bestcfg-matrix] cache overlay: ${server} winner ${win_cid} (repoint router)"
      # Repoint the router alias at this server's winning backend, then measure.
      if [[ "${SELFTEST}" != "1" ]]; then
        cp -f "${GATEWAY_CONFIG}" "${WORK}/${server}-config.bak"
        endpoint="${OLLAMA_CONTAINER}:${OLLAMA_PORT}"; rmodel="${OLLAMA_AUTO_TAG}"
        [[ "${server}" == "llamacpp" ]] && { endpoint="${LLAMACPP_ROUTER_NET}"; rmodel="${LLAMACPP_MODEL}"; }
        "${PY_BIN}" "${SCRIPT_DIR}/repoint_backend.py" --config "${GATEWAY_CONFIG}" \
          --alias "${ROUTER_ALIAS}" --endpoint "${endpoint}" --model "${rmodel}" >/dev/null 2>&1 \
          || echo "    (repoint failed for ${server}; overlay uses current routing)"
        wait_url "${ROUTER_CONFIG_URL}" 30 || true; sleep 3
      fi
      "${PY_BIN}" "${SCRIPT_DIR}/bestcfg_matrix.py" cache-overlay \
        --router-url "${ROUTER_URL}" --server "${server}" --cell-id "${win_cid}" \
        --config "${GATEWAY_CONFIG}" --container "${ROUTER_CONTAINER}" \
        --threshold "${CACHE_THRESHOLD}" --reload-timeout "${reload_timeout}" \
        --reload-settle "${reload_settle}" --out "${WORK}/cache-${server}.json" \
        >"${LOG_DIR}/cache-${server}.log" 2>&1 || echo "    (cache overlay failed for ${server})"
      if [[ "${SELFTEST}" != "1" && -f "${WORK}/${server}-config.bak" ]]; then
        cp -f "${WORK}/${server}-config.bak" "${GATEWAY_CONFIG}"; sleep 2   # restore routing
      fi
    done
  fi
fi

# Deferred llama-server teardown (moved here from the llamacpp server loop above):
# the container is kept up THROUGH the cache-overlay phase so that if llamacpp is a
# per-server winner, the overlay could repoint the router at a live backend and
# actually record its cache numbers (fixes the §11.4 footnote-7 gap, where the old
# post-cells teardown left the overlay measuring an absent backend). Torn down now,
# after the overlay -- same effect as the original cleanup, just later. Guarded so an
# ollama-only run (no llamacpp in SERVERS) and SELFTEST never touch the container.
if [[ "${SELFTEST}" != "1" && " ${SERVERS} " == *" llamacpp "* ]]; then
  docker rm -f "${LLAMACPP_CONTAINER}" >/dev/null 2>&1 || true
fi

echo "==> [bestcfg-matrix] assembling final rollup ${OUT}"
assemble

echo "==> [bestcfg-matrix] done -> ${OUT}"
