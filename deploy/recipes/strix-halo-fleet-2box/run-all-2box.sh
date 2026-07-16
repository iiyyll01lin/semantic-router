#!/usr/bin/env bash
#
# ONE-SHOT, hands-off runner: deploy + verify + (non-interactive) demo across
# EVERY Strix Halo box, capturing a complete log bundle for offline review.
#
# Use exactly the same env as deploy-fleet-2box.sh, e.g.:
#   HALO_A_IP=10.0.0.1 HALO_B_IP=10.0.0.2 HALO_B_SSH=user@10.0.0.2 \
#   HALO_B_REPO=/home/user/semantic-router FLEET_MODE=gateway \
#     bash run-all-2box.sh
#
# Win or lose, every relevant log lands in one directory you can share. Exit code
# is the deploy/verify result. Optional: SKIP_DEMO=1 to skip the demo step.
#
# R10: this runner sets up ONE shared SSH ControlMaster socket and hands it to
# deploy-fleet-2box.sh (via FLEET_SSH_CONTROL_PATH), so the WHOLE run (deploy +
# demo + log collection) authenticates to each box ONCE instead of prompting for
# a password on every SSH/scp. Use key-based SSH (ssh-copy-id) for zero prompts.
# R3: sources an optional versions.env so the pinned router digest is captured
# into the bundle. R7: collects logs from every box in fleet.hosts (if present).
#
set -uo pipefail   # deliberately NOT -e: always reach the log-collection step
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"
REMOTE_STATE="\${TMPDIR:-/tmp}/vllm-sr-fleet"

# --- R3: optional image pin (also inherited by the child deploy) -------------
VERSIONS_ENV="${VERSIONS_ENV:-${SCRIPT_DIR}/versions.env}"
if [ -f "${VERSIONS_ENV}" ]; then
  echo "==> [run-all] sourcing image pin ${VERSIONS_ENV}"
  set -a
  # shellcheck source=/dev/null
  source "${VERSIONS_ENV}"
  set +a
fi

# --- R10: one shared SSH ControlMaster socket for the whole run --------------
# deploy-fleet-2box.sh REUSES this (FLEET_SSH_CONTROL_PATH) instead of making its
# own and does NOT tear it down; we own it and close it on exit here.
SSH_CTRL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vllm-sr-fleet-ssh.XXXXXX")"
export FLEET_SSH_CONTROL_PATH="${SSH_CTRL_DIR}/cm-%r@%h:%p"

# --- R7: resolve remote boxes for log collection (lenient copy of deploy's) --
FLEET_HOSTS_FILE="${FLEET_HOSTS_FILE:-${SCRIPT_DIR}/fleet.hosts}"
BOX_IDS=(); BOX_SSH=(); BOX_MODE=(); BOX_REPO=(); BOX_PORT=(); BOX_KEY=()
_undash() { case "${1:-}" in ""|-) printf '' ;; *) printf '%s' "$1" ;; esac; }
resolve_remote_boxes() {
  BOX_IDS=(); BOX_SSH=(); BOX_MODE=(); BOX_REPO=(); BOX_PORT=(); BOX_KEY=()
  if [ -f "${FLEET_HOSTS_FILE}" ] && grep -qvE '^[[:space:]]*(#.*)?$' "${FLEET_HOSTS_FILE}"; then
    local id ssht ip mode repo port key rest
    # shellcheck disable=SC2034  # positional read: ip/rest are column placeholders
    while read -r id ssht ip mode repo port key rest; do
      [ -z "${id:-}" ] && continue
      case "${id}" in \#*) continue ;; esac
      [ -z "${ssht:-}" ] && continue            # lenient: deploy already validated
      [ "${id}" = "halo-a" ] && continue
      BOX_IDS+=("${id}"); BOX_SSH+=("${ssht}")
      BOX_MODE+=("$(_undash "${mode:-}")"); BOX_REPO+=("$(_undash "${repo:-}")")
      BOX_PORT+=("$(_undash "${port:-}")"); BOX_KEY+=("$(_undash "${key:-}")")
    done < "${FLEET_HOSTS_FILE}"
  elif [ -n "${HALO_B_SSH:-}" ]; then
    BOX_IDS+=("halo-b"); BOX_SSH+=("${HALO_B_SSH}")
    BOX_MODE+=("${HALO_B_MODE:-${FLEET_MODE:-mock}}"); BOX_REPO+=("${HALO_B_REPO:-}")
    BOX_PORT+=("${HALO_B_SSH_PORT:-}"); BOX_KEY+=("${HALO_B_SSH_KEY:-}")
  fi
}

# shellcheck disable=SC2329  # invoked indirectly via 'trap runall_cleanup EXIT'
runall_cleanup() {
  local idx opts
  for idx in ${BOX_SSH[@]+"${!BOX_SSH[@]}"}; do
    opts=(-o ControlPath="${FLEET_SSH_CONTROL_PATH}")
    [ -n "${BOX_KEY[$idx]:-}" ] && opts+=(-i "${BOX_KEY[$idx]}")
    [ -n "${BOX_PORT[$idx]:-}" ] && opts+=(-p "${BOX_PORT[$idx]}")
    ssh "${opts[@]}" -O exit "${BOX_SSH[$idx]}" >/dev/null 2>&1 || true
  done
  rm -rf "${SSH_CTRL_DIR}" 2>/dev/null || true
}
trap runall_cleanup EXIT

RUN_DIR="${FLEET_STATE_DIR}/run-$(date +%Y%m%d-%H%M%S)"
mkdir -p "${RUN_DIR}"
MAIN_LOG="${RUN_DIR}/run-all.log"
echo "==> [run-all] mode=${FLEET_MODE:-mock}; full transcript -> ${MAIN_LOG}"

echo "==> [run-all] STEP 1/2: deploy + verify" | tee -a "${MAIN_LOG}"
bash "${SCRIPT_DIR}/deploy-fleet-2box.sh" 2>&1 | tee -a "${MAIN_LOG}"
rc="${PIPESTATUS[0]}"

if [ "${rc}" -eq 0 ] && [ "${SKIP_DEMO:-}" != "1" ]; then
  echo "==> [run-all] STEP 2/2: demo (non-interactive)" | tee -a "${MAIN_LOG}"
  bash "${SCRIPT_DIR}/demo-fleet.sh" </dev/null 2>&1 | tee -a "${MAIN_LOG}"
elif [ "${rc}" -ne 0 ]; then
  echo "==> [run-all] deploy/verify failed (rc=${rc}); skipping demo" | tee -a "${MAIN_LOG}"
fi

# --- collect logs (best-effort, regardless of outcome) ----------------------
echo "==> [run-all] collecting logs into ${RUN_DIR}"
ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
for f in fleet.env ccp.log halo-a-agent.log halo-a-router.log; do
  cp -f "${FLEET_STATE_DIR}/${f}" "${RUN_DIR}/" 2>/dev/null || true
done
# R9: capture the CCP's RAW JSON-lines audit (CCP_STATE_DIR/audit.log) into the
# bundle. Unlike the TEXT fleet-audit.txt snapshot below, this carries the agent
# write->converge timer (apply_seconds) that fleet_metrics.py needs to compute
# p50/p95 hot-reload latency; it reads <bundle>/audit.log first.
cp -f "${FLEET_STATE_DIR}/ccp/audit.log" "${RUN_DIR}/audit.log" 2>/dev/null || true
# Router CONTAINER logs (gateway mode): a hot-reload crash on the real ROCm
# router surfaces HERE, not in the serve-wrapper halo-a-router.log above.
docker logs --tail 500 vllm-sr-router-container >"${RUN_DIR}/halo-a-router-container.log" 2>&1 || true

# Load the deployment env, then resolve EVERY remote box (fleet.hosts or HALO_B_*).
if [ -f "${ENV_FILE}" ]; then
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
fi
resolve_remote_boxes

# --- R3: capture the RESOLVED router image digest per box into the bundle ----
DIGESTS="${RUN_DIR}/router-image-digests.txt"
{
  echo "# router image digest per box, resolved at run time (R3 drift evidence)"
  echo "# pinned VLLM_SR_ROUTER_IMAGE=${VLLM_SR_ROUTER_IMAGE:-<unset>}"
  if command -v docker >/dev/null 2>&1; then
    _img="$(docker inspect --format '{{.Image}}' vllm-sr-router-container 2>/dev/null || true)"
    if [ -n "${_img}" ]; then
      _dg="$(docker inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "${_img}" 2>/dev/null || true)"
      echo "halo-a ${_dg:-${_img}}"
    else
      echo "halo-a (no router container; mock mode or not running)"
    fi
  fi
} >"${DIGESTS}"

# --- R7/R10: pull each remote box's agent + router logs and digest -----------
# All SSH/scp reuse the shared ControlMaster => no extra password prompts.
for idx in ${BOX_SSH[@]+"${!BOX_SSH[@]}"}; do
  id="${BOX_IDS[$idx]}"; target="${BOX_SSH[$idx]}"
  sshopts=(-o ControlMaster=auto -o ControlPath="${FLEET_SSH_CONTROL_PATH}" -o ControlPersist=10m)
  [ -n "${BOX_KEY[$idx]:-}" ] && sshopts+=(-i "${BOX_KEY[$idx]}")
  [ -n "${BOX_PORT[$idx]:-}" ] && sshopts+=(-p "${BOX_PORT[$idx]}")
  echo "    [${id}] collecting logs from ${target}"
  ssh "${sshopts[@]}" "${target}" "cat ${REMOTE_STATE}/${id}-agent.log" \
    >"${RUN_DIR}/${id}-agent.log" 2>/dev/null || true
  ssh "${sshopts[@]}" "${target}" "docker logs --tail 500 vllm-sr-router-container 2>&1" \
    >"${RUN_DIR}/${id}-router-container.log" 2>/dev/null || true
  _rdg="$(ssh "${sshopts[@]}" "${target}" \
    'i=$(docker inspect --format "{{.Image}}" vllm-sr-router-container 2>/dev/null); docker inspect --format "{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}" "$i" 2>/dev/null' \
    2>/dev/null || true)"
  echo "${id} ${_rdg:-(no router container)}" >>"${DIGESTS}"
done
echo "    router image digests -> ${DIGESTS}"

PYBIN="$(fleet_pybin)"
fctl() { CCP_URL="${CCP_URL:-http://localhost:9300}" FLEET_TOKEN="${FLEET_TOKEN:-}" \
         "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" "$@"; }
fctl status >"${RUN_DIR}/fleet-status.txt" 2>&1 || true
fctl audit  >"${RUN_DIR}/fleet-audit.txt"  2>&1 || true

# Distil the bundle into machine-readable metrics (convergence latency, hash
# agreement, router readiness, config size) -> metrics.json + metrics.txt. This
# turns every hardware run into a structured, paper-grade data point. See
# docs/research-pipeline.md for the metric definitions.
# CCP_AUDIT_LOG points fleet_metrics.py at the live raw JSON audit as a fallback
# if the in-bundle copy above is missing (R9 p50/p95 latency source).
CCP_URL="${CCP_URL:-http://localhost:9300}" FLEET_TOKEN="${FLEET_TOKEN:-}" \
CCP_AUDIT_LOG="${FLEET_STATE_DIR}/ccp/audit.log" \
  "${PYBIN}" "${SCRIPT_DIR}/fleet_metrics.py" --bundle "${RUN_DIR}" 2>&1 \
  | tee "${RUN_DIR}/metrics.txt" || true

# Optional: fleet-wide PERF benchmarks (Test 1 co-location overhead + Test 2
# inference-server comparison). Opt-in because they stop/restart the local stack
# and take time; results (overhead-*/server-*/perf-metrics.json/perf-summary.md)
# land in the SAME run bundle. See perf/README.md.
if [ "${PERF_BENCH:-0}" = "1" ]; then
  echo "==> [run-all] PERF_BENCH=1: fleet-wide perf benchmarks" | tee -a "${MAIN_LOG}"
  BUNDLE="${RUN_DIR}" bash "${SCRIPT_DIR}/perf/run-perf-fleet.sh" 2>&1 | tee -a "${MAIN_LOG}" || true
fi

echo
echo "=============================================================="
if [ "${rc}" -eq 0 ]; then
  echo "PASS: deploy + verify completed (mode=${FLEET_MODE:-mock})."
else
  echo "FAIL: deploy/verify returned ${rc}."
fi
echo "Log bundle (share this whole directory if anything failed):"
echo "  ${RUN_DIR}"
ls -1 "${RUN_DIR}" 2>/dev/null | sed 's/^/    /'
echo "=============================================================="
exit "${rc}"
