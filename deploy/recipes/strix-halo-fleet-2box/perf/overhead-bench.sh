#!/usr/bin/env bash
#
# overhead-bench.sh -- TEST 1: how much does co-locating the vllm-sr stack cost
# on ONE Strix Halo box, and which models stop being usable?
#
# It answers the three questions directly:
#   (a) How much does the vllm-sr stack OCCUPY?  -> stack_footprint: the router /
#       Envoy / dashboard / Grafana container RAM+CPU and the host-memory delta
#       between "stack down" and "stack up" idle (unified-memory budget lost).
#   (b) How much does THROUGHPUT drop for the SAME model?  -> per tier, tokens/sec
#       with the stack DOWN (Ollama alone) vs UP (Ollama + router competing for the
#       same LPDDR5X bandwidth), both measured on the SAME direct-to-Ollama path so
#       the delta is pure contention; plus an end-to-end through-router number.
#   (c) Which model SPEC becomes unusable?  -> an ascending OOM sweep with the
#       stack up: a model that fails to load, or whose decode rate collapses while
#       GTT spills, is flagged unusable, giving the max-usable boundary.
#
# WHY a new harness: run-bench.sh measures routing LATENCY overhead and topology-
# bench.sh measures rps, but NEITHER measures the router's resource footprint,
# token-throughput drop from co-location, or the max-model-fit -- and both are
# Ollama-only. Strix Halo's unified memory makes this a bandwidth/footprint story,
# not a VRAM one, which is exactly what this script quantifies.
#
# The inference server (Ollama) runs as a SEPARATE container from the vllm-sr
# stack, so "stack down" cleanly stops the router/Envoy/dashboard while Ollama
# keeps serving -- a clean A/B on one box.
#
# SAFETY: this deliberately stops and restarts the local vllm-sr stack. Run it as
# a dedicated measurement pass, NOT during a live fleet convergence demo.
#
# Env (all optional):
#   OLLAMA_URL        direct inference-server base   (default http://localhost:11434)
#   ROUTER_URL        router OpenAI listener /v1      (default http://localhost:8899/v1)
#   ROUTER_CONFIG_URL router config API for up/down   (default http://localhost:8080/config/hash)
#   TIERS             "tag=alias ..." ; alias empty => skip through-router probe
#                     (default: the 5 poc-strix tiers; 32b has no alias by design)
#   OVERSIZED_TAGS    ascending OOM-sweep tags        (default "llama3.1:70b")
#   MAX_TOKENS/PROMPT_TOKENS/RUNS/CONCURRENCY  probe shape (default 128/256/3/1)
#   OOM_MIN_TPS       decode tok/s floor for "usable" (default 3)
#   PULL_MODELS       1 => `ollama pull` each tag first (default 1)
#   STACK_DOWN_CMD    bring the stack down            (default "vllm-sr stop")
#   STACK_UP_CMD      bring the stack up (idempotent) (default gateway-bring-up.sh)
#   RESTORE_STACK     1 => leave the stack UP at the end (default 1)
#   BOX               box label in the output JSON    (default: hostname)
#   OUT               output JSON path                (default overhead-<box>.json here)
#
# Usage (on the gateway box, with the stack already up):
#   bash overhead-bench.sh
#   TIERS="qwen2.5:7b=google/gemini-2.5-flash-lite qwen2.5:14b=google/gemini-3.1-pro" \
#   OVERSIZED_TAGS="qwen2.5:72b llama3.1:70b" bash overhead-bench.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=/dev/null
source "${RECIPE_DIR}/fleet_common.sh"
PY_BIN="$(fleet_pybin)"

OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
ROUTER_URL="${ROUTER_URL:-http://localhost:8899/v1}"
ROUTER_CONFIG_URL="${ROUTER_CONFIG_URL:-http://localhost:8080/config/hash}"
TIERS="${TIERS:-llama3.2:3b=qwen/qwen3.5-rocm qwen2.5:7b=google/gemini-2.5-flash-lite qwen2.5:14b=google/gemini-3.1-pro qwen3:14b=openai/gpt5.4 qwen2.5:32b=}"
OVERSIZED_TAGS="${OVERSIZED_TAGS:-llama3.1:70b}"
MAX_TOKENS="${MAX_TOKENS:-128}"
PROMPT_TOKENS="${PROMPT_TOKENS:-256}"
RUNS="${RUNS:-3}"
CONCURRENCY="${CONCURRENCY:-1}"
OOM_MIN_TPS="${OOM_MIN_TPS:-3}"
PULL_MODELS="${PULL_MODELS:-1}"
STACK_DOWN_CMD="${STACK_DOWN_CMD:-vllm-sr stop}"
STACK_UP_CMD="${STACK_UP_CMD:-bash ${SCRIPT_DIR%/perf}/gateway-bring-up.sh}"
RESTORE_STACK="${RESTORE_STACK:-1}"
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-ollama}"
BOX="${BOX:-$(hostname 2>/dev/null || echo box)}"
OUT="${OUT:-${SCRIPT_DIR}/overhead-${BOX}.json}"

WORK="$(mktemp -d "${FLEET_STATE_DIR}/overhead-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
echo "==> [overhead] box=${BOX}  work=${WORK}"
echo "    ollama=${OLLAMA_URL}  router=${ROUTER_URL}"
echo "    tiers: ${TIERS}"
echo "    oversized (OOM sweep): ${OVERSIZED_TAGS}"

# --- helpers --------------------------------------------------------------- #
stack_up_now()   { curl -fsS "${ROUTER_CONFIG_URL}" >/dev/null 2>&1; }
ollama_up_now()  { curl -fsS "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; }

wait_for() {  # wait_for <predicate-fn> <want 0|1 for up> <tries>
  local fn="$1" want="$2" tries="${3:-60}" i
  for ((i = 0; i < tries; i++)); do
    if "${fn}"; then [[ "${want}" == "1" ]] && return 0; else [[ "${want}" == "0" ]] && return 0; fi
    sleep 1
  done
  return 1
}

stack_down() {
  echo "==> [overhead] bringing the vllm-sr stack DOWN (${STACK_DOWN_CMD})"
  eval "${STACK_DOWN_CMD}" >/dev/null 2>&1 || true
  wait_for stack_up_now 0 60 || echo "    WARNING: router config API still answering; footprint delta may be understated." >&2
  ollama_up_now || echo "    WARNING: Ollama is not answering while stack is down -- baseline will be empty." >&2
}

stack_up() {
  echo "==> [overhead] bringing the vllm-sr stack UP (${STACK_UP_CMD})"
  eval "${STACK_UP_CMD}" || { echo "ERROR: STACK_UP_CMD failed." >&2; return 1; }
  wait_for stack_up_now 1 600 || { echo "ERROR: router config API never came up." >&2; return 1; }
}

pull_tag() {  # best-effort model pull; returns nonzero if it fails
  local tag="$1"
  [[ "${PULL_MODELS}" == "1" ]] || return 0
  docker exec "${OLLAMA_CONTAINER}" ollama pull "${tag}" >/dev/null 2>&1
}

# measure LABEL BACKEND_URL API MODEL  -> writes ${WORK}/LABEL.json (+ -res.json)
measure() {
  local label="$1" url="$2" api="$3" model="$4"
  local nd="${WORK}/${label}.ndjson" pid="${WORK}/${label}.pid"
  "${PY_BIN}" "${SCRIPT_DIR}/resource_sampler.py" start \
    --out "${nd}" --pidfile "${pid}" --interval 1 >/dev/null 2>&1 || true
  "${PY_BIN}" "${SCRIPT_DIR}/tokrate_probe.py" \
    --backend-url "${url}" --api "${api}" --model "${model}" \
    --max-tokens "${MAX_TOKENS}" --prompt-tokens "${PROMPT_TOKENS}" \
    --runs "${RUNS}" --concurrency "${CONCURRENCY}" --label "${label}" \
    --out "${WORK}/${label}.json" >/dev/null 2>&1 || true
  "${PY_BIN}" "${SCRIPT_DIR}/resource_sampler.py" stop \
    --pidfile "${pid}" --in "${nd}" --out "${WORK}/${label}-res.json" >/dev/null 2>&1 || true
}

snapshot() { "${PY_BIN}" "${SCRIPT_DIR}/resource_sampler.py" snapshot --out "${WORK}/$1.json" >/dev/null 2>&1 || true; }

# --- phase 1: BASELINE (stack down, Ollama alone) -------------------------- #
stack_down
sleep 2
snapshot idle-baseline
for entry in ${TIERS}; do
  tag="${entry%%=*}"
  pull_tag "${tag}" || echo "    (pull failed for ${tag}; probing anyway)"
  echo "==> [overhead] baseline (stack down): ${tag}"
  measure "baseline-$(echo "${tag}" | tr '/:' '__')" "${OLLAMA_URL}" ollama "${tag}"
done

# --- phase 2: COLOCATED (stack up) ----------------------------------------- #
stack_up
sleep 3
snapshot idle-colo
for entry in ${TIERS}; do
  tag="${entry%%=*}"; alias="${entry#*=}"; [[ "${alias}" == "${entry}" ]] && alias=""
  safe="$(echo "${tag}" | tr '/:' '__')"
  echo "==> [overhead] colocated direct: ${tag}"
  measure "colo-direct-${safe}" "${OLLAMA_URL}" ollama "${tag}"
  if [[ -n "${alias}" ]]; then
    echo "==> [overhead] colocated through-router: ${tag} (alias ${alias})"
    measure "colo-router-${safe}" "${ROUTER_URL}" openai "${alias}"
  fi
done

# --- phase 3: OOM sweep (ascending, stack up) ------------------------------ #
oom_index=""
for tag in ${OVERSIZED_TAGS}; do
  safe="$(echo "${tag}" | tr '/:' '__')"
  echo "==> [overhead] OOM sweep: attempting ${tag}"
  if pull_tag "${tag}"; then
    measure "oom-${safe}" "${OLLAMA_URL}" ollama "${tag}"
  else
    echo "{\"ok\":false,\"error\":\"pull failed (model unavailable or does not fit)\"}" \
      >"${WORK}/oom-${safe}.json"
    echo "{}" >"${WORK}/oom-${safe}-res.json"
  fi
  oom_index="${oom_index} ${tag}"
done

[[ "${RESTORE_STACK}" == "1" ]] || echo "==> [overhead] RESTORE_STACK!=1; leaving stack in its current state."

# --- assemble the report --------------------------------------------------- #
echo "==> [overhead] assembling ${OUT}"
"${PY_BIN}" - "${WORK}" "${OUT}" "${BOX}" "${MAX_TOKENS}" "${PROMPT_TOKENS}" "${RUNS}" \
  "${CONCURRENCY}" "${OOM_MIN_TPS}" "${TIERS}" "${oom_index}" <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone

(work, out_path, box, max_tokens, prompt_tokens, runs, concurrency,
 oom_min_tps, tiers_spec, oom_spec) = sys.argv[1:11]
oom_min_tps = float(oom_min_tps)


def load(name):
    try:
        with open(os.path.join(work, name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def safe(tag):
    return tag.replace("/", "_").replace(":", "_")


def decode_tps(probe):
    return ((probe or {}).get("aggregate") or {}).get("decode_tps_median")


def agg(probe):
    return (probe or {}).get("aggregate") or {}


def res_of(name):
    return load(name + "-res.json")


def container_mem(res, needle):
    for cname, rec in (res.get("containers") or {}).items():
        if needle in cname.lower():
            mm = (rec or {}).get("mem_used_b") or {}
            return mm.get("max")
    return None


def pct_drop(base, new):
    if not base or new is None:
        return None
    return (base - new) / base * 100.0


idle_base = load("idle-baseline.json")
idle_colo = load("idle-colo.json")
base_used = ((idle_base.get("host") or {}).get("mem_used_b"))
colo_used = ((idle_colo.get("host") or {}).get("mem_used_b"))
# idle snapshots are single samples: mem_used_b is a raw number, not reduced.
host_delta = (colo_used - base_used) if (isinstance(base_used, (int, float)) and isinstance(colo_used, (int, float))) else None

# Attribute stack containers from any colocated resource summary that saw them.
stack_containers = {}
for entry in tiers_spec.split():
    tag = entry.split("=", 1)[0]
    res = res_of("colo-direct-" + safe(tag))
    for needle, key in (("router", "router"), ("envoy", "envoy"),
                        ("dashboard", "dashboard"), ("grafana", "grafana"),
                        ("prometheus", "prometheus")):
        mem = container_mem(res, needle)
        if mem is not None:
            prev = stack_containers.get(key)
            stack_containers[key] = max(prev, mem) if prev else mem

tiers = []
usable_tags = []
for entry in tiers_spec.split():
    tag, _, alias = entry.partition("=")
    s = safe(tag)
    base = load("baseline-" + s + ".json")
    cdir = load("colo-direct-" + s + ".json")
    crtr = load("colo-router-" + s + ".json") if alias else {}
    b_tps, cd_tps, cr_tps = decode_tps(base), decode_tps(cdir), decode_tps(crtr)
    tier = {
        "tag": tag,
        "alias": alias or None,
        "baseline_decode_tps": b_tps,
        "colocated_direct_decode_tps": cd_tps,
        "colocated_router_decode_tps": cr_tps,
        "throughput_drop_pct_contention": pct_drop(b_tps, cd_tps),
        "throughput_drop_pct_end_to_end": pct_drop(b_tps, cr_tps),
        "baseline_ttft_ms": agg(base).get("ttft_ms_mean"),
        "colocated_router_ttft_ms": agg(crtr).get("ttft_ms_mean"),
        "peak_gtt_used_b": ((res_of("colo-direct-" + s).get("gpu") or {}).get("gtt_used_b") or {}).get("max"),
    }
    tiers.append(tier)
    if agg(cdir).get("ok_runs"):
        usable_tags.append(tag)

oom = []
first_unusable = None
for tag in oom_spec.split():
    s = safe(tag)
    probe = load("oom-" + s + ".json")
    res = res_of("oom-" + s)
    a = agg(probe)
    tps = decode_tps(probe)
    gtt = ((res.get("gpu") or {}).get("gtt_used_b") or {}).get("max")
    if not a.get("ok_runs"):
        verdict = "unusable(load-fail)"
    elif tps is not None and tps < oom_min_tps:
        verdict = "unusable(slow-spill)"
    else:
        verdict = "usable"
    if verdict == "usable":
        usable_tags.append(tag)
    elif first_unusable is None:
        first_unusable = tag
    oom.append({
        "tag": tag, "ok": bool(a.get("ok_runs")), "decode_tps": tps,
        "peak_gtt_used_b": gtt, "error": probe.get("error") or (probe.get("runs_detail") or [{}])[-1].get("error"),
        "verdict": verdict,
    })

report = {
    "schema": "overhead-bench/v1",
    "box": box,
    "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "shape": {"max_tokens": int(max_tokens), "prompt_tokens": int(prompt_tokens),
              "runs": int(runs), "concurrency": int(concurrency), "oom_min_tps": oom_min_tps},
    "unified_mem_total_b": (idle_colo.get("host") or {}).get("mem_total_b"),
    "stack_footprint": {
        "idle_host_mem_used_delta_b": host_delta,
        "containers_mem_b": stack_containers,
        "stack_container_mem_total_b": sum(v for v in stack_containers.values() if v) or None,
    },
    "tiers": tiers,
    "oom_sweep": oom,
    "max_usable_tag": usable_tags[-1] if usable_tags else None,
    "first_unusable_tag": first_unusable,
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2, sort_keys=True)
    fh.write("\n")

# Human summary.
print("== overhead (box=%s) ==" % box)
um = report["unified_mem_total_b"]
print("unified memory: %s GiB" % ("%.1f" % (um / 1024**3) if um else "n/a"))
fp = report["stack_footprint"]["stack_container_mem_total_b"]
print("vllm-sr stack container RAM: %s" % ("%.2f GiB" % (fp / 1024**3) if fp else "n/a"))
for t in tiers:
    print("  %-14s drop(contention)=%s%%  drop(end2end)=%s%%" % (
        t["tag"],
        "n/a" if t["throughput_drop_pct_contention"] is None else "%.1f" % t["throughput_drop_pct_contention"],
        "n/a" if t["throughput_drop_pct_end_to_end"] is None else "%.1f" % t["throughput_drop_pct_end_to_end"]))
print("max usable model: %s ; first unusable: %s" % (report["max_usable_tag"], report["first_unusable_tag"]))
print("(written to %s)" % out_path)
PYEOF

echo "==> [overhead] done -> ${OUT}"
