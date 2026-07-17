#!/usr/bin/env bash
#
# run-tests.sh -- the single authoring/CI gate for the strix-halo-fleet-2box
# recipe. Fast and fully OFFLINE: static checks plus the stdlib regression
# tests. It intentionally does NOT run verify_local.py or a real deploy (those
# belong to the hardware-validation path), so it is safe to run on any box.
#
# Steps (a failure in ANY step fails the whole run):
#   1. bash -n            on every top-level *.sh          (syntax)
#   2. python3 -m py_compile on every top-level *.py       (syntax)
#   3. shellcheck         on every top-level *.sh          (skipped if absent)
#   4. python3 test_fleet_metrics.py                        (metrics unit tests)
#   5. bash    test-remote-agent-env.sh                     (env forwarding test)
#
# Scope note: only the recipe's own top-level scripts are linted (the perf/ and
# docs/ trees are separate concerns), keeping this gate quick and deterministic.
#
# Run:  bash run-tests.sh     # exits nonzero if anything failed

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}" || exit 1

PYBIN="python3"
command -v "${PYBIN}" >/dev/null 2>&1 || PYBIN="python"

pass_steps=0
fail_steps=0
skip_steps=0

hr() { printf -- '----------------------------------------------------------------\n'; }

step_result() {  # <status> <label>
  case "$1" in
    PASS) pass_steps=$((pass_steps + 1)) ;;
    FAIL) fail_steps=$((fail_steps + 1)) ;;
    SKIP) skip_steps=$((skip_steps + 1)) ;;
  esac
  printf '[%s] %s\n' "$1" "$2"
}

# Top-level recipe scripts only (non-recursive).
shopt -s nullglob
sh_files=( *.sh )
py_files=( *.py )
shopt -u nullglob

# --- Step 1: bash -n on every *.sh ------------------------------------------
hr; echo "STEP 1/5: bash -n (shell syntax) on ${#sh_files[@]} *.sh file(s)"
bad=""
for f in "${sh_files[@]}"; do
  if bash -n "${f}" 2>/tmp/run-tests-bashn.$$; then
    printf '  ok   %s\n' "${f}"
  else
    printf '  FAIL %s\n' "${f}"; sed 's/^/       /' /tmp/run-tests-bashn.$$
    bad="${bad} ${f}"
  fi
done
rm -f /tmp/run-tests-bashn.$$ 2>/dev/null || true
if [ -n "${bad}" ]; then step_result FAIL "bash -n (offending:${bad})"; else step_result PASS "bash -n on all *.sh"; fi

# --- Step 2: py_compile on every *.py ---------------------------------------
hr; echo "STEP 2/5: ${PYBIN} -m py_compile (python syntax) on ${#py_files[@]} *.py file(s)"
bad=""
for f in "${py_files[@]}"; do
  if "${PYBIN}" -m py_compile "${f}" 2>/tmp/run-tests-pyc.$$; then
    printf '  ok   %s\n' "${f}"
  else
    printf '  FAIL %s\n' "${f}"; sed 's/^/       /' /tmp/run-tests-pyc.$$
    bad="${bad} ${f}"
  fi
done
rm -f /tmp/run-tests-pyc.$$ 2>/dev/null || true
if [ -n "${bad}" ]; then step_result FAIL "py_compile (offending:${bad})"; else step_result PASS "py_compile on all *.py"; fi

# --- Step 3: shellcheck on every *.sh (if installed) ------------------------
# Severity is 'error' by default: the recipe's shipped scripts carry a few
# pre-existing warning/info-level findings (e.g. fleet_common.sh SC2034) that
# are outside this test workstream's scope, and `bash -n` above already covers
# plain syntax. Tighten locally with SHELLCHECK_SEVERITY=warning (or style) to
# audit those. This still fails the gate on any genuine shellcheck ERROR.
SHELLCHECK_SEVERITY="${SHELLCHECK_SEVERITY:-error}"
hr; echo "STEP 3/5: shellcheck (shell lint, --severity=${SHELLCHECK_SEVERITY}) on ${#sh_files[@]} *.sh file(s)"
if command -v shellcheck >/dev/null 2>&1; then
  bad=""
  for f in "${sh_files[@]}"; do
    if shellcheck -x -S "${SHELLCHECK_SEVERITY}" "${f}"; then
      printf '  ok   %s\n' "${f}"
    else
      printf '  FAIL %s\n' "${f}"
      bad="${bad} ${f}"
    fi
  done
  if [ -n "${bad}" ]; then step_result FAIL "shellcheck (offending:${bad})"; else step_result PASS "shellcheck on all *.sh"; fi
else
  step_result SKIP "shellcheck not installed (skipping shell lint)"
fi

# --- Step 4: fleet_metrics unit tests ---------------------------------------
hr; echo "STEP 4/5: ${PYBIN} test_fleet_metrics.py"
if "${PYBIN}" test_fleet_metrics.py; then
  step_result PASS "test_fleet_metrics.py"
else
  step_result FAIL "test_fleet_metrics.py"
fi

# --- Step 5: remote_agent_env forwarding test -------------------------------
hr; echo "STEP 5/5: bash test-remote-agent-env.sh"
if bash test-remote-agent-env.sh; then
  step_result PASS "test-remote-agent-env.sh"
else
  step_result FAIL "test-remote-agent-env.sh"
fi

# --- Summary ----------------------------------------------------------------
hr
echo "SUMMARY: ${pass_steps} passed, ${fail_steps} failed, ${skip_steps} skipped"
if [ "${fail_steps}" -gt 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: PASS"
exit 0
