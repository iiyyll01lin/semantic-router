#!/usr/bin/env bash
#
# run-perf-fleet.sh -- FLEET-WIDE runner for the two perf tests, aggregated into
# one bundle. It runs Test 1 (overhead-bench.sh) and Test 2 (server-bench.sh) on
# Halo-A locally and, best-effort, on Halo-B over SSH, then rolls every per-box
# JSON into a single fleet record with perf_metrics.py.
#
# It is the turnkey entry point for:
#   Test 1 -- "how much does vllm-sr occupy, how much throughput drops, which
#              models become unusable" (per box, fleet-aggregated)
#   Test 2 -- "performance difference of different inference servers bundled with
#              vllm-sr" (per box, fleet-aggregated)
#
# SAFETY: overhead-bench.sh stops/restarts the local vllm-sr stack. Run this as a
# dedicated measurement pass; do not interleave it with a live convergence demo.
#
# Env (all optional):
#   BUNDLE        output dir for per-box JSON + aggregate (default: a timestamped
#                 dir under the fleet state dir; run-all passes its RUN_DIR here)
#   HALO_A_BOX / HALO_B_BOX   box labels (default halo-a / halo-b)
#   RUN_OVERHEAD / RUN_SERVER 1/0 toggles (default 1 / 1)
#   HALO_A_PERF   1 => measure the LOCAL box (Halo-A) (default 1); 0 => skip it and
#                 reuse a pre-seeded overhead-/server-<halo-a>.json already in BUNDLE
#   HALO_B_PERF   1 => also measure Halo-B over SSH (default 0; needs the perf dir
#                 present on Halo-B under HALO_B_REPO and the stack up there)
#   HALO_B_SSH / HALO_B_REPO / HALO_B_SSH_KEY / HALO_B_SSH_PORT  (from env/fleet.env)
#   plus any overhead-bench.sh / server-bench.sh env (TIERS, SERVERS, RUNS, ...);
#   these perf knobs are FORWARDED to the Halo-B SSH legs so both boxes run the
#   same (safe) shape instead of the bench defaults (which include 32B + a 70B sweep)
#
# Usage:
#   bash run-perf-fleet.sh
#   HALO_B_PERF=1 HALO_B_SSH=user@halo-b HALO_B_REPO=~/semantic-router bash run-perf-fleet.sh
set -uo pipefail   # NOT -e: always reach the aggregation step

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=/dev/null
source "${RECIPE_DIR}/fleet_common.sh"
PY_BIN="$(fleet_pybin)"

# Offline proof path: exercise the whole harness against mock backends (no ROCm,
# no Docker, no gateway) so the pipeline can be validated before any HW run.
if [ "${SELFTEST:-0}" = "1" ]; then
  echo "==> [perf-fleet] SELFTEST=1: offline verifier (no hardware)"
  exec "${PY_BIN}" "${SCRIPT_DIR}/verify_perf_local.py"
fi

# Pull SSH env from the deploy's fleet.env if present (same pattern as run-all).
ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
if [ -f "${ENV_FILE}" ]; then
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
fi

BUNDLE="${BUNDLE:-${FLEET_STATE_DIR}/perf-run-$(date +%Y%m%d-%H%M%S)}"
HALO_A_BOX="${HALO_A_BOX:-halo-a}"
HALO_B_BOX="${HALO_B_BOX:-halo-b}"
RUN_OVERHEAD="${RUN_OVERHEAD:-1}"
RUN_SERVER="${RUN_SERVER:-1}"
HALO_B_PERF="${HALO_B_PERF:-0}"
mkdir -p "${BUNDLE}"
echo "==> [perf-fleet] bundle=${BUNDLE}"

run_local() {
  if [ "${RUN_OVERHEAD}" = "1" ]; then
    echo "==> [perf-fleet] Halo-A Test 1 (overhead-bench)"
    BOX="${HALO_A_BOX}" OUT="${BUNDLE}/overhead-${HALO_A_BOX}.json" \
      bash "${SCRIPT_DIR}/overhead-bench.sh" || echo "    (overhead-bench returned nonzero; continuing)"
  fi
  if [ "${RUN_SERVER}" = "1" ]; then
    echo "==> [perf-fleet] Halo-A Test 2 (server-bench)"
    BOX="${HALO_A_BOX}" OUT="${BUNDLE}/server-${HALO_A_BOX}.json" \
      bash "${SCRIPT_DIR}/server-bench.sh" || echo "    (server-bench returned nonzero; continuing)"
  fi
}

run_halo_b() {
  [ "${HALO_B_PERF}" = "1" ] || { echo "==> [perf-fleet] Halo-B perf skipped (HALO_B_PERF!=1)"; return 0; }
  [ -n "${HALO_B_SSH:-}" ] || { echo "==> [perf-fleet] Halo-B perf skipped (no HALO_B_SSH)"; return 0; }
  local repo="${HALO_B_REPO:-}"
  [ -n "${repo}" ] || { echo "    WARNING: HALO_B_PERF=1 but HALO_B_REPO unset; skipping Halo-B." >&2; return 0; }
  local perf_dir="${repo}/deploy/recipes/strix-halo-fleet-2box/perf"
  # Concrete path (NOT \${TMPDIR:-/tmp}): scp uses SFTP and does NOT shell-expand
  # the remote path, so a literal \${TMPDIR...} dir would make the copy-back fail.
  local remote_tmp="/tmp/vllm-sr-perf"
  # Harden every ssh/scp: with a key they work, WITHOUT one they fail fast to a
  # clean "skipped" instead of dropping to an interactive password prompt that
  # would hang the whole run (the root cause of run-20260709-020717 stalling).
  local SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15)
  [ -n "${HALO_B_SSH_KEY:-}" ] && SSH_OPTS+=(-i "${HALO_B_SSH_KEY}")
  [ -n "${HALO_B_SSH_PORT:-}" ] && SSH_OPTS+=(-p "${HALO_B_SSH_PORT}")
  # Forward the SAME safe perf knobs to Halo-B that shape the local run. WITHOUT
  # this, Halo-B's overhead-/server-bench fall back to their DEFAULTS -- which
  # include a 32B tier and a 70B OOM sweep -- and would OOM-thrash a memory-tight
  # box. Only vars that are actually SET are forwarded; each value is wrapped in
  # double quotes so the remote bash keeps space-bearing values (e.g. multi-tier
  # TIERS) intact (the remote script itself is single-quoted, so these embedded
  # double quotes quote the value remotely). Values must stay free of embedded
  # double quotes / $ / backticks (the perf knobs are simple tag=alias lists).
  local remote_env="" _k
  for _k in TIERS OVERSIZED_TAGS SERVERS RUNS MAX_TOKENS PROMPT_TOKENS CONCURRENCY \
            OOM_MIN_TPS PULL_MODELS LEMONADE_MODEL SERVER_BENCH_ROUTER ROUTER_ALIAS \
            COMMON_MODEL_HINT VLLM_SR_IMAGE_PULL_POLICY; do
    [ -n "${!_k+x}" ] && remote_env+="${_k}=\"${!_k}\" "
  done
  echo "==> [perf-fleet] Halo-B perf over SSH (${HALO_B_SSH}, perf=${perf_dir})"
  [ -n "${remote_env}" ] && echo "    forwarding knobs to Halo-B: ${remote_env}"
  ssh "${SSH_OPTS[@]}" "${HALO_B_SSH}" "bash -lc '\
    set -e; mkdir -p ${remote_tmp}; \
    if [ ! -d ${perf_dir} ]; then echo MISSING_PERF_DIR; exit 3; fi; \
    cd ${perf_dir}; \
    ${RUN_OVERHEAD:+ ${remote_env}BOX=${HALO_B_BOX} OUT=${remote_tmp}/overhead-${HALO_B_BOX}.json bash overhead-bench.sh || true;} \
    ${RUN_SERVER:+ ${remote_env}BOX=${HALO_B_BOX} OUT=${remote_tmp}/server-${HALO_B_BOX}.json bash server-bench.sh || true;} \
    '" 2>&1 | sed 's/^/    [halo-b] /' || echo "    (Halo-B perf run returned nonzero; continuing)"
  # Best-effort copy the per-box JSON back into the bundle.
  for f in "overhead-${HALO_B_BOX}.json" "server-${HALO_B_BOX}.json"; do
    scp "${SSH_OPTS[@]}" "${HALO_B_SSH}:${remote_tmp//\\/}/${f}" "${BUNDLE}/${f}" 2>/dev/null \
      || echo "    (no ${f} from Halo-B)"
  done
}

# HALO_A_PERF=0 skips the LOCAL box (Halo-A) so an already-collected/committed
# overhead-/server-<halo-a>.json can be reused as-is (seed it into BUNDLE first);
# the aggregation below then combines that with the fresh Halo-B leg. Default 1.
if [ "${HALO_A_PERF:-1}" = "1" ]; then
  run_local
else
  echo "==> [perf-fleet] Halo-A (local) perf skipped (HALO_A_PERF=0; reusing seeded per-box JSON in ${BUNDLE})"
fi
run_halo_b

echo "==> [perf-fleet] aggregating -> ${BUNDLE}/perf-metrics.json + perf-summary.md"
"${PY_BIN}" "${SCRIPT_DIR}/perf_metrics.py" --bundle "${BUNDLE}" || true

echo
echo "=============================================================="
echo "Fleet perf bundle:"
echo "  ${BUNDLE}"
ls -1 "${BUNDLE}" 2>/dev/null | sed 's/^/    /'
echo "=============================================================="
