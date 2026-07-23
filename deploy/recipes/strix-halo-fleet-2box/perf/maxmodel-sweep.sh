#!/usr/bin/env bash
#
# maxmodel-sweep.sh -- ascending "max model under topology" ceiling sweep, run
# LOCALLY on one Strix Halo box with the full vllm-sr stack left CO-RESIDENT.
#
# Where overhead-bench.sh A/Bs the stack up-vs-down to price the router's
# footprint, THIS script answers a different question: with the stack already up
# (router + Envoy + dashboard + Grafana + ... competing for the same unified
# LPDDR5X pool), what is the LARGEST model that still loads AND sustains a usable
# decode rate, and HOW does it fail at the boundary -- a clean VRAM fit, a GTT
# spill that thrashes, or an outright load-fail (OOM)?
#
# For each tag (ascending) it:
#   1. pulls it (idempotent; a pull failure is recorded, not fatal),
#   2. samples the unified-memory split (VRAM carveout + GTT overflow + host RAM,
#      via resource_sampler.py -> rocm-smi/amd-smi + /proc/meminfo) while
#   3. tokrate_probe.py streams a real decode, then
#   4. unloads the model so the next rung's peak reflects only itself.
#
# Verdict per rung (mirrors overhead-bench's OOM logic):
#   usable                decode_tps >= OOM_MIN_TPS
#   unusable(slow-spill)  loaded but decode_tps < OOM_MIN_TPS (GTT thrash)
#   load-fail             no tokens (OOM / would-not-load / pull failed)
# Memory mode per rung (the reliability tell): peak GTT above GTT_SPILL_GIB means
# weights spilled past the VRAM carveout into GTT overflow ("gtt-spill"), else the
# model sat entirely in the VRAM carveout ("vram-fit").
#
# The point of the enlarged-GTT + headless tuning is exactly this boundary: a
# model that fits the VRAM carveout loads cleanly; one that spills into GTT is the
# fragile part on ROCm's unified memory. This records where that line is.
#
# SAFETY: this only pulls models and sends inference requests. It never stops the
# vllm-sr stack (co-residence is the point) and unloads each model when done.
#
# Env (all optional):
#   SWEEP_TAGS     ascending Ollama tags to climb
#                  (default "qwen2.5:32b llama3.1:70b gpt-oss:120b")
#   OLLAMA_URL     direct inference-server base   (default http://localhost:11434)
#   OLLAMA_CONTAINER  ollama container name       (default ollama)
#   OOM_MIN_TPS    decode tok/s floor for "usable" (default 3)
#   GTT_SPILL_GIB  peak-GTT GiB above which a rung counts as gtt-spill (default 2)
#   MAX_TOKENS/PROMPT_TOKENS/RUNS/WARMUP  probe shape (default 96/256/2/1)
#   NUM_CTX        force ollama options.num_ctx (KV size) for EVERY rung; 0=default.
#                  Use a large value (e.g. 65536) to deliberately push a model's
#                  footprint PAST the VRAM carveout and characterize the GTT spill.
#   NUM_GPU        force ollama options.num_gpu (# layers pinned on GPU) for EVERY
#                  rung; empty=off (server auto-estimate). Set 999 to pin ALL layers
#                  on GPU so a big model stays 100% resident in the VRAM carveout
#                  instead of being CPU-offloaded (the fix for the 96 GiB-carveout
#                  "system-RAM layer budget" trap -- see docs/halo-b-maxmodel.md).
#   USE_MMAP       force ollama options.use_mmap for EVERY rung; empty=off. Set
#                  'false' (pairs with NUM_GPU=999) to load weights straight into the
#                  carveout instead of mmap'ing from disk; 'true' to send it true.
#   PULL_MODELS    1 => `ollama pull` each tag first (default 1)
#   SAMPLE_INTERVAL  resource sampler seconds       (default 2)
#   BOX            box label in the output JSON      (default: hostname)
#   OUT            output JSON path        (default maxmodel-sweep-<box>.json here)
#
# Usage (on the gateway box, stack already up):
#   bash maxmodel-sweep.sh
#   SWEEP_TAGS="qwen2.5:32b llama3.1:70b gpt-oss:120b llama3.1:70b-instruct-q8_0" \
#     bash maxmodel-sweep.sh
#   # Forced full VRAM residency (bypass the auto layer estimate; 96 GiB carveout):
#   NUM_GPU=999 USE_MMAP=false NUM_CTX=4096 \
#     SWEEP_TAGS="llama3.1:70b-instruct-q8_0 qwen2.5:72b-instruct-q8_0" \
#     bash maxmodel-sweep.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=/dev/null
source "${RECIPE_DIR}/fleet_common.sh"
PY_BIN="$(fleet_pybin)"

SWEEP_TAGS="${SWEEP_TAGS:-qwen2.5:32b llama3.1:70b gpt-oss:120b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-ollama}"
OOM_MIN_TPS="${OOM_MIN_TPS:-3}"
GTT_SPILL_GIB="${GTT_SPILL_GIB:-2}"
MAX_TOKENS="${MAX_TOKENS:-96}"
PROMPT_TOKENS="${PROMPT_TOKENS:-256}"
RUNS="${RUNS:-2}"
WARMUP="${WARMUP:-1}"
NUM_CTX="${NUM_CTX:-0}"
NUM_GPU="${NUM_GPU:-}"
USE_MMAP="${USE_MMAP:-}"
PULL_MODELS="${PULL_MODELS:-1}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-2}"
BOX="${BOX:-$(hostname 2>/dev/null || echo box)}"
OUT="${OUT:-${SCRIPT_DIR}/maxmodel-sweep-${BOX}.json}"

# Optional force-residency knobs -> extra tokrate_probe.py flags, applied to EVERY
# rung. Empty = omit the flag (default probe path, backward compatible).
PROBE_FORCE_ARGS=()
if [[ -n "${NUM_GPU}" ]]; then
  PROBE_FORCE_ARGS+=(--num-gpu "${NUM_GPU}")
fi
if [[ -n "${USE_MMAP}" ]]; then
  case "${USE_MMAP,,}" in
    false|0|no|off) PROBE_FORCE_ARGS+=(--no-use-mmap) ;;
    true|1|yes|on)  PROBE_FORCE_ARGS+=(--use-mmap) ;;
    *) echo "WARNING: USE_MMAP='${USE_MMAP}' unrecognized (use true/false); ignoring" >&2 ;;
  esac
fi

WORK="$(mktemp -d "${FLEET_STATE_DIR}/maxmodel-sweep-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

echo "==> [maxmodel-sweep] box=${BOX} work=${WORK}"
echo "    ollama=${OLLAMA_URL}  oom_min_tps=${OOM_MIN_TPS}  gtt_spill_gib=${GTT_SPILL_GIB}"
echo "    ascending ladder: ${SWEEP_TAGS}"
if [[ ${#PROBE_FORCE_ARGS[@]} -gt 0 ]]; then
  echo "    FORCED residency: num_gpu='${NUM_GPU}' use_mmap='${USE_MMAP}' -> probe args: ${PROBE_FORCE_ARGS[*]}"
fi

if ! curl -fsS --max-time 5 "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
  echo "ERROR: Ollama is not answering on ${OLLAMA_URL} -- bring the backend up first." >&2
  exit 1
fi
# Advisory: confirm the vllm-sr router is co-resident (this is the "topology").
if curl -fsS --max-time 5 "http://localhost:${ROUTER_PORT}/config/hash" >/dev/null 2>&1; then
  echo "    router co-resident: /config/hash answering on :${ROUTER_PORT} (topology UP)"
else
  echo "    WARNING: router /config/hash not answering on :${ROUTER_PORT}; sweep will still run" >&2
fi

unload() {  # best-effort unload so the next rung starts from a clean carveout
  local tag="$1"
  docker exec "${OLLAMA_CONTAINER}" ollama stop "${tag}" >/dev/null 2>&1 \
    || curl -fsS --max-time 10 "${OLLAMA_URL}/api/generate" \
         -d "{\"model\":\"${tag}\",\"keep_alive\":0}" >/dev/null 2>&1 || true
}

INDEX=""
for tag in ${SWEEP_TAGS}; do
  safe="$(echo "${tag}" | tr '/:.' '___')"
  INDEX="${INDEX} ${tag}"
  echo "==> [maxmodel-sweep] rung: ${tag}"

  pull_ok=1
  if [[ "${PULL_MODELS}" == "1" ]]; then
    echo "    pulling ${tag} (idempotent) ..."
    if docker exec "${OLLAMA_CONTAINER}" ollama pull "${tag}" >"${WORK}/${safe}.pull.log" 2>&1; then
      pull_ok=1
    else
      pull_ok=0
      echo "    PULL FAILED for ${tag} (unavailable or out of space) -- recording load-fail"
    fi
  fi
  echo "${pull_ok}" >"${WORK}/${safe}.pullok"

  if [[ "${pull_ok}" == "1" ]]; then
    "${PY_BIN}" "${SCRIPT_DIR}/resource_sampler.py" start \
      --out "${WORK}/${safe}.ndjson" --pidfile "${WORK}/${safe}.pid" \
      --interval "${SAMPLE_INTERVAL}" >/dev/null 2>&1 || true
    "${PY_BIN}" "${SCRIPT_DIR}/tokrate_probe.py" \
      --backend-url "${OLLAMA_URL}" --api ollama --model "${tag}" \
      --max-tokens "${MAX_TOKENS}" --prompt-tokens "${PROMPT_TOKENS}" \
      --num-ctx "${NUM_CTX}" \
      ${PROBE_FORCE_ARGS[@]+"${PROBE_FORCE_ARGS[@]}"} \
      --runs "${RUNS}" --warmup "${WARMUP}" --label "${tag}" \
      --out "${WORK}/${safe}.probe.json" >/dev/null 2>&1 || true
    "${PY_BIN}" "${SCRIPT_DIR}/resource_sampler.py" stop \
      --pidfile "${WORK}/${safe}.pid" --in "${WORK}/${safe}.ndjson" \
      --out "${WORK}/${safe}.res.json" >/dev/null 2>&1 || true
    # Authoritative residency (model still loaded): /api/ps size_vram/size == 1.0
    # means 100% VRAM-resident. Lets the verdict tell a resident-but-bandwidth-bound
    # rung apart from a real CPU-offload / GTT spill (see the assembly step below).
    curl -fsS --max-time 15 "${OLLAMA_URL}/api/ps" 2>/dev/null \
      | "${PY_BIN}" -c 'import json,sys
d=json.load(sys.stdin); t=sys.argv[1]
r=next((m for m in d.get("models", []) if m.get("name")==t), {}) or {}
print("%d %d" % (r.get("size_vram") or 0, r.get("size") or 0))' "${tag}" \
      >"${WORK}/${safe}.ps" 2>/dev/null || true
    unload "${tag}"
    sleep 2
  fi
done

echo "==> [maxmodel-sweep] assembling ${OUT}"
"${PY_BIN}" - "${WORK}" "${OUT}" "${BOX}" "${OOM_MIN_TPS}" "${GTT_SPILL_GIB}" \
  "${MAX_TOKENS}" "${PROMPT_TOKENS}" "${RUNS}" "${NUM_CTX}" "${NUM_GPU}" "${USE_MMAP}" "${INDEX}" <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone

(work, out_path, box, oom_min_tps, gtt_spill_gib,
 max_tokens, prompt_tokens, runs, num_ctx, num_gpu, use_mmap, index) = sys.argv[1:13]
oom_min_tps = float(oom_min_tps)
gtt_spill_b = float(gtt_spill_gib) * 1024**3
GIB = 1024**3

# Forced-residency knobs, echoed into the report shape (empty => not forced).
try:
    num_gpu_rec = int(num_gpu) if num_gpu.strip() else None
except ValueError:
    num_gpu_rec = num_gpu.strip() or None
use_mmap_rec = use_mmap.strip().lower() if use_mmap.strip() else None
forced_residency = bool(num_gpu.strip() or use_mmap.strip())


def load(name):
    try:
        with open(os.path.join(work, name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def safe(tag):
    return tag.replace("/", "_").replace(":", "_").replace(".", "_")


def pull_ok(s):
    try:
        with open(os.path.join(work, s + ".pullok"), "r", encoding="utf-8") as fh:
            return fh.read().strip() == "1"
    except OSError:
        return True


def read_ps(s):
    # (size_vram, size) from /api/ps captured while the model was loaded, else (None, None).
    try:
        with open(os.path.join(work, s + ".ps"), "r", encoding="utf-8") as fh:
            sv, sz = fh.read().split()
            return int(sv), int(sz)
    except (OSError, ValueError):
        return None, None


results = []
vram_total = gtt_total = sys_total = None
max_usable = None
max_resident = None
first_unusable = None

for tag in index.split():
    s = safe(tag)
    probe = load(s + ".probe.json")
    res = load(s + ".res.json")
    agg = (probe or {}).get("aggregate") or {}
    gpu = (res or {}).get("gpu") or {}
    host = (res or {}).get("host") or {}

    # Capture the box memory map from whatever rung sampled it.
    vram_total = vram_total or ((gpu.get("vram_total_b") or {}).get("max"))
    gtt_total = gtt_total or ((gpu.get("gtt_total_b") or {}).get("max"))
    sys_total = sys_total or ((host.get("mem_total_b") or {}).get("max"))

    ok_runs = agg.get("ok_runs") or 0
    tps = agg.get("decode_tps_median")
    peak_vram = (res or {}).get("peak_vram_used_b")
    peak_gtt = (res or {}).get("peak_gtt_used_b")
    peak_sys = (host.get("mem_used_b") or {}).get("max")
    err = probe.get("error") or (probe.get("runs_detail") or [{}])[-1].get("error")

    # Authoritative residency from /api/ps (decoupled from decode speed).
    ps_vram, ps_size = read_ps(s)
    gpu_frac = (ps_vram / ps_size) if (ps_vram is not None and ps_size) else None
    resident = gpu_frac is not None and gpu_frac >= 0.99
    gtt_spilled = peak_gtt is not None and peak_gtt > gtt_spill_b

    if not pull_ok(s):
        verdict, reason = "load-fail", "pull failed (model unavailable / no space)"
    elif not ok_runs:
        verdict = "load-fail"
        reason = "no tokens produced (OOM / would-not-load): %s" % (err or "n/a")
    elif tps is not None and tps < oom_min_tps:
        # Slow. Distinguish resident-but-bandwidth-bound from a real offload/spill: a
        # model fully in VRAM (size_vram==size, GTT ~0) that decodes below the floor is
        # LPDDR5X bandwidth-bound, NOT spilling -- flag usable-slow, not unusable.
        if resident and not gtt_spilled:
            verdict = "usable-slow"
            reason = ("decode %.2f tok/s < OOM_MIN_TPS=%g but 100%% VRAM-resident "
                      "(size_vram/size=%.2f, GTT ~0) -- LPDDR5X bandwidth-bound, not a spill"
                      % (tps, oom_min_tps, gpu_frac))
        else:
            verdict = "unusable(slow-spill)"
            if gtt_spilled:
                why = "GTT spill (%.1f GiB)" % (peak_gtt / GIB)
            elif gpu_frac is not None:
                why = "CPU offload (size_vram/size=%.2f)" % gpu_frac
            else:
                why = "GTT thrash / near-OOM"
            reason = "decode %.2f tok/s < OOM_MIN_TPS=%g (%s)" % (tps, oom_min_tps, why)
    else:
        verdict, reason = "usable", ""

    # Memory mode from RESIDENCY evidence (independent of the speed floor):
    #   gtt-spill     peak GTT above the spill threshold (weights past the carveout)
    #   vram-fit      100% resident in the VRAM carveout (size_vram==size, GTT ~0)
    #   cpu-offload   part of the model on CPU (size_vram < size) -- the slow failure
    #   vram-exceeded couldn't place it (no /api/ps signal, but VRAM was touched)
    if gtt_spilled:
        mem_mode = "gtt-spill"
    elif resident:
        mem_mode = "vram-fit"
    elif gpu_frac is not None:
        mem_mode = "cpu-offload"
    elif peak_vram is not None:
        mem_mode = "vram-exceeded"
    else:
        mem_mode = "unknown"

    # Ceilings: usable = >= floor; resident = loaded 100% in VRAM (usable or usable-slow).
    if verdict == "usable":
        max_usable = tag
    if verdict in ("usable", "usable-slow"):
        max_resident = tag
    if (verdict.startswith("unusable") or verdict == "load-fail") and first_unusable is None:
        first_unusable = tag

    results.append({
        "tag": tag,
        "verdict": verdict,
        "mem_mode": mem_mode,
        "gpu_resident_frac": round(gpu_frac, 3) if gpu_frac is not None else None,
        "decode_tps_median": tps,
        "ttft_ms_mean": agg.get("ttft_ms_mean"),
        "ok_runs": ok_runs,
        "peak_vram_used_b": peak_vram,
        "peak_gtt_used_b": peak_gtt,
        "peak_sys_used_b": peak_sys,
        "peak_vram_used_gib": round(peak_vram / GIB, 2) if peak_vram else None,
        "peak_gtt_used_gib": round(peak_gtt / GIB, 2) if peak_gtt else None,
        "peak_sys_used_gib": round(peak_sys / GIB, 2) if peak_sys else None,
        "reason": reason,
    })

report = {
    "schema": "maxmodel-sweep/v1",
    "box": box,
    "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "shape": {"max_tokens": int(max_tokens), "prompt_tokens": int(prompt_tokens),
              "runs": int(runs), "num_ctx": int(num_ctx),
              "num_gpu": num_gpu_rec, "use_mmap": use_mmap_rec,
              "forced_residency": forced_residency,
              "oom_min_tps": oom_min_tps, "gtt_spill_gib": float(gtt_spill_gib)},
    "memory_map": {
        "vram_total_b": vram_total, "gtt_total_b": gtt_total, "sys_total_b": sys_total,
        "vram_total_gib": round(vram_total / GIB, 2) if vram_total else None,
        "gtt_total_gib": round(gtt_total / GIB, 2) if gtt_total else None,
        "sys_total_gib": round(sys_total / GIB, 2) if sys_total else None,
    },
    "results": results,
    "max_usable_tag": max_usable,
    "max_resident_tag": max_resident,
    "first_unusable_tag": first_unusable,
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2, sort_keys=True)
    fh.write("\n")

# Human summary.
mm = report["memory_map"]
print("== maxmodel-sweep (box=%s) ==" % box)
print("memory map: VRAM %s GiB | GTT %s GiB | system %s GiB" % (
    mm["vram_total_gib"], mm["gtt_total_gib"], mm["sys_total_gib"]))
for r in results:
    print("  %-30s %-20s %-10s decode=%s tok/s  VRAM=%s GTT=%s sys=%s GiB" % (
        r["tag"], r["verdict"], r["mem_mode"],
        "n/a" if r["decode_tps_median"] is None else "%.1f" % r["decode_tps_median"],
        r["peak_vram_used_gib"], r["peak_gtt_used_gib"], r["peak_sys_used_gib"]))
print("max usable (>= floor): %s ; max VRAM-resident: %s ; first unusable: %s"
      % (max_usable, max_resident, first_unusable))
print("(written to %s)" % out_path)
PYEOF

echo "==> [maxmodel-sweep] done -> ${OUT}"
