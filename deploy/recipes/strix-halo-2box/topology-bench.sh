#!/usr/bin/env bash
#
# topology-bench.sh -- measure and compare throughput / latency / network-hop
# cost across deployed topologies for the Strix Halo PoC.
#
# WHY this exists: run-bench.sh proves routing happens, but it does NOT isolate
# the topology-specific cost (the cross-box network hop) and does NOT compare one
# topology against another. This harness does both. For each topology you DEPLOY,
# it (1) isolates the pure network-hop latency to the edge (local) and
# datacenter (remote) backends -- model-size free, so it is the clean topology
# signal -- and (2) drives a FIXED agentic load through the gateway and records
# throughput (rps), latency percentiles, and the edge/datacenter request split.
# Run it once per deployed topology with the SAME load, then --report renders a
# side-by-side comparison and writes topology-comparison.md.
#
# HONEST CAVEAT: on this PoC both boxes are gfx1151 APUs (Halo-B only PLAYS the
# datacenter), so cross-topology differences reflect TOPOLOGY / NETWORK /
# CONTENTION, not hardware tiers. This is a topology/throughput comparison, not
# an Instinct performance claim (see docs/poc/07-client-server-topology.md 5,6.5).
#
# USAGE:
#   # 1) deploy a topology (e.g. single-box poc-strix.yaml), then measure it:
#   bash topology-bench.sh --label single-box --edge-backend localhost:11434
#
#   # 2) re-deploy the 2-box edge-gateway (poc-client-edge.yaml), then measure:
#   bash topology-bench.sh --label edge-2box \
#       --edge-backend localhost:11434 --datacenter-backend "$HALO_B_IP:11434"
#
#   # 3) render the comparison report from all measured topologies:
#   bash topology-bench.sh --report
#
# Keep --scenario/--sessions/--turns/--concurrency IDENTICAL across topologies
# (the report flags any mismatch as a not-apples-to-apples comparison). To also
# explore a concurrency sweep, use distinct labels (e.g. edge-c1 / edge-c2).
#
# Inputs (flags, all optional except --label for a measure run):
#   --label NAME              topology label (required to measure)
#   --base-url URL            gateway /v1 base   (default http://localhost:8899/v1)
#   --edge-backend H:P        local backend for the 0-hop probe (default localhost:11434)
#   --datacenter-backend H:P  remote backend for the 1-hop probe (default: none)
#   --scenario NAME           agentic scenario   (default tool-heavy)
#   --sessions/--turns/--concurrency  agentic shape (default 6 / 8 / 2)
#   --hop-samples N           network-hop samples (default 20)
#   --results-dir DIR         output dir (default .agent-harness/experiments/topology-bench)
#   --report | --compare      aggregate all topology-*.json into a report
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BENCH_DIR="${REPO_ROOT}/bench"

PY_BIN="python3"
command -v "${PY_BIN}" >/dev/null 2>&1 || PY_BIN="python"
command -v "${PY_BIN}" >/dev/null 2>&1 || { echo "ERROR: python3/python not on PATH." >&2; exit 1; }

BASE_URL="http://localhost:8899/v1"
EDGE_BACKEND="localhost:11434"
DATACENTER_BACKEND=""
SCENARIO="tool-heavy"
SESSIONS=6
TURNS=8
CONCURRENCY=2
HOP_SAMPLES=20
RESULTS_DIR="${REPO_ROOT}/.agent-harness/experiments/topology-bench"
LABEL=""
MODE="measure"
# Edge/datacenter router-alias classification (matches smoke_test.py); override
# via TOPO_EDGE_MODELS / TOPO_DATACENTER_MODELS if your config differs.
EDGE_MODELS_CSV="${TOPO_EDGE_MODELS:-qwen/qwen3.5-rocm,google/gemini-2.5-flash-lite}"
DC_MODELS_CSV="${TOPO_DATACENTER_MODELS:-google/gemini-3.1-pro,openai/gpt5.4}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) LABEL="$2"; shift 2;;
    --base-url) BASE_URL="$2"; shift 2;;
    --edge-backend) EDGE_BACKEND="$2"; shift 2;;
    --datacenter-backend) DATACENTER_BACKEND="$2"; shift 2;;
    --scenario) SCENARIO="$2"; shift 2;;
    --sessions) SESSIONS="$2"; shift 2;;
    --turns) TURNS="$2"; shift 2;;
    --concurrency) CONCURRENCY="$2"; shift 2;;
    --hop-samples) HOP_SAMPLES="$2"; shift 2;;
    --results-dir) RESULTS_DIR="$2"; shift 2;;
    --report|--compare) MODE="report"; shift;;
    -h|--help) sed -n '2,47p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "ERROR: unknown argument '$1' (use --help)" >&2; exit 2;;
  esac
done

mkdir -p "${RESULTS_DIR}"

# --------------------------------------------------------------------------- #
# report mode: aggregate all topology-*.json into a comparison report
# --------------------------------------------------------------------------- #
if [[ "${MODE}" == "report" ]]; then
  "${PY_BIN}" - "${RESULTS_DIR}" <<'PYEOF'
import glob, json, os, sys
results_dir = sys.argv[1]
files = sorted(glob.glob(os.path.join(results_dir, "topology-*.json")))
if not files:
    print(f"No topology-*.json under {results_dir}. Run a measure pass first.")
    sys.exit(0)
rows = []
for f in files:
    with open(f) as fh:
        rows.append(json.load(fh))


def fmt(v, nd=1):
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:.{nd}f}"
    return str(v)


def pct_delta(new, base):
    if new is None or base in (None, 0):
        return None
    return (new - base) / base * 100.0


out = []
out.append("# Topology performance comparison\n")
out.append("> Honest caveat: on this PoC both boxes are gfx1151 APUs (Halo-B plays")
out.append("> the datacenter), so the differences below reflect topology / network /")
out.append("> contention, NOT hardware tiers. Topology/throughput comparison only.\n")
shapes = {(r["shape"]["scenario"], r["shape"]["sessions"], r["shape"]["turns"],
           r["shape"]["concurrency"]) for r in rows}
if len(shapes) > 1:
    out.append("> WARNING: load shapes differ across topologies -- NOT apples-to-apples:")
    for r in rows:
        s = r["shape"]
        out.append(f">   - {r['label']}: {s['scenario']} sessions={s['sessions']} "
                   f"turns={s['turns']} concurrency={s['concurrency']}")
    out.append("")
else:
    s = rows[0]["shape"]
    out.append(f"Load (identical across all topologies): scenario={s['scenario']} "
               f"sessions={s['sessions']} turns={s['turns']} "
               f"concurrency={s['concurrency']} -> {rows[0]['agentic']['requests']} requests.\n")
out.append("| topology | requests | rps | p50 ms | p95 ms | p99 ms | edge % | dc % | "
           "edge hop ms | dc hop ms | hop delta ms | success |")
out.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
for r in rows:
    a, h = r["agentic"], r["hop"]
    sr = a.get("success_rate")
    sr_str = "-" if sr is None else f"{sr * 100:.0f}%"
    out.append("| {label} | {req} | {rps} | {p50} | {p95} | {p99} | {edge} | {dc} | "
               "{eh} | {dh} | {hd} | {sr} |".format(
                   label=r["label"], req=fmt(a.get("requests"), 0), rps=fmt(a.get("rps"), 3),
                   p50=fmt(a.get("p50")), p95=fmt(a.get("p95")), p99=fmt(a.get("p99")),
                   edge=fmt(a.get("edge_pct"), 0), dc=fmt(a.get("datacenter_pct"), 0),
                   eh=fmt(h.get("edge_hop_ms"), 3), dh=fmt(h.get("datacenter_hop_ms"), 3),
                   hd=fmt(h.get("hop_delta_ms"), 3), sr=sr_str))
if len(rows) >= 2:
    base = rows[0]
    out.append(f"\n## Deltas vs `{base['label']}` (baseline)\n")
    for r in rows[1:]:
        b, c = base["agentic"], r["agentic"]
        out.append(f"- **{r['label']}**: rps {fmt(pct_delta(c.get('rps'), b.get('rps')), 1)}% "
                   f" .  p95 {fmt(pct_delta(c.get('p95'), b.get('p95')), 1)}%  vs {base['label']}")
out.append("\n## Per-topology model distribution\n")
for r in rows:
    counts = r["agentic"].get("selected_model_counts", {})
    dist = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    out.append(f"- **{r['label']}**: {dist or '(none)'}")
text = "\n".join(out) + "\n"
report_path = os.path.join(results_dir, "topology-comparison.md")
with open(report_path, "w") as fh:
    fh.write(text)
print(text)
print(f"(written to {report_path})")
PYEOF
  exit 0
fi

# --------------------------------------------------------------------------- #
# measure mode: one topology pass
# --------------------------------------------------------------------------- #
[[ -n "${LABEL}" ]] || { echo "ERROR: --label is required for a measure run (or use --report)." >&2; exit 2; }

echo "==> Topology bench: label='${LABEL}'  base-url=${BASE_URL}"
echo "    edge backend=${EDGE_BACKEND}  datacenter backend=${DATACENTER_BACKEND:-<none>}"
echo "    load: scenario=${SCENARIO} sessions=${SESSIONS} turns=${TURNS} concurrency=${CONCURRENCY}"
echo

# 1) pure network-hop probe -- isolates the topology network cost (model-size free).
hop_median_ms() {
  local host_port="$1" n="$2"
  local url="http://${host_port}/api/tags"
  {
    for ((i = 0; i < n; i++)); do
      curl -s -o /dev/null -w '%{time_total}\n' --max-time 5 "${url}" 2>/dev/null || true
    done
  } | "${PY_BIN}" -c 'import sys, statistics
xs = [float(x) * 1000 for x in sys.stdin if x.strip()]
print(round(statistics.median(xs), 3) if xs else "")'
}

echo "==> [1/2] Network-hop probe (${HOP_SAMPLES} samples, GET /api/tags)"
EDGE_HOP_MS="$(hop_median_ms "${EDGE_BACKEND}" "${HOP_SAMPLES}")"
echo "    edge (0-hop, ${EDGE_BACKEND}): ${EDGE_HOP_MS:-n/a} ms (median)"
DC_HOP_MS=""
if [[ -n "${DATACENTER_BACKEND}" ]]; then
  DC_HOP_MS="$(hop_median_ms "${DATACENTER_BACKEND}" "${HOP_SAMPLES}")"
  echo "    datacenter (1-hop, ${DATACENTER_BACKEND}): ${DC_HOP_MS:-n/a} ms (median)"
fi
echo

# 2) agentic throughput under a fixed load.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${RESULTS_DIR}/agentic-${LABEL}-${STAMP}"
mkdir -p "${RUN_DIR}"
echo "==> [2/2] Agentic load (agentic_routing_live_benchmark.py)"
"${PY_BIN}" "${BENCH_DIR}/agentic_routing_live_benchmark.py" \
  --base-url "${BASE_URL}" --model auto \
  --scenario "${SCENARIO}" --sessions "${SESSIONS}" --turns "${TURNS}" --concurrency "${CONCURRENCY}" \
  --output-dir "${RUN_DIR}" >/dev/null 2>&1 || true
SUMMARY_JSON="${RUN_DIR}/summary.json"
[[ -f "${SUMMARY_JSON}" ]] || {
  echo "ERROR: agentic benchmark produced no summary.json at ${SUMMARY_JSON}." >&2
  echo "       Is the gateway up at ${BASE_URL}? Try: curl -sS ${BASE_URL%/v1}/v1/models" >&2
  exit 1
}

# 3) assemble topology-<label>.json from the hop probe + the agentic summary.
OUT_JSON="${RESULTS_DIR}/topology-${LABEL}.json"
"${PY_BIN}" - "${SUMMARY_JSON}" "${OUT_JSON}" "${LABEL}" "${BASE_URL}" "${EDGE_HOP_MS}" "${DC_HOP_MS}" \
  "${SCENARIO}" "${SESSIONS}" "${TURNS}" "${CONCURRENCY}" "${EDGE_MODELS_CSV}" "${DC_MODELS_CSV}" <<'PYEOF'
import json, sys
from datetime import datetime, timezone
(summary_path, out_path, label, base_url, edge_hop, dc_hop, scenario,
 sessions, turns, concurrency, edge_csv, dc_csv) = sys.argv[1:13]
with open(summary_path) as fh:
    s = json.load(fh)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


lat = s.get("latency_ms", {}) or {}
counts = s.get("selected_model_counts", {}) or {}
edge_models = {m.strip() for m in edge_csv.split(",") if m.strip()}
dc_models = {m.strip() for m in dc_csv.split(",") if m.strip()}
edge_n = sum(v for k, v in counts.items() if k in edge_models)
dc_n = sum(v for k, v in counts.items() if k in dc_models)
total = sum(counts.values()) or 0
eh, dh = num(edge_hop), num(dc_hop)
out = {
    "label": label,
    "base_url": base_url,
    "measured_at": datetime.now(timezone.utc).isoformat(),
    "shape": {"scenario": scenario, "sessions": int(sessions),
              "turns": int(turns), "concurrency": int(concurrency)},
    "agentic": {
        "requests": s.get("requests"),
        "success_rate": s.get("success_rate"),
        "rps": s.get("requests_per_second"),
        "p50": lat.get("p50"), "p95": lat.get("p95"), "p99": lat.get("p99"),
        "mean": lat.get("mean"), "max": lat.get("max"),
        "wall_time_seconds": s.get("wall_time_seconds"),
        "selected_model_counts": counts,
        "edge_requests": edge_n, "datacenter_requests": dc_n,
        "edge_pct": (edge_n / total * 100) if total else None,
        "datacenter_pct": (dc_n / total * 100) if total else None,
    },
    "hop": {
        "edge_hop_ms": eh, "datacenter_hop_ms": dh,
        "hop_delta_ms": (dh - eh) if (eh is not None and dh is not None) else None,
    },
}
with open(out_path, "w") as fh:
    json.dump(out, fh, indent=2, sort_keys=True)
print(json.dumps(out, indent=2, sort_keys=True))
PYEOF

echo
echo "==> Wrote ${OUT_JSON}"
echo "    Measure the other topologies the same way (same load), then render:"
echo "      bash ${BASH_SOURCE[0]##*/} --report"
