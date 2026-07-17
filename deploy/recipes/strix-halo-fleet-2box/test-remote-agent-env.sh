#!/usr/bin/env bash
# shellcheck disable=SC2088
# ^ This test deliberately works with LITERAL, UNEXPANDED '~' paths (the whole
#   point is that '~' is forwarded verbatim and expands on the REMOTE shell), so
#   SC2088 ("tilde does not expand in quotes") is expected everywhere below.
#
# test-remote-agent-env.sh -- OFFLINE guard for deploy-fleet-2box.sh
# remote_agent_env(). Two independent regressions are locked in here:
#
#   A) `set -e` step-4 abort: the step-4 name-logging once built _fwd_names with
#      a command substitution whose subshell exited non-zero when the LAST
#      forwarded var (an R8 extra) was unset, tripping `set -euo pipefail`
#      before any remote was provisioned. (Sections [1/6]-[4/6].)
#   B) Per-box staged path forwarding: with FLEET_REMOTE_STAGED=1 the four
#      path-valued agent vars must be emitted as RAW, UNQUOTED, HOME-relative
#      '~' paths (each resolved to THAT box's own staged material) so '~'
#      expands on the REMOTE shell -- and must NOT be mangled into '\~' by
#      `printf %q`. With FLEET_REMOTE_STAGED unset the forwarding is the
#      byte-identical `printf %q` verbatim local path. (Sections [5/6]-[6/6].)
#
# It sources deploy-fleet-2box.sh with FLEET_DEPLOY_LIB_ONLY=1 (which exposes
# remote_agent_env + the forwarded-var lists WITHOUT running the deploy). This
# also turns on `set -euo pipefail`, so every check runs under errexit exactly
# like the real step 4.
#
# No network, no SSH, no hardware. Run: bash test-remote-agent-env.sh
set -euo pipefail

SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="${SELFTEST_DIR}/deploy-fleet-2box.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
note() { echo "     $*"; }

# Counted PASS/FAIL assertions for the path-forwarding sections (these do NOT
# exit on failure -- they tally, so every assertion is reported -- and always
# return 0 so the surrounding `set -e` never aborts on a failed assertion).
PF_PASS=0
PF_FAIL=0
pf_pass() { PF_PASS=$((PF_PASS + 1)); echo "PASS: $1"; }
pf_fail() { PF_FAIL=$((PF_FAIL + 1)); echo "FAIL: $1"; }

assert_contains() {  # <desc> <haystack> <needle>
  case "$2" in
    *"$3"*) pf_pass "$1" ;;
    *)      pf_fail "$1"; printf '      want substring: %s\n      in: %s\n' "$3" "$2" ;;
  esac
  return 0
}
assert_not_contains() {  # <desc> <haystack> <needle>
  case "$2" in
    *"$3"*) pf_fail "$1"; printf '      unexpected substring: %s\n      in: %s\n' "$3" "$2" ;;
    *)      pf_pass "$1" ;;
  esac
  return 0
}
assert_eq() {  # <desc> <actual> <expected>
  if [ "$2" = "$3" ]; then
    pf_pass "$1"
  else
    pf_fail "$1"; printf '      got:  %s\n      want: %s\n' "$2" "$3"
  fi
  return 0
}

# Pull the "NAME=value" token for NAME out of a space-separated env string
# (values are filesystem paths, so IFS word-splitting is safe here).
extract_assign() {  # <env string> <var name>
  local env_str="$1" name="$2" tok
  local -a toks=()
  read -ra toks <<<"${env_str}" || true
  for tok in "${toks[@]}"; do
    case "${tok}" in
      "${name}="*) printf '%s' "${tok}"; return 0 ;;
    esac
  done
  return 1
}

[ -f "${DEPLOY_SCRIPT}" ] || fail "deploy-fleet-2box.sh not found next to this test (${DEPLOY_SCRIPT})"

# Keep the source hermetic: a throwaway state dir so sourcing fleet_common.sh
# never touches the live /tmp/vllm-sr-fleet, and no optional versions.env.
FLEET_STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fleet-selftest.XXXXXX")"
export FLEET_STATE_DIR
export VERSIONS_ENV="${FLEET_STATE_DIR}/no-such-versions.env"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "${FLEET_STATE_DIR}" 2>/dev/null || true; }
trap cleanup EXIT

# --- the EXACT failing scenario from the field -------------------------------
# Agent-side security vars SET (so _fwd_names is non-empty and, under staging,
# remote_agent_env emits the '~' paths). The four path vars are LOCAL absolute
# paths under the recipe dir (all absolute, rooted at ${SELFTEST_DIR}); when FLEET_REMOTE_STAGED=1
# they are remapped to each remote's own home-relative staged locations
# regardless of the local value.
export FLEET_SIGN_MODE="ed25519"
export FLEET_ED25519_PUBLIC_FILE="${SELFTEST_DIR}/keys/ccp_ed25519.pub"
export FLEET_TLS_CA="${SELFTEST_DIR}/mtls-certs/ca-cert.pem"
export FLEET_TLS_CLIENT_CERT="${SELFTEST_DIR}/mtls-certs/halo-a-client-cert.pem"
export FLEET_TLS_CLIENT_KEY="${SELFTEST_DIR}/mtls-certs/halo-a-client-key.pem"
# R8 extras -- the LAST names in the forwarded list -- UNSET (the regression
# trigger: the loop's final iteration is the "false" one).
unset ROUTER_HEALTH_PATH ROUTER_HEALTH_TIMEOUT APPLY_BACKOFF APPLY_BACKOFF_MAX 2>/dev/null || true
# Each remote gets its OWN home-relative staged paths.
export FLEET_REMOTE_STAGED=1

# Load remote_agent_env + FLEET_SECURITY_AGENT_VARS/FLEET_AGENT_EXTRA_VARS + the
# fleet_common.sh remote-path helpers, WITHOUT executing the deploy. This also
# turns on `set -euo pipefail` (deploy-fleet-2box.sh line 37), so every check
# below runs under errexit exactly like the real step 4.
# shellcheck source=/dev/null
FLEET_DEPLOY_LIB_ONLY=1 source "${DEPLOY_SCRIPT}"

echo "==> [1/6] control: the OLD command-sub form must abort under set -e (proves the scenario reproduces the bug)"
# The construct the regression introduced. It must run as a STANDALONE subshell:
# putting it in an `if`/`&&` condition places it in a "tested" context that
# SUPPRESSES errexit (even an explicit inner `set -e`), hiding the very abort we
# want to demonstrate. So drop errexit in the parent just long enough to run it
# and capture its real exit status.
set +e
# shellcheck disable=SC2030
( set -euo pipefail
  _bad="$(for v in ${FLEET_SECURITY_AGENT_VARS} ${FLEET_AGENT_EXTRA_VARS}; do [ -n "${!v:-}" ] && printf ' %s' "${v}"; done)"
  printf '%s' "${_bad}" ) >/dev/null 2>&1
_ctrl_rc=$?
set -e
if [ "${_ctrl_rc}" -ne 0 ]; then
  note "old '_fwd_names=\"\$(... && printf ...)\"' aborts under set -e (rc=${_ctrl_rc}) -- this is what broke the deploy"
else
  note "WARNING: old command-sub form did NOT abort on this bash; steps [2-6] still hold the fix."
fi

echo "==> [2/6] step-4 name-logging loop (fixed if-guarded form) must NOT abort under set -e"
# The identical construct now living in deploy-fleet-2box.sh step 4.
_fwd_names=""
# shellcheck disable=SC2031
for v in ${FLEET_SECURITY_AGENT_VARS} ${FLEET_AGENT_EXTRA_VARS}; do
  if [ -n "${!v:-}" ]; then _fwd_names+=" ${v}"; fi
done
# Reaching this line means errexit did not fire (exit 0).
note "loop survived; forwarded names:${_fwd_names}"
for want in FLEET_SIGN_MODE FLEET_ED25519_PUBLIC_FILE FLEET_TLS_CA FLEET_TLS_CLIENT_CERT FLEET_TLS_CLIENT_KEY; do
  case " ${_fwd_names} " in
    *" ${want} "*) : ;;
    *) fail "step-4 loop omitted ${want} (got:${_fwd_names})" ;;
  esac
done

echo "==> [3/6] remote_agent_env halo-b must return 0 (mirrors box_env=\$(remote_agent_env <id>)) and emit staged '~' paths"
# Command-substitution capture, exactly like deploy-fleet-2box.sh line ~333:
# under set -e this line aborts if remote_agent_env returns non-zero.
box_env="$(remote_agent_env halo-b)"
note "box_env: ${box_env}"
for want in \
  '~/mtls-certs/halo-b-client-cert.pem' \
  '~/keys/ccp_ed25519.pub' \
  '~/mtls-certs/ca-cert.pem' \
  '~/mtls-certs/halo-b-client-key.pem'; do
  case "${box_env}" in
    *"${want}"*) note "emits ${want}" ;;
    *) fail "remote_agent_env halo-b did not emit ${want} (got: ${box_env})" ;;
  esac
done

echo "==> [4/6] static guard: deploy-fleet-2box.sh must not rebuild _fwd_names via a command substitution"
if grep -Eq '_fwd_names[[:space:]]*=[[:space:]]*"?\$\(' "${DEPLOY_SCRIPT}"; then
  fail "deploy-fleet-2box.sh builds _fwd_names with a command substitution again (the set -e regression is back)"
fi
note "no _fwd_names command-substitution assignment present"

echo
echo "==> [5/6] staged ON: per-box '~' path forwarding (raw, unquoted, no printf %q '\\~' trap; expands under the REMOTE \$HOME)"
staged_env="$(remote_agent_env halo-b)"
note "staged output: ${staged_env}"

# Each path var forwarded as the raw, HOME-relative remote path, resolved to
# halo-b's OWN client cert/key.
assert_contains "staged: ed25519 pub is ~/keys path" \
  "${staged_env}" "FLEET_ED25519_PUBLIC_FILE=~/keys/ccp_ed25519.pub"
assert_contains "staged: TLS CA is ~/mtls-certs path" \
  "${staged_env}" "FLEET_TLS_CA=~/mtls-certs/ca-cert.pem"
assert_contains "staged: per-box client cert (halo-b)" \
  "${staged_env}" "FLEET_TLS_CLIENT_CERT=~/mtls-certs/halo-b-client-cert.pem"
assert_contains "staged: per-box client key (halo-b)" \
  "${staged_env}" "FLEET_TLS_CLIENT_KEY=~/mtls-certs/halo-b-client-key.pem"
# A non-path var is still forwarded (staging only rewrites the 4 path vars).
assert_contains "staged: non-path FLEET_SIGN_MODE still forwarded" \
  "${staged_env}" "FLEET_SIGN_MODE=ed25519"
# The `printf %q` trap: a quoted '~' becomes '\~' and would NOT expand remotely.
assert_not_contains "staged: no printf %q backslash-tilde trap" \
  "${staged_env}" '\~'

# Simulate the remote shell: each 'NAME=~/...' assignment must expand '~' to the
# REMOTE $HOME (assignment-position tilde expansion), not the local one.
check_remote_expand() {  # <var name> <expected expansion under HOME=/home/test001>
  local name="$1" want="$2" assign got
  assign="$(extract_assign "${staged_env}" "${name}")" || assign=""
  if [ -z "${assign}" ]; then
    pf_fail "staged: remote-expands ${name} (no assignment emitted)"; return 0
  fi
  got="$(HOME=/home/test001 bash -c "${assign}; printf '%s' \"\$${name}\"")" || got="<eval-failed>"
  assert_eq "staged: remote-expands ${name} -> ${want}" "${got}" "${want}"
  return 0
}
check_remote_expand FLEET_ED25519_PUBLIC_FILE "/home/test001/keys/ccp_ed25519.pub"
check_remote_expand FLEET_TLS_CA              "/home/test001/mtls-certs/ca-cert.pem"
check_remote_expand FLEET_TLS_CLIENT_CERT     "/home/test001/mtls-certs/halo-b-client-cert.pem"
check_remote_expand FLEET_TLS_CLIENT_KEY      "/home/test001/mtls-certs/halo-b-client-key.pem"

echo
echo "==> [6/6] staged OFF: default flow is byte-identical printf %q verbatim forwarding (local absolute path)"
unset FLEET_REMOTE_STAGED
unstaged_env="$(remote_agent_env halo-b)"
note "unstaged output: ${unstaged_env}"
assert_contains "unstaged: verbatim local ed25519 pub path" \
  "${unstaged_env}" "FLEET_ED25519_PUBLIC_FILE=${SELFTEST_DIR}/keys/ccp_ed25519.pub"
assert_contains "unstaged: forwards a local absolute path (recipe dir)" \
  "${unstaged_env}" "${SELFTEST_DIR}/"
assert_not_contains "unstaged: does NOT emit remote ~/keys path" \
  "${unstaged_env}" "~/keys"
assert_contains "unstaged: non-path FLEET_SIGN_MODE still forwarded" \
  "${unstaged_env}" "FLEET_SIGN_MODE=ed25519"

echo
echo "==> [ship-list] static guard: every vendored module fleet_lib.py/fleet_agent.py import must ship to BOTH remote scp lists"
# We have twice shipped fleet_lib.py/fleet_agent.py to each remote WITHOUT a
# runtime dependency they need (first cross-box paths, then the vendored
# _ed25519.py module), so the remote agent died with ModuleNotFoundError. This
# OFFLINE check statically confirms that every LOCAL (non-stdlib) module those
# two files import is present in BOTH scp file lists in deploy-fleet-2box.sh
# (the gateway-mode and mock-mode remote pushes), so a dropped vendored dep now
# fails HERE, before any hardware run.

# The two files scp'd to every remote whose imports we must satisfy on the box.
_sl_importers=("${SELFTEST_DIR}/fleet_lib.py" "${SELFTEST_DIR}/fleet_agent.py")
for _sl_f in "${_sl_importers[@]}"; do
  [ -f "${_sl_f}" ] || fail "ship-list guard: importer not found (${_sl_f})"
done
# Their import statements, comment-stripped so a module named only in a trailing
# '# ...' comment can't masquerade as an import.
_sl_import_lines="$(sed -E 's/#.*//' "${_sl_importers[@]}" \
  | grep -E '^[[:space:]]*(import|from)[[:space:]]')" \
  || fail "ship-list guard: found no import statements in fleet_lib.py/fleet_agent.py (parser broke?)"

# The remote scp file lists in deploy-fleet-2box.sh, each flattened onto ONE line
# (line-continuations joined). Exactly two are expected: the gateway-mode and
# mock-mode pushes, both to ${target}:${REMOTE_DIR}/ (matched via REMOTE_DIR).
# shellcheck disable=SC2016  # the awk body is awk code ($0 etc.), not shell
mapfile -t _sl_lists < <(awk '
  /^[[:space:]]*scp[[:space:]]/ { inscp = 1; buf = "" }
  inscp {
    line = $0; sub(/\\[[:space:]]*$/, "", line); buf = buf " " line
    if ($0 !~ /\\[[:space:]]*$/) { if (buf ~ /REMOTE_DIR/) print buf; inscp = 0 }
  }
' "${DEPLOY_SCRIPT}")
[ "${#_sl_lists[@]}" -eq 2 ] \
  || fail "ship-list guard: expected 2 remote scp lists in deploy-fleet-2box.sh, found ${#_sl_lists[@]} (deploy structure changed -- update this guard)"

# The importers are the files being shipped, not deps -- skip them; every OTHER
# local *.py they import is a vendored dep that MUST be in EVERY remote scp list.
_sl_importer_names=""
for _sl_f in "${_sl_importers[@]}"; do _sl_importer_names+=" $(basename "${_sl_f}" .py) "; done
_sl_deps=""
for _sl_pyf in "${SELFTEST_DIR}"/*.py; do
  _sl_mod="$(basename "${_sl_pyf}" .py)"
  case "${_sl_importer_names}" in *" ${_sl_mod} "*) continue ;; esac
  printf '%s\n' "${_sl_import_lines}" | grep -Fwq -- "${_sl_mod}" || continue
  _sl_deps+=" ${_sl_mod}"
  _sl_i=0
  for _sl_list in "${_sl_lists[@]}"; do
    _sl_i=$((_sl_i + 1))
    case "${_sl_list}" in
      *"\${SCRIPT_DIR}/${_sl_mod}.py"*) : ;;
      *) fail "ship-list guard: vendored dep '${_sl_mod}.py' (imported by fleet_lib.py/fleet_agent.py) is MISSING from remote scp list #${_sl_i} in deploy-fleet-2box.sh -- add \"\${SCRIPT_DIR}/${_sl_mod}.py\" so the remote agent doesn't crash with ModuleNotFoundError" ;;
    esac
  done
done
[ -n "${_sl_deps}" ] \
  || fail "ship-list guard: discovered no vendored deps at all (expected at least _ed25519) -- the import scan or file layout changed"
note "ship-list guard: vendored dep(s)${_sl_deps} present in BOTH remote scp lists"

echo
echo "----------------------------------------------------------------"
if [ "${PF_FAIL}" -eq 0 ]; then
  echo "PASS: set -e regression guard + ${PF_PASS} path-forwarding assertion(s) all green."
  exit 0
fi
echo "FAIL: ${PF_FAIL} path-forwarding assertion(s) failed (${PF_PASS} passed)."
exit 1
