#!/usr/bin/env bash
#
# Strix Halo PoC: evidence benchmark runner against a running `vllm-sr serve`
# stack. This is the executable counterpart to docs/poc/03-strix-halo-runbook.md
# section 9 ("Measure and Demo") and maps the bench suite onto the PoC success
# criteria in docs/poc/02-poc-plan.md section 1.
#
# Why this script exists:
#   The bench/ tools default to the MANUAL dev topology (direct backend :8000,
#   Envoy :8801, metrics :9279). The PoC runbook brings the stack up with the
#   `vllm-sr serve` CLI, which exposes the LISTENER on :8899 and metrics on
#   :9190 (src/vllm-sr/cli/consts.py). This wrapper points the under-wired bench
#   tools at the correct vllm-sr ports so they produce GA-style evidence without
#   per-invocation flag surgery.
#
# What it runs (each step maps to a 02-poc-plan.md section-1 success criterion):
#   1. GA diagnostic probe            -> routing observability (x-vsr-* headers)
#   2. Agentic session-routing live   -> local-served ratio + routing + overhead
#   3. Cache-token reporting probe    -> semantic-cache / cached-token evidence
#   4. (--with-reasoning, opt-in)     -> quality retention (router vs direct)
#
# It does NOT auto-run fault injection or the GA assembler (both need extra
# wiring); those are printed as advanced next steps at the end.
#
# Inputs (env vars, all optional):
#   BASE_URL          router LISTENER /v1 base.   Default: http://localhost:8899/v1
#   METRICS_URL       Prometheus metrics.         Default: http://localhost:9190/metrics
#   BASELINE_BASE_URL direct-backend /v1 base for A/B (e.g. the local Ollama or
#                     Halo-B). Empty disables baseline/overhead/quality steps.
#                     Example: http://localhost:11434/v1
#   BASELINE_MODEL    real model the baseline serves.  Default: llama3.2:3b
#   SCENARIO          agentic scenario.           Default: tool-heavy
#   SESSIONS/TURNS/CONCURRENCY  agentic shape.    Default: 6 / 8 / 2
#   SAMPLES_PER_CATEGORY  reasoning sample size.  Default: 5
#
# Usage (run on the box where the stack is serving, after bring-up.sh):
#   bash run-bench.sh
#   BASELINE_BASE_URL=http://localhost:11434/v1 bash run-bench.sh
#   BASELINE_BASE_URL=http://localhost:11434/v1 bash run-bench.sh --with-reasoning
#
# This is a traffic-only runner: it sends OpenAI-compatible requests to the
# running stack and writes results under .agent-harness/experiments/. It does
# NOT serve, deploy, or mutate any config.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

# vllm-sr stack defaults (NOT the bench :8000/:8801/:9279 manual-dev defaults).
BASE_URL="${BASE_URL:-http://localhost:8899/v1}"
METRICS_URL="${METRICS_URL:-http://localhost:9190/metrics}"
BASELINE_BASE_URL="${BASELINE_BASE_URL:-}"
BASELINE_MODEL="${BASELINE_MODEL:-llama3.2:3b}"
SCENARIO="${SCENARIO:-tool-heavy}"
SESSIONS="${SESSIONS:-6}"
TURNS="${TURNS:-8}"
CONCURRENCY="${CONCURRENCY:-2}"
SAMPLES_PER_CATEGORY="${SAMPLES_PER_CATEGORY:-5}"

WITH_REASONING=0
for arg in "$@"; do
  case "${arg}" in
    --with-reasoning) WITH_REASONING=1 ;;
    -h|--help)
      sed -n '2,55p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument '${arg}' (use --with-reasoning or --help)" >&2
      exit 2
      ;;
  esac
done

PY_BIN="python3"
command -v "${PY_BIN}" >/dev/null 2>&1 || PY_BIN="python"
command -v "${PY_BIN}" >/dev/null 2>&1 || {
  echo "ERROR: neither python3 nor python is on PATH." >&2
  exit 1
}

BENCH_DIR="${REPO_ROOT}/bench"

# Optional baseline args, shared by the agentic and cache steps.
baseline_args=()
if [[ -n "${BASELINE_BASE_URL}" ]]; then
  baseline_args+=(--baseline-base-url "${BASELINE_BASE_URL}" --baseline-model "${BASELINE_MODEL}")
  echo "Baseline A/B ENABLED: ${BASELINE_BASE_URL} (model ${BASELINE_MODEL})"
else
  echo "Baseline A/B DISABLED: set BASELINE_BASE_URL=http://localhost:11434/v1 to compare vs the direct backend."
fi
echo "Router base-url : ${BASE_URL}"
echo "Metrics url     : ${METRICS_URL}"
echo

echo "==> [1/4] GA diagnostic probe (routing observability: x-vsr-* headers)"
# Informational, not a hard gate: a single-shot probe cannot produce
# x-vsr-session-phase (the session-aware/ACR phase only appears with multi-turn
# session context), so it may report that one header missing on a cold request.
# The session diagnostics are gated for real in the multi-turn agentic step below
# (--require-router-diagnostics), so we do NOT stop the run on the probe exit code.
"${PY_BIN}" "${BENCH_DIR}/session_routing_branch_image_probe.py" \
  --base-url "${BASE_URL}" \
  --model auto \
  || echo "    NOTE: probe reported issues above; x-vsr-session-phase is expected to be absent on a single-shot request (it is verified in step 2)."

echo "==> [2/4] Agentic session-routing live (local ratio + routing + overhead)"
"${PY_BIN}" "${BENCH_DIR}/agentic_routing_live_benchmark.py" \
  --base-url "${BASE_URL}" \
  --metrics-url "${METRICS_URL}" \
  --model auto \
  --scenario "${SCENARIO}" \
  --sessions "${SESSIONS}" \
  --turns "${TURNS}" \
  --concurrency "${CONCURRENCY}" \
  --require-router-diagnostics \
  "${baseline_args[@]}"

echo "==> [3/4] Cache-token reporting probe (semantic-cache / cached-token evidence)"
"${PY_BIN}" "${BENCH_DIR}/cache_token_probe.py" \
  --base-url "${BASE_URL}" \
  --model auto \
  --repeats 8 \
  "${baseline_args[@]}"

if [[ "${WITH_REASONING}" -eq 1 ]]; then
  if [[ -z "${BASELINE_BASE_URL}" ]]; then
    echo "ERROR: --with-reasoning needs BASELINE_BASE_URL set to the direct backend" >&2
    echo "       (e.g. the local Ollama at http://localhost:11434/v1) so the router" >&2
    echo "       can be compared against direct generation for quality retention." >&2
    exit 2
  fi
  echo "==> [4/4] Reasoning quality retention (router vs direct on reasoning datasets)"
  echo "    NOTE: this needs the bench dataset deps: pip install -r ${BENCH_DIR}/requirements.txt"
  "${PY_BIN}" "${BENCH_DIR}/router_reason_bench.py" \
    --run-router --run-vllm \
    --router-endpoint "${BASE_URL}" \
    --router-models auto \
    --vllm-endpoint "${BASELINE_BASE_URL}" \
    --vllm-models "${BASELINE_MODEL}" \
    --samples-per-category "${SAMPLES_PER_CATEGORY}"
else
  echo "==> [4/4] Reasoning quality retention SKIPPED (pass --with-reasoning to enable)"
fi

echo
echo "Done. Evidence is under ${REPO_ROOT}/.agent-harness/experiments/:"
echo "  - branch-image-diagnostic/   (step 1: GA diagnostic headers)"
echo "  - live-agentic-routing/      (step 2: summary.json + comparison.* when baseline set)"
echo "  - cache-token-probe/         (step 3: aggregate-summary.json)"
[[ "${WITH_REASONING}" -eq 1 ]] && echo "  - results/reasonbench/        (step 4: router-vs-direct accuracy/latency)"
echo
echo "Advanced (not auto-run):"
echo "  * Failure recovery: start bench/openai_fault_proxy.py between the backend and the"
echo "    router, then re-serve so Envoy points at the proxy (see bench/README.md). Changing"
echo "    only the runtime config after startup leaves Envoy on the old backend cluster."
echo "  * GA evidence bundle: assemble one gate with bench/session_routing_branch_image_benchmark.py"
echo "    (--diagnostic-summary/--live-aggregate/--cache-aggregate from the dirs above)."
