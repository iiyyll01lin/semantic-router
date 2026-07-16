#!/usr/bin/env bash
#
# run-hardware-validation.sh -- ONE-SHOT, OPT-IN orchestrator that turns the
# offline 20/20 (verify_local.py) into on-hardware evidence for the CCP
# production-hardening on the real Strix Halo fleet, WITHOUT any copy-paste.
#
# It is a THIN wrapper: it mirrors docs/hardware-validation-runbook.md Steps 0-6
# (the verify-hardening.sh path) plus the optional warm-standby drill (Step 7),
# REUSING the existing scripts rather than reimplementing any of them --
#   verify_local.py      (Step 0 offline gate)
#   run-all-2box.sh      (Steps 1 + 2d deploy/verify/demo + evidence bundle)
#   _ed25519.py          (Step 2a selftest + keypair)
#   make-mtls-certs.sh   (Step 2a CA + server + per-agent client certs)
#   verify-hardening.sh  (Steps 2-6 on-hardware verifier)
#   ccp-standby-sync.sh + promote-standby.sh (Step 7 warm-standby drill)
#
# Everything it enables is OPT-IN; run it FROM the recipe dir ON Halo-A:
#
#   HALO_A_IP=192.0.2.10 HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 \
#   HALO_B_REPO=/home/ubuntu/yy/workspace/semantic-router \
#     bash run-hardware-validation.sh
#
# Modes / opt-in flags (all default OFF):
#   DRY_RUN=1            print the ordered plan + check prereqs; touch NO hardware
#   RUN_REGRESSION=1     also run the Step 1 default-flow run-all-2box.sh baseline
#   RUN_STANDBY_DRILL=1  also run the Step 7 warm-standby promotion drill
#                        (requires STANDBY_SSH + STANDBY_HOST)
#   FORCE=1              re-mint keys/certs even if ./keys + ./mtls-certs exist
#
# Required env (fail-fast; see the runbook Prerequisites on any miss):
#   HALO_A_IP    address of THIS box (Halo-A) reachable from the remotes (CCP + cert SAN)
#   HALO_B_IP    address of Halo-B (informational baseline)
#   HALO_B_SSH   user@host control address for Halo-B
#   HALO_B_REPO  semantic-router repo path on Halo-B (gateway mode)
#
# Selected optional env (see the reused scripts for the full surface):
#   FLEET_MODE / HALO_A_MODE / HALO_B_MODE   default gateway (validation drives
#                                            REAL routers: R8 rollback / R1 drift)
#   STANDBY_SSH / STANDBY_HOST               warm-standby ssh target / agent-facing addr
#   STANDBY_STATE_DIR / PROMOTE_WAIT         passed through to the Step 7 scripts
#   FLEET_HOSTS_FILE                         N-box inventory (default ./fleet.hosts)
#
# Authoring-time validation ONLY (this is NOT a hardware run): a syntax check
# (bash -n), static analysis (ShellCheck), and a DRY_RUN=1 invocation. The
# on-hardware execution stays YOUR step on Halo-A; it is never run against real
# boxes from CI / the authoring environment.
#
set -uo pipefail   # deliberately NOT -e: always reach evidence collection
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}" || { echo "ERROR: cannot cd to ${SCRIPT_DIR}" >&2; exit 1; }
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/fleet_common.sh"
PYBIN="$(fleet_pybin)"

RUNBOOK="docs/hardware-validation-runbook.md"

# --- opt-in flags / config ---------------------------------------------------
DRY_RUN="${DRY_RUN:-0}"
RUN_REGRESSION="${RUN_REGRESSION:-0}"
RUN_STANDBY_DRILL="${RUN_STANDBY_DRILL:-0}"
FORCE="${FORCE:-0}"

KEYS_DIR="${SCRIPT_DIR}/keys"
MTLS_DIR="${SCRIPT_DIR}/mtls-certs"

# Hardware validation drives REAL routers (R8 auto-rollback / R1 drift-heal need
# gateway mode); default to it but let the operator override per box.
export FLEET_MODE="${FLEET_MODE:-gateway}"
export HALO_A_MODE="${HALO_A_MODE:-${FLEET_MODE}}"
export HALO_B_MODE="${HALO_B_MODE:-${FLEET_MODE}}"

OVERALL_RC=0
EVIDENCE_DONE=0

ts()   { date +%Y%m%d-%H%M%S; }
say()  { echo "==> [hw-validate] $*"; }
step() {
  echo
  echo "======================================================================"
  echo "== $*"
  echo "======================================================================"
}
die()  {
  echo "ERROR: $*" >&2
  echo "       See ${SCRIPT_DIR}/${RUNBOOK} (Prerequisites / the relevant Step)." >&2
  exit 1
}

# --- required env / prereqs --------------------------------------------------
require_env() {
  local v miss=0
  for v in HALO_A_IP HALO_B_IP HALO_B_SSH HALO_B_REPO; do
    if [ -z "${!v:-}" ]; then
      echo "ERROR: required env ${v} is not set" >&2
      miss=1
    fi
  done
  [ "${miss}" = 0 ] || die "set HALO_A_IP HALO_B_IP HALO_B_SSH HALO_B_REPO (fleet addresses)"
}

REQUIRED_SCRIPTS=(verify_local.py _ed25519.py make-mtls-certs.sh run-all-2box.sh
                  verify-hardening.sh ccp-standby-sync.sh promote-standby.sh)
# $1 = "fatal" (die on any miss) | "report" (list only, never fail)
check_prereqs() {
  local mode="$1" s t missing=0
  say "checking reusable scripts + tools"
  for s in "${REQUIRED_SCRIPTS[@]}"; do
    if [ -f "${SCRIPT_DIR}/${s}" ]; then echo "    [ok]   ${s}"; else echo "    [MISS] ${s}"; missing=1; fi
  done
  for t in "${PYBIN}" ssh scp openssl; do
    if command -v "${t}" >/dev/null 2>&1; then echo "    [ok]   ${t}"; else echo "    [MISS] ${t}"; missing=1; fi
  done
  if [ "${missing}" != 0 ] && [ "${mode}" = fatal ]; then
    die "missing reusable script(s) or tool(s) listed above"
  fi
}

# --- N-box resolution (lenient copy of run-all-2box.sh's) --------------------
FLEET_HOSTS_FILE="${FLEET_HOSTS_FILE:-${SCRIPT_DIR}/fleet.hosts}"
BOX_IDS=(); BOX_SSH=(); BOX_PORT=(); BOX_KEY=(); BOX_SOURCE=""
_undash() { case "${1:-}" in ""|-) printf '' ;; *) printf '%s' "$1" ;; esac; }
resolve_remote_boxes() {
  BOX_IDS=(); BOX_SSH=(); BOX_PORT=(); BOX_KEY=()
  if [ -f "${FLEET_HOSTS_FILE}" ] && grep -qvE '^[[:space:]]*(#.*)?$' "${FLEET_HOSTS_FILE}"; then
    BOX_SOURCE="${FLEET_HOSTS_FILE}"
    local id ssht ip mode repo port key rest
    # shellcheck disable=SC2034  # positional read: ip/mode/repo/rest are column placeholders
    while read -r id ssht ip mode repo port key rest; do
      [ -z "${id:-}" ] && continue
      case "${id}" in \#*) continue ;; esac
      [ "${id}" = "halo-a" ] && continue
      [ -z "${ssht:-}" ] && continue
      BOX_IDS+=("${id}"); BOX_SSH+=("${ssht}")
      BOX_PORT+=("$(_undash "${port:-}")"); BOX_KEY+=("$(_undash "${key:-}")")
    done < "${FLEET_HOSTS_FILE}"
  fi
  if [ "${#BOX_IDS[@]}" -eq 0 ]; then
    BOX_SOURCE="HALO_B_* env (no active fleet.hosts)"
    BOX_IDS+=("halo-b"); BOX_SSH+=("${HALO_B_SSH}")
    BOX_PORT+=("${HALO_B_SSH_PORT:-}"); BOX_KEY+=("${HALO_B_SSH_KEY:-}")
  fi
}

# "halo-a" (implicit CCP + first edge) plus every resolved remote box id.
agents_list() {
  local a="halo-a" i
  for i in "${!BOX_IDS[@]}"; do a="${a} ${BOX_IDS[$i]}"; done
  printf '%s' "${a}"
}

# --- ordered plan (printed in DRY_RUN and as a preamble to a real run) -------
print_plan() {
  local agents; agents="$(agents_list)"
  cat <<EOF
Ordered plan (mirrors ${RUNBOOK} Steps 0-7):
  remote boxes:   ${BOX_IDS[*]}   [source: ${BOX_SOURCE}]
  cert agents:    ${agents}
  fleet mode:     HALO_A_MODE=${HALO_A_MODE} HALO_B_MODE=${HALO_B_MODE}
  state dir:      ${FLEET_STATE_DIR}
  opt-in flags:   RUN_REGRESSION=${RUN_REGRESSION} RUN_STANDBY_DRILL=${RUN_STANDBY_DRILL} FORCE=${FORCE}

  Step 0   python3 verify_local.py                     -> require 20/20 (abort otherwise)
  Step 1   [RUN_REGRESSION] run-all-2box.sh            -> default-flow (HMAC/HTTP) baseline
  Step 2a  python3 _ed25519.py selftest + keygen       -> ./keys (reuse if present unless FORCE=1)
           make-mtls-certs.sh --host ${HALO_A_IP} --agents "${agents}"  -> ./mtls-certs
  Step 2b  scp pub key + CA + per-box client cert/key  -> each remote (~/keys, ~/mtls-certs)
  Step 2c  export Ed25519 + TLS + mTLS env (CCP + agent side)
  Step 2d  run-all-2box.sh (secure redeploy), then
           FLEET_VERIFY_DRIFT_ON_GATEWAY=1 FLEET_VERIFY_STANDBY=1 verify-hardening.sh (covers Steps 2-6)
  Step 7   [RUN_STANDBY_DRILL] ccp-standby-sync.sh + promote-standby.sh
           (needs STANDBY_SSH/STANDBY_HOST; starting the standby ccp_server.py is a MANUAL step)
  Evidence tee'd logs collected into the newest run-* bundle under ${FLEET_STATE_DIR}
EOF
}

# --- Step 2b: stage per-box material on each remote --------------------------
stage_certs() {
  local idx id target sopts popts
  for idx in "${!BOX_IDS[@]}"; do
    id="${BOX_IDS[$idx]}"; target="${BOX_SSH[$idx]}"
    sopts=(); popts=()
    if [ -n "${BOX_KEY[$idx]:-}" ]; then sopts+=(-i "${BOX_KEY[$idx]}"); popts+=(-i "${BOX_KEY[$idx]}"); fi
    if [ -n "${BOX_PORT[$idx]:-}" ]; then sopts+=(-p "${BOX_PORT[$idx]}"); popts+=(-P "${BOX_PORT[$idx]}"); fi
    say "staging Ed25519 pub + CA + '${id}' client cert/key on ${target}"
    ssh ${sopts[@]+"${sopts[@]}"} "${target}" 'mkdir -p ~/keys ~/mtls-certs' \
      || die "ssh ${target}: could not create ~/keys ~/mtls-certs"
    scp ${popts[@]+"${popts[@]}"} "${KEYS_DIR}/ccp_ed25519.pub"      "${target}:~/keys/"       || die "scp pub key -> ${target} failed"
    scp ${popts[@]+"${popts[@]}"} "${MTLS_DIR}/ca-cert.pem"          "${target}:~/mtls-certs/" || die "scp CA -> ${target} failed"
    scp ${popts[@]+"${popts[@]}"} "${MTLS_DIR}/${id}-client-cert.pem" "${target}:~/mtls-certs/" || die "scp ${id} client cert -> ${target} failed"
    scp ${popts[@]+"${popts[@]}"} "${MTLS_DIR}/${id}-client-key.pem"  "${target}:~/mtls-certs/" || die "scp ${id} client key -> ${target} failed"
  done
}

# --- Step 2c: export the exact security env from the runbook (2c) ------------
export_security_env() {
  # CCP side (Halo-A only): private seed + server cert + client-CA (mTLS).
  export FLEET_SIGN_MODE=ed25519
  export FLEET_ED25519_SECRET_FILE="${KEYS_DIR}/ccp_ed25519.seed"
  export CCP_TLS_CERT="${MTLS_DIR}/ccp-cert.pem"
  export CCP_TLS_KEY="${MTLS_DIR}/ccp-key.pem"
  export CCP_TLS_CLIENT_CA="${MTLS_DIR}/ca-cert.pem"
  # Agent side (forwarded to remotes by the deploy; local paths on Halo-A).
  export FLEET_ED25519_PUBLIC_FILE="${KEYS_DIR}/ccp_ed25519.pub"
  export FLEET_TLS_CA="${MTLS_DIR}/ca-cert.pem"
  export FLEET_TLS_CLIENT_CERT="${MTLS_DIR}/halo-a-client-cert.pem"
  export FLEET_TLS_CLIENT_KEY="${MTLS_DIR}/halo-a-client-key.pem"
}

# --- evidence collection (best-effort; runs inline + as an EXIT safety net) --
# shellcheck disable=SC2329  # invoked inline AND indirectly via 'trap ... EXIT'
collect_evidence() {
  [ "${DRY_RUN}" = "1" ] && return 0
  [ "${EVIDENCE_DONE}" = "1" ] && return 0
  local run f
  run="$(ls -dt "${FLEET_STATE_DIR}"/run-* 2>/dev/null | head -1 || true)"
  # Nothing produced yet (e.g. an early pre-flight abort) -> stay silent.
  if [ -z "${run}" ] \
     && ! ls "${FLEET_STATE_DIR}"/verify-hardening-*.log >/dev/null 2>&1 \
     && ! ls "${FLEET_STATE_DIR}"/verify_local-*.txt      >/dev/null 2>&1; then
    return 0
  fi
  EVIDENCE_DONE=1
  step "Evidence"
  if [ -n "${run}" ]; then
    say "newest run bundle: ${run}"
    for f in "${FLEET_STATE_DIR}"/verify_local-*.txt \
             "${FLEET_STATE_DIR}"/verify-hardening-*.log \
             "${FLEET_STATE_DIR}"/run-all-*.log \
             "${FLEET_STATE_DIR}"/promote-standby-*.log \
             "${FLEET_STATE_DIR}"/standby-converge-*.log; do
      [ -e "${f}" ] && cp -f "${f}" "${run}/" 2>/dev/null
    done
    say "driver logs copied in; archive the whole directory:"
    echo "    ${run}"
    ls -1 "${run}" 2>/dev/null | sed 's/^/      /'
  else
    say "no run-* bundle yet; driver logs live under ${FLEET_STATE_DIR}"
  fi
  cat <<EOF

  Evidence checklist (see ${RUNBOOK} "Evidence bundle checklist"):
    - verify_local.py 20/20 output (Step 0)
    - run-all.log + "PASS: deploy + verify completed" (Step 1, if run)
    - verify-hardening-*.log PASS/SKIP block (Steps 2-6)
    - fleet-status.txt / fleet-audit.txt / audit.log (converged hash + rows)
    - metrics.txt + metrics.json (hot_reload_latency_seconds p50/p95, R9)
    - router-image-digests.txt (same pinned image per box, R3)
    - promote-standby log + ccp-standby-sync.status + fleet.env.bak (Step 7, if run)
EOF
}
trap collect_evidence EXIT

# ===========================================================================
# main
# ===========================================================================
step "run-hardware-validation.sh (DRY_RUN=${DRY_RUN}, mode=${FLEET_MODE})"
require_env
resolve_remote_boxes

if [ "${DRY_RUN}" = "1" ]; then
  say "DRY_RUN=1: printing the plan + checking prerequisites; NOTHING touches hardware"
  echo
  print_plan
  echo
  check_prereqs report
  if [ "${FORCE}" != "1" ] && [ -f "${KEYS_DIR}/ccp_ed25519.seed" ] && [ -f "${MTLS_DIR}/ca-cert.pem" ]; then
    say "existing ./keys + ./mtls-certs detected -> Step 2a would REUSE them (FORCE=1 to re-mint)"
  else
    say "no ./keys + ./mtls-certs (or FORCE=1) -> Step 2a would mint them"
  fi
  if [ "${RUN_STANDBY_DRILL}" = "1" ]; then
    if [ -n "${STANDBY_SSH:-}" ] && [ -n "${STANDBY_HOST:-}" ]; then
      say "Step 7 standby drill armed (STANDBY_SSH=${STANDBY_SSH} STANDBY_HOST=${STANDBY_HOST})"
    else
      say "WARN: RUN_STANDBY_DRILL=1 but STANDBY_SSH/STANDBY_HOST unset -> Step 7 would abort"
    fi
  fi
  echo
  say "DRY_RUN complete: prereq/plan path OK (no hardware touched)."
  exit 0
fi

check_prereqs fatal
say "plan for this run:"
print_plan

# --- Step 0: offline gate ----------------------------------------------------
step "Step 0 -- offline gate: python3 verify_local.py (require 20/20)"
VL_OUT="${FLEET_STATE_DIR}/verify_local-$(date +%Y%m%d).txt"
"${PYBIN}" "${SCRIPT_DIR}/verify_local.py" 2>&1 | tee "${VL_OUT}" || true
if grep -q "20/20 checks passed" "${VL_OUT}"; then
  say "Step 0 PASS: verify_local.py 20/20 (saved ${VL_OUT})"
else
  die "verify_local.py did NOT report 20/20 (see ${VL_OUT}); fix the offline proof before spending hardware time"
fi

# --- Step 1: default-flow regression (opt-in) --------------------------------
if [ "${RUN_REGRESSION}" = "1" ]; then
  step "Step 1 -- default-flow regression: run-all-2box.sh (HMAC/HTTP baseline)"
  # Force the DEFAULT flow: unset any security env so the baseline is byte-identical
  # to today's default (the secure redeploy happens later in Step 2d).
  ( unset FLEET_SIGN_MODE FLEET_ED25519_SECRET_FILE FLEET_ED25519_PUBLIC_FILE \
          CCP_TLS_CERT CCP_TLS_KEY CCP_TLS_CLIENT_CA \
          FLEET_TLS_CA FLEET_TLS_CLIENT_CERT FLEET_TLS_CLIENT_KEY
    bash "${SCRIPT_DIR}/run-all-2box.sh" ) 2>&1 | tee "${FLEET_STATE_DIR}/run-all-baseline-$(ts).log"
  rc="${PIPESTATUS[0]}"
  [ "${rc}" -eq 0 ] || die "Step 1 baseline run-all-2box.sh failed (rc=${rc}); fix the default flow first"
  say "Step 1 PASS: default-flow baseline converged both routers"
else
  say "Step 1 SKIP: default-flow regression (set RUN_REGRESSION=1 to enable)"
fi

# --- Step 2a: mint / validate Ed25519 keypair + mTLS material ----------------
step "Step 2a -- Ed25519 keypair + mTLS material"
say "validating the vendored Ed25519 implementation (RFC 8032 selftest)"
"${PYBIN}" "${SCRIPT_DIR}/_ed25519.py" selftest || die "_ed25519.py selftest failed"

if [ "${FORCE}" != "1" ] && [ -f "${KEYS_DIR}/ccp_ed25519.seed" ]; then
  say "reusing existing Ed25519 seed ${KEYS_DIR}/ccp_ed25519.seed (FORCE=1 to re-mint)"
else
  say "generating Ed25519 keypair -> ${KEYS_DIR}"
  "${PYBIN}" "${SCRIPT_DIR}/_ed25519.py" keygen --out-dir "${KEYS_DIR}" || die "_ed25519.py keygen failed"
fi

MINT_FORCE=()
[ "${FORCE}" = "1" ] && MINT_FORCE+=(--force)
if [ "${FORCE}" != "1" ] && [ -f "${MTLS_DIR}/ca-cert.pem" ]; then
  say "reusing existing mTLS CA ${MTLS_DIR}/ca-cert.pem; (re)issuing leaf certs for: $(agents_list)"
else
  say "minting mTLS CA + server + client certs -> ${MTLS_DIR}"
fi
bash "${SCRIPT_DIR}/make-mtls-certs.sh" --host "${HALO_A_IP}" --agents "$(agents_list)" \
  ${MINT_FORCE[@]+"${MINT_FORCE[@]}"} || die "make-mtls-certs.sh failed"

# --- Step 2b: stage per-box files on each remote -----------------------------
step "Step 2b -- stage pub key + CA + per-box client cert/key on each remote"
stage_certs
say "Step 2b done: staged material on -> ${BOX_IDS[*]}"

# --- Step 2c: export the security env (drives the secure deploy + verifier) --
step "Step 2c -- export Ed25519 + TLS + mTLS env"
export_security_env
say "exported FLEET_SIGN_MODE=ed25519, FLEET_ED25519_*_FILE, CCP_TLS_*, FLEET_TLS_*"
cat <<EOF
    NOTE: the deploy forwards the AGENT-side var NAMES to each remote with THIS
    box's values. If a remote's home paths differ from Halo-A's, override
    FLEET_ED25519_PUBLIC_FILE / FLEET_TLS_CA / FLEET_TLS_CLIENT_CERT /
    FLEET_TLS_CLIENT_KEY to the staged remote paths (see ${RUNBOOK} 2c).
EOF

# --- Step 2d: secure redeploy + on-hardware verifier (covers Steps 2-6) ------
step "Step 2d -- secure redeploy (run-all-2box.sh) + verify-hardening.sh"
say "secure redeploy under Ed25519 + TLS + mTLS"
bash "${SCRIPT_DIR}/run-all-2box.sh" 2>&1 | tee "${FLEET_STATE_DIR}/run-all-secure-$(ts).log"
rc="${PIPESTATUS[0]}"
[ "${rc}" -eq 0 ] || die "secure run-all-2box.sh failed (rc=${rc}); see the log + ${RUNBOOK} Step 2d"

VH_LOG="${FLEET_STATE_DIR}/verify-hardening-$(ts).log"
say "running the opt-in hardware verifier (Steps 2-6) -> ${VH_LOG}"
FLEET_VERIFY_DRIFT_ON_GATEWAY=1 FLEET_VERIFY_STANDBY=1 \
  bash "${SCRIPT_DIR}/verify-hardening.sh" 2>&1 | tee "${VH_LOG}"
VERIFY_RC="${PIPESTATUS[0]}"
if [ "${VERIFY_RC}" -eq 0 ]; then
  say "Steps 2-6 PASS: verify-hardening.sh reported all checks passed"
else
  say "WARN: verify-hardening.sh returned ${VERIFY_RC}; inspect ${VH_LOG} (evidence still collected)"
  OVERALL_RC="${VERIFY_RC}"
fi

# --- Step 7: warm-standby promotion drill (opt-in) ---------------------------
if [ "${RUN_STANDBY_DRILL}" = "1" ]; then
  step "Step 7 -- warm-standby promotion drill"
  [ -n "${STANDBY_SSH:-}" ]  || die "RUN_STANDBY_DRILL=1 needs STANDBY_SSH (ssh target of the standby box)"
  [ -n "${STANDBY_HOST:-}" ] || die "RUN_STANDBY_DRILL=1 needs STANDBY_HOST (address agents will reach the standby on)"
  ENV_FILE="${FLEET_STATE_DIR}/fleet.env"
  [ -f "${ENV_FILE}" ] || die "no ${ENV_FILE}; Step 2d must run first (it writes fleet.env)"

  say "final zero-RPO sync of the CCP state dir -> ${STANDBY_SSH}"
  if ! STANDBY_HOST="${STANDBY_SSH}" SYNC_ONCE=1 bash "${SCRIPT_DIR}/ccp-standby-sync.sh"; then
    say "WARN: final standby sync failed; the promotion may see stale/short audit"
    OVERALL_RC=1
  fi
  cat "${FLEET_STATE_DIR}/ccp-standby-sync.status" 2>/dev/null || true

  # Capture the ACTIVE audit_count BEFORE stopping it (used for zero-loss check).
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  EXPECT="$("${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" status 2>/dev/null \
           | sed -n 's/.*audit_count=\([0-9]*\).*/\1/p' | head -n1)"
  say "captured active audit_count=${EXPECT:-<unknown>}"

  say "stopping the ACTIVE CCP (planned/graceful failover)"
  fleet_stop_pidfile "${FLEET_STATE_DIR}/ccp.pid" 2>/dev/null || true

  cat <<EOF

  MANUAL STEP (NOT auto-run): start the standby CCP ON ${STANDBY_SSH}, from the
  recipe dir, against the SYNCED state with the SAME FLEET_TOKEN + keys/TLS, e.g.:

    export CCP_STATE_DIR=${STANDBY_STATE_DIR:-${FLEET_STATE_DIR}/ccp} FLEET_TOKEN=<same-as-active> CCP_HOST=0.0.0.0
    export FLEET_SIGN_MODE=ed25519 FLEET_ED25519_SECRET_FILE=~/keys/ccp_ed25519.seed
    export CCP_TLS_CERT=~/mtls-certs/ccp-cert.pem CCP_TLS_KEY=~/mtls-certs/ccp-key.pem
    export CCP_TLS_CLIENT_CA=~/mtls-certs/ca-cert.pem
    python3 ccp_server.py      # _restore()s the replicated desired + audit

  See ${RUNBOOK} Step 7 for the full drill.
EOF
  if [ -t 0 ]; then
    read -r -p "  Press ENTER once the standby ccp_server is serving (Ctrl-C to abort)... " || true
  else
    say "non-interactive: promote-standby.sh will wait up to PROMOTE_WAIT=${PROMOTE_WAIT:-60}s for the standby"
  fi

  say "promoting the standby -> active"
  STANDBY_HOST="${STANDBY_HOST}" EXPECT_AUDIT_COUNT="${EXPECT:-}" PROMOTE_SINCE="$(date +%s)" \
    bash "${SCRIPT_DIR}/promote-standby.sh" 2>&1 | tee "${FLEET_STATE_DIR}/promote-standby-$(ts).log"
  prc="${PIPESTATUS[0]}"
  if [ "${prc}" -eq 0 ]; then
    say "Step 7 PASS: promotion completed (see the promote log for RTO + zero-audit-loss)"
  else
    say "WARN: promote-standby.sh returned ${prc}; see the promote log"
    OVERALL_RC="${prc}"
  fi

  # Confirm the fleet reconverges against the promoted standby (best-effort).
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  "${PYBIN}" "${SCRIPT_DIR}/fleetctl.py" wait-converged \
    --boxes "${FLEET_BOXES:-halo-a}" --timeout 120 2>&1 \
    | tee "${FLEET_STATE_DIR}/standby-converge-$(ts).log" || true
else
  say "Step 7 SKIP: warm-standby drill (set RUN_STANDBY_DRILL=1 + STANDBY_SSH/STANDBY_HOST)"
fi

# --- evidence + summary ------------------------------------------------------
collect_evidence

step "Done"
if [ "${OVERALL_RC}" -eq 0 ]; then
  say "hardware validation completed; verify-hardening reported all checks passed"
else
  say "hardware validation completed WITH FAILURES (rc=${OVERALL_RC}); inspect the logs above"
fi
say "record the results in the README hardware note per ${RUNBOOK} Step 8"
exit "${OVERALL_RC}"
