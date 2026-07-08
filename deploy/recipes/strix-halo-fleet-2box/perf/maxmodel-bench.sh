#!/usr/bin/env bash
#
# maxmodel-bench.sh -- "max usable model" probe with NEAR-FULL FALLBACK to Halo-B.
#
# The oversized/max-model sweep is the memory-hungriest test: on a box whose
# unified memory is already near full it just OOMs. So for each big model this
# script ESTIMATES the footprint, and if loading it would push Halo-A's unified
# memory past NEARFULL_PCT it OFFLOADS that model's probe to Halo-B over SSH
# (auto-pulling it there). Small models are never touched -- they stay on Halo-A
# (measured by overhead-bench). If Halo-B is unreachable it records a clear
# skip-with-reason instead of failing.
#
# Footprint estimate: est_gib ~= params_billions * QUANT_GIB_PER_B.
#   Q4 ~= 0.6 GiB / 1B params (validated: 70B*0.6 = 42 GiB ~ the 42 GB GTT the 70B
#   load-fail hit on Halo-A), Q8 ~= 1.1, fp16 ~= 2.2.
#
# Env (all optional):
#   MAXMODEL_TAGS     space list of big Ollama tags   (default "qwen2.5:32b llama3.1:70b")
#   NEARFULL_PCT      offload threshold, % unified     (default 85)
#   QUANT_GIB_PER_B   GiB per 1B params for the quant  (default 0.6 = Q4)
#   OLLAMA_URL        local backend base               (default http://localhost:11434)
#   OLLAMA_CONTAINER  local ollama container name      (default ollama)
#   PROBE_TOKENS      decode length for the probe      (default 64)
#   BOX               local box label                  (default hostname)
#   OUT               output JSON                       (default maxmodel-<box>.json here)
#   HALO_B_SSH / HALO_B_REPO / HALO_B_SSH_KEY / HALO_B_SSH_PORT / HALO_B_BOX  (from env/fleet.env)
#
# Usage:  bash maxmodel-bench.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=/dev/null
source "${RECIPE_DIR}/fleet_common.sh"
PY_BIN="$(fleet_pybin)"

# Pull SSH env from the deploy's fleet.env if present (same pattern as run-perf-fleet).
ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
[ -f "${ENV_FILE}" ] && . "${ENV_FILE}"

MAXMODEL_TAGS="${MAXMODEL_TAGS:-qwen2.5:32b llama3.1:70b}"
NEARFULL_PCT="${NEARFULL_PCT:-85}"
QUANT_GIB_PER_B="${QUANT_GIB_PER_B:-0.6}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-ollama}"
PROBE_TOKENS="${PROBE_TOKENS:-64}"
BOX="${BOX:-$(hostname 2>/dev/null || echo box)}"
HALO_B_BOX="${HALO_B_BOX:-halo-b}"
OUT="${OUT:-${SCRIPT_DIR}/maxmodel-${BOX}.json}"
WORK="$(mktemp -d "${FLEET_STATE_DIR}/maxmodel-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# --- unified-memory snapshot (total + currently used) ---------------------- #
read_mem_gib() {  # echoes "TOTAL_GIB USED_GIB"
  "${PY_BIN}" - <<'PYEOF'
tot = avail = None
try:
    for l in open("/proc/meminfo"):
        if l.startswith("MemTotal:"):
            tot = int(l.split()[1]) * 1024
        elif l.startswith("MemAvailable:"):
            avail = int(l.split()[1]) * 1024
except Exception:
    pass
print("%.2f %.2f" % ((tot or 0) / 1024**3, ((tot - avail) if (tot and avail is not None) else 0) / 1024**3))
PYEOF
}
read -r TOTAL_GIB USED_GIB <<<"$(read_mem_gib)"
echo "==> [maxmodel] box=${BOX} unified=${TOTAL_GIB}GiB used=${USED_GIB}GiB nearfull=${NEARFULL_PCT}%"

# --- helpers --------------------------------------------------------------- #
params_of() { echo "$1" | grep -oiE '[0-9]+b' | head -n1 | tr -dc '0-9'; }
decode_of() {
  "${PY_BIN}" -c "import json;d=json.load(open(r'$1'));a=d.get('aggregate',{});print(a.get('aggregate_decode_tps') or '')" 2>/dev/null || true
}

probe_local() {  # tag safe -> echoes decode_tps or ""
  local tag="$1" safe="$2"
  docker exec "${OLLAMA_CONTAINER}" ollama pull "${tag}" >/dev/null 2>&1 || true
  "${PY_BIN}" "${SCRIPT_DIR}/tokrate_probe.py" --backend-url "${OLLAMA_URL}" --api ollama \
    --model "${tag}" --runs 1 --max-tokens "${PROBE_TOKENS}" --warmup 0 \
    --out "${WORK}/${safe}.json" >/dev/null 2>&1 || true
  [ -f "${WORK}/${safe}.json" ] && decode_of "${WORK}/${safe}.json"
}

probe_halo_b() {  # tag safe -> echoes decode_tps or "" ; return 1 unreachable, 2 not-configured
  local tag="$1" safe="$2"
  [ -n "${HALO_B_SSH:-}" ] || return 2
  local repo="${HALO_B_REPO:-}"; [ -n "${repo}" ] || return 2
  local perf="${repo}/deploy/recipes/strix-halo-fleet-2box/perf"
  local rtmp="\${TMPDIR:-/tmp}/vllm-sr-maxmodel"
  local OPTS=(); [ -n "${HALO_B_SSH_KEY:-}" ] && OPTS+=(-i "${HALO_B_SSH_KEY}")
  [ -n "${HALO_B_SSH_PORT:-}" ] && OPTS+=(-p "${HALO_B_SSH_PORT}")
  ssh -o ConnectTimeout=10 "${OPTS[@]}" "${HALO_B_SSH}" "bash -lc '\
    set -e; mkdir -p ${rtmp}; \
    docker exec ${OLLAMA_CONTAINER} ollama pull ${tag} >/dev/null 2>&1 || true; \
    python3 ${perf}/tokrate_probe.py --backend-url ${OLLAMA_URL} --api ollama --model ${tag} \
      --runs 1 --max-tokens ${PROBE_TOKENS} --warmup 0 --out ${rtmp}/${safe}.json >/dev/null 2>&1 || true'" >/dev/null 2>&1 || return 1
  scp -o ConnectTimeout=10 "${OPTS[@]}" "${HALO_B_SSH}:${rtmp//\\/}/${safe}.json" "${WORK}/${safe}.json" 2>/dev/null || return 1
  [ -f "${WORK}/${safe}.json" ] && decode_of "${WORK}/${safe}.json"
}

# --- per-tag decision + probe ---------------------------------------------- #
RESULTS="["
first=1
for tag in ${MAXMODEL_TAGS}; do
  safe="$(echo "${tag}" | tr ':/.' '___')"
  pb="$(params_of "${tag}")"; pb="${pb:-0}"
  est_gib="$("${PY_BIN}" -c "print('%.1f' % (${pb} * ${QUANT_GIB_PER_B}))")"
  proj_pct="$("${PY_BIN}" -c "t=${TOTAL_GIB} or 1; print('%.1f' % ((${USED_GIB} + ${est_gib}) / t * 100))")"
  over="$("${PY_BIN}" -c "print(1 if ${proj_pct} > ${NEARFULL_PCT} else 0)")"

  chosen="${BOX}"; tps=""; verdict="usable"; reason=""
  if [ "${over}" = "1" ]; then
    echo "==> [maxmodel] ${tag}: est ${est_gib}GiB -> projected ${proj_pct}% > ${NEARFULL_PCT}% -> OFFLOAD to ${HALO_B_BOX}"
    chosen="${HALO_B_BOX}"
    if tps="$(probe_halo_b "${tag}" "${safe}")"; then
      [ -n "${tps}" ] && verdict="usable" || { verdict="load-fail"; reason="halo-b load returned no tokens"; }
    else
      rc=$?
      verdict="skipped"
      reason="halo-a near-full (${proj_pct}%) AND halo-b $([ "$rc" = 2 ] && echo not-configured || echo unreachable)"
      echo "    SKIP ${tag}: ${reason}"
    fi
  else
    echo "==> [maxmodel] ${tag}: est ${est_gib}GiB -> projected ${proj_pct}% <= ${NEARFULL_PCT}% -> probe LOCAL (${BOX})"
    tps="$(probe_local "${tag}" "${safe}")"
    [ -n "${tps}" ] || { verdict="load-fail"; reason="local load-fail or OOM"; }
  fi

  tps_json="null"; [ -n "${tps}" ] && tps_json="${tps}"
  [ "${first}" = 1 ] || RESULTS="${RESULTS},"
  first=0
  RESULTS="${RESULTS}
    {\"tag\":\"${tag}\",\"params_b\":${pb},\"est_footprint_gib\":${est_gib},\"projected_pct\":${proj_pct},\"chosen_box\":\"${chosen}\",\"decode_tps\":${tps_json},\"verdict\":\"${verdict}\",\"reason\":\"${reason}\"}"
done
RESULTS="${RESULTS}
  ]"

cat >"${OUT}" <<JSON
{
  "schema": "maxmodel-bench/v1",
  "box": "${BOX}",
  "unified_mem_gib": ${TOTAL_GIB},
  "used_mem_gib": ${USED_GIB},
  "nearfull_pct": ${NEARFULL_PCT},
  "quant_gib_per_b": ${QUANT_GIB_PER_B},
  "results": ${RESULTS}
}
JSON

echo "==> [maxmodel] wrote ${OUT}"
"${PY_BIN}" -c "import json;print(json.dumps(json.load(open(r'${OUT}')),indent=2))" 2>/dev/null || cat "${OUT}"
