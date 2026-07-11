#!/usr/bin/env bash
#
# collect-report-data.sh -- ONE-SHOT report-data collection for the Strix Halo
# perf report. Run this single command on Halo-A and, at the end, you get one
# bundle containing every number the report needs plus a stitched `report-data.md`.
#
# It runs, in order (each step is individually skippable):
#   [1] offline verifier (gate)      verify_perf_local.py        -> 7/7 or abort
#   [2] install Lemonade (idempotent) install-lemonade.sh        -> so Test 2 measures it
#   [3] Test 1 + Test 2 (+Halo-B)    run-perf-fleet.sh           -> overhead-*/server-*/
#                                                                   perf-metrics.json/perf-summary.md
#   [4] ensure the vllm-sr stack UP  gateway-bring-up.sh (defensive)
#   [5] concurrency sweep            tokrate_probe.py {1..16}    -> conc-c*-<box>.json
#   [6] semantic-cache sweep         cache-sweep.sh              -> cache-sweep-<box>.csv
#   [7] finalize                     -> report-data.md (perf-summary + concurrency + cache tables)
#
# Steps [2]->[3] order matters: Lemonade must be installed BEFORE server-bench so
# the Lemonade leg is measured instead of skipped. Halo-B (step 6 in the manual
# runbook) is folded into [3] -- set HALO_B_PERF=1 and it is measured over SSH.
#
# Env (all optional):
#   RUN_SELFTEST    1 => run [1] and abort on failure   (default 1)
#   DO_LEMONADE     1 => run [2]                         (default 1)
#   DO_CONCURRENCY  1 => run [5]                         (default 1)
#   DO_CACHE        1 => run [6]                         (default 1)
#   CONC_MODEL      model for the concurrency sweep      (default qwen2.5:7b)
#   CONC_LEVELS     concurrency levels                   (default "1 2 4 8 16")
#   OLLAMA_URL      direct backend base                  (default http://localhost:11434)
#   BUNDLE          output dir                           (default report-run-<ts> under fleet state)
#   BOX             box label                            (default hostname)
#   HALO_B_PERF / HALO_B_SSH / HALO_B_REPO   forwarded to run-perf-fleet.sh
#   plus any overhead-bench/server-bench env (TIERS, SERVERS, OVERSIZED_TAGS, RUNS, ...)
#
# Usage (on Halo-A; stack may be up or down):
#   bash perf/collect-report-data.sh
#   HALO_B_PERF=1 HALO_B_SSH=test001@10.96.28.126 HALO_B_REPO=~/yy/workspace/semantic-router \
#     bash perf/collect-report-data.sh
set -uo pipefail   # NOT -e: always reach the finalize step

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=/dev/null
source "${RECIPE_DIR}/fleet_common.sh"
PY_BIN="$(fleet_pybin)"

# Make user-installed CLIs (lemonade-server) resolvable in this non-login shell.
_user_base_bin="$("${PY_BIN}" -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))' 2>/dev/null || true)"
export PATH="${HOME}/.local/bin${_user_base_bin:+:${_user_base_bin}}:${PATH}"

RUN_SELFTEST="${RUN_SELFTEST:-1}"
DO_LEMONADE="${DO_LEMONADE:-1}"
DO_CONCURRENCY="${DO_CONCURRENCY:-1}"
DO_CACHE="${DO_CACHE:-1}"
CONC_MODEL="${CONC_MODEL:-qwen2.5:7b}"
CONC_LEVELS="${CONC_LEVELS:-1 2 4 8 16}"
# Lemonade model for Test 2: Qwen3-8B-GGUF is a valid GPU GGUF in the registry
# (Qwen2.5-7B-Instruct-GGUF is NOT). Exported so run-perf-fleet -> server-bench
# serves the same model that step [2] pre-pulls.
LEMONADE_MODEL="${LEMONADE_MODEL:-Qwen3-8B-GGUF}"; export LEMONADE_MODEL
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
ROUTER_CONFIG_URL="${ROUTER_CONFIG_URL:-http://localhost:8080/config/hash}"
BOX="${BOX:-$(hostname 2>/dev/null || echo box)}"
BUNDLE="${BUNDLE:-${FLEET_STATE_DIR}/report-run-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "${BUNDLE}"

CURRENT_STEP="init"
log() {
  echo "==> [collect] $*"
  # Remember the last step marker ("[N/7]") so an interrupted run records where
  # it stopped (see the interrupted-run guard below).
  case "${1:-}" in
    "["*) CURRENT_STEP="${1%% *}" ;;
  esac
}
log "bundle=${BUNDLE}  box=${BOX}"

# --- interrupted-run guard --------------------------------------------------
# A crash / kill / SSH-hang used to leave a directory of 0-byte files that
# looked exactly like a real bundle (e.g. report-run-20260709-235809: 10 files,
# all empty). Stamp a run-status marker (RUNNING -> COMPLETE) and, on any early
# exit or signal, prune the 0-byte shells and flip the marker to INTERRUPTED so
# a partial run is unmistakable. Best-effort throughout (|| true): consistent
# with this script's fail-soft `set -uo pipefail` (no -e) style -- the guard
# must never itself abort the run.
STATUS_FILE="${BUNDLE}/run-status.txt"
RUN_COMPLETE=0
_mark_status() { printf '%s\n' "$*" >"${STATUS_FILE}" 2>/dev/null || true; }
_prune_empty() {
  find "${BUNDLE}" -maxdepth 1 -type f -empty ! -name "$(basename "${STATUS_FILE}")" \
    -delete 2>/dev/null || true
}
_on_exit() {
  local rc=$?
  [ "${RUN_COMPLETE}" = "1" ] && return 0
  _prune_empty
  _mark_status "INTERRUPTED step=${CURRENT_STEP} rc=${rc} at=$(date +%Y%m%d-%H%M%S)"
}
trap _on_exit EXIT INT TERM
_mark_status "RUNNING step=${CURRENT_STEP} started=$(date +%Y%m%d-%H%M%S)"

# [1] Offline verifier -- prove the harness before touching the box.
if [ "${RUN_SELFTEST}" = "1" ]; then
  log "[1/7] offline verifier (verify_perf_local.py)"
  if ! "${PY_BIN}" "${SCRIPT_DIR}/verify_perf_local.py"; then
    echo "ERROR: offline verifier failed; aborting before any hardware run." >&2
    exit 1
  fi
fi

# [2] Install Lemonade FIRST so the Test 2 Lemonade leg is measured, not skipped.
if [ "${DO_LEMONADE}" = "1" ]; then
  log "[2/7] install Lemonade (idempotent) + pre-pull ${LEMONADE_MODEL}"
  PULL_MODEL="${LEMONADE_MODEL}" bash "${SCRIPT_DIR}/install-lemonade.sh" \
    || log "    (lemonade install returned nonzero; Test 2 will just skip it -- continuing)"
fi

# [3] Test 1 (overhead) + Test 2 (servers), fleet-wide (+Halo-B if HALO_B_PERF=1),
#     aggregated into perf-metrics.json + perf-summary.md. Leaves the stack UP.
log "[3/7] Test 1 + Test 2 (fleet) -> ${BUNDLE}"
BUNDLE="${BUNDLE}" bash "${SCRIPT_DIR}/run-perf-fleet.sh" \
  || log "    (run-perf-fleet returned nonzero; continuing)"

# [3.5] Max-model probe: estimate each big model's footprint and, if Halo-A would
#       exceed NEARFULL_PCT of unified memory, OFFLOAD that probe to Halo-B (skip
#       with reason if Halo-B is unreachable). Small tiers stay on Halo-A (step 3).
log "[3.5/7] max-model probe (near-full -> Halo-B fallback)"
BOX="${BOX}" OUT="${BUNDLE}/maxmodel-${BOX}.json" \
  bash "${SCRIPT_DIR}/maxmodel-bench.sh" \
  || log "    (maxmodel-bench returned nonzero; continuing)"

# [4] Defensive: cache-sweep + concurrency need the backend/router up.
log "[4/7] ensuring the vllm-sr stack is UP"
if ! curl -fsS "${ROUTER_CONFIG_URL}" >/dev/null 2>&1; then
  bash "${RECIPE_DIR}/gateway-bring-up.sh" \
    || log "    (gateway-bring-up returned nonzero; cache-sweep may skip)"
fi

# [5] Concurrency sweep -- one JSON per level (tokrate_probe prints pretty JSON,
#     so write each to its own file rather than appending to a .jsonl).
if [ "${DO_CONCURRENCY}" = "1" ]; then
  log "[5/7] concurrency sweep (${CONC_MODEL}) levels: ${CONC_LEVELS}"
  for c in ${CONC_LEVELS}; do
    "${PY_BIN}" "${SCRIPT_DIR}/tokrate_probe.py" \
      --backend-url "${OLLAMA_URL}" --api ollama --model "${CONC_MODEL}" \
      --concurrency "${c}" --runs 1 --max-tokens 128 --label "c${c}" \
      --out "${BUNDLE}/conc-c${c}-${BOX}.json" >/dev/null 2>&1 \
      || log "    (concurrency c=${c} failed)"
  done
fi

# [6] Semantic-cache threshold sweep -> CSV in the bundle (restores config after).
if [ "${DO_CACHE}" = "1" ]; then
  log "[6/7] semantic-cache sweep"
  OUT="${BUNDLE}/cache-sweep-${BOX}.csv" RESTORE=1 BOX="${BOX}" \
    bash "${SCRIPT_DIR}/cache-sweep.sh" \
    || log "    (cache-sweep returned nonzero; skipping)"
fi

# [7] Finalize: re-aggregate, then stitch every table into one report-data.md.
log "[7/7] finalize -> ${BUNDLE}/report-data.md"
"${PY_BIN}" "${SCRIPT_DIR}/perf_metrics.py" --bundle "${BUNDLE}" >/dev/null 2>&1 || true
"${PY_BIN}" - "${BUNDLE}" "${BOX}" <<'PYEOF'
import glob, json, os, sys

BUNDLE, BOX = sys.argv[1], sys.argv[2]
out = ["# Strix Halo perf — collected report data (%s)" % BOX, ""]

# Test 1 + Test 2 aggregate (already markdown, with the TTFT columns).
ps = os.path.join(BUNDLE, "perf-summary.md")
if os.path.exists(ps):
    out.append(open(ps, encoding="utf-8").read().rstrip())
    out.append("")

# Concurrency sweep -> table (one JSON per level).
rows, model = [], ""
for p in sorted(glob.glob(os.path.join(BUNDLE, "conc-c*-*.json"))):
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        continue
    a = d.get("aggregate", {}) or {}
    model = d.get("model", model)
    rows.append((a.get("concurrency"), a.get("aggregate_decode_tps"),
                 a.get("ttft_ms_mean"), a.get("ttft_ms_p95"), a.get("success_rate")))
if rows:
    rows.sort(key=lambda r: (r[0] if r[0] is not None else 0))
    base_tps = next((r[1] for r in rows if r[1]), None)
    out.append("## Concurrency sweep (%s)" % model)
    out.append("")
    out.append("| concurrency | aggregate decode tok/s | scaling vs c1 | TTFT mean ms | TTFT p95 ms | success |")
    out.append("|---|---|---|---|---|---|")
    for c, tps, tm, tp, sr in rows:
        scal = (tps / base_tps) if (tps and base_tps) else None
        out.append("| %s | %s | %s | %s | %s | %s |" % (
            "-" if c is None else c,
            "-" if tps is None else "%.0f" % tps,
            "-" if scal is None else "%.2fx" % scal,
            "-" if tm is None else "%.0f" % tm,
            "-" if tp is None else "%.0f" % tp,
            "-" if sr is None else "%.0f%%" % (sr * 100)))
    out.append("")
    out.append("_Ollama default serializes (single slot): aggregate throughput stays flat while TTFT "
               "grows with the queue -- concurrency queues, it does not scale. Raise OLLAMA_NUM_PARALLEL, "
               "or use llama.cpp/vLLM, for true parallel throughput._")
    out.append("")

# Semantic-cache sweep CSV -> table.
csv = os.path.join(BUNDLE, "cache-sweep-%s.csv" % BOX)
if os.path.exists(csv):
    lines = [l for l in open(csv, encoding="utf-8").read().splitlines() if l.strip()]
    if lines:
        hdr = lines[0].split(",")
        out.append("## Semantic-cache threshold sweep")
        out.append("")
        out.append("| " + " | ".join(hdr) + " |")
        out.append("|" + "---|" * len(hdr))
        for l in lines[1:]:
            out.append("| " + " | ".join(l.split(",")) + " |")
        out.append("")
        out.append("_Recommendation: the lowest threshold that keeps `false_hit_rate` at 0._")
        out.append("")

out.append("---")
out.append("Narrative + interpretation: `docs/perf-report.md` — replace its **[P]** rows with the tables above.")
with open(os.path.join(BUNDLE, "report-data.md"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
print("report-data.md written (%d concurrency rows, cache=%s)" % (len(rows), os.path.exists(csv)))
PYEOF

# Customer-facing report (feasibility boundary + cost) from the same bundle.
"${PY_BIN}" "${SCRIPT_DIR}/gen-customer-report.py" "${BUNDLE}" >/dev/null 2>&1 \
  || log "    (gen-customer-report returned nonzero; skipping)"

# Finalize: prune any 0-byte artifacts left by failed sub-steps so the bundle
# never ships empty shells, then flag the run COMPLETE for consumers. Reaching
# here means every step ran (individually skippable); the EXIT trap now no-ops.
_prune_empty
RUN_COMPLETE=1
_mark_status "COMPLETE steps=7/7 finished=$(date +%Y%m%d-%H%M%S)"

echo
echo "=============================================================="
echo "Report data bundle:  ${BUNDLE}"
ls -1 "${BUNDLE}" 2>/dev/null | sed 's/^/    /'
echo "--------------------------------------------------------------"
echo "FILLED REPORT ->      ${BUNDLE}/report-data.md"
echo "CUSTOMER REPORT ->    ${BUNDLE}/customer-report.md"
echo "Narrative template -> docs/perf-report.md (swap [P] rows for the collected tables)"
echo "=============================================================="
