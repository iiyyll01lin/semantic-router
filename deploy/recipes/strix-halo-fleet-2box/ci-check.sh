#!/usr/bin/env bash
#
# ci-check.sh — fleet "config x pinned-image" compatibility gate (R3 + R2).
#
# ONE script, TWO callers:
#   * CI (strict):   `CI_CHECK_STRICT=1 bash ci-check.sh` (see .github/workflows).
#                    Parses the committed strix-halo-poc/poc-strix.yaml and
#                    enforces that versions.env.example pins the router image to
#                    an immutable @sha256 digest (kills `:latest` drift, R3).
#   * deploy (fail-fast, advisory): deploy-fleet-2box.sh runs it on the RENDERED
#                    gateway config BEFORE the ~44 GB model pull / cold start, so
#                    a schema mismatch fails in SECONDS instead of ~9 minutes in
#                    (R2). Advisory: if no validator is available it warns and
#                    lets the deploy proceed (never regresses the default flow).
#
# It does NOT pull images, contact the CCP, or start anything.
#
# Usage:
#   ci-check.sh [--config PATH] [--image REF] [--strict] [--no-image]
# Env:
#   CI_CHECK_STRICT=1        same as --strict (missing pin / no validator = FAIL)
#   VLLM_SR_ROUTER_IMAGE     overrides the pinned image ref to lint
#   VLLM_SR_BIN              dir holding `vllm-sr` if it is not on PATH
#   FLEET_SKIP_VALIDATE=1    skip the schema validation step entirely
#
# Exit: 0 = OK (advisory warnings allowed), 1 = a fatal check failed.
#
set -uo pipefail   # NOT -e: we manage exit status explicitly for clear reporting
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POC_DIR="${SCRIPT_DIR}/../strix-halo-poc"
STRUCTURAL_VALIDATOR="${POC_DIR}/validate_poc_config.py"

STRICT="${CI_CHECK_STRICT:-0}"
CONFIG=""
IMAGE_OVERRIDE=""
DO_IMAGE=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --image)  IMAGE_OVERRIDE="$2"; shift 2 ;;
    --strict) STRICT=1; shift ;;
    --no-image) DO_IMAGE=0; shift ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "ci-check: unknown arg '$1'" >&2; exit 2 ;;
  esac
done
CONFIG="${CONFIG:-${POC_DIR}/poc-strix.yaml}"

fail=0
warns=0
note() { echo "    $*"; }
warn() { echo "    WARN: $*" >&2; warns=$((warns + 1)); }
err()  { echo "    ERROR: $*" >&2; fail=1; }
# strict-aware gate: fatal under --strict (CI), advisory warning otherwise (deploy)
gate() { if [ "${STRICT}" = "1" ]; then err "$1"; else warn "$1"; fi; }

_trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }

# Read VAR=value from an env-style file WITHOUT sourcing it (no code execution);
# last assignment wins, surrounding quotes and inline comments stripped.
read_env_var() {
  local file="$1" var="$2" line
  [ -f "${file}" ] || return 1
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${var}=" "${file}" | tail -n1)" || return 1
  [ -n "${line}" ] || return 1
  line="${line#*=}"
  line="${line%%#*}"
  line="$(_trim "${line}")"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "${line}"
}

echo "==> [ci-check] fleet config x pinned-image compatibility (R3+R2)"
echo "    config = ${CONFIG}"
echo "    strict = ${STRICT}"

if [ ! -f "${CONFIG}" ]; then
  echo "    ERROR: config not found: ${CONFIG}" >&2
  echo "CI-CHECK: FAIL"
  exit 1
fi

# --- 1) Resolve + lint the pinned router image (R3) --------------------------
IMAGE=""
IMAGE_SRC=""
if [ -n "${IMAGE_OVERRIDE}" ]; then
  IMAGE="${IMAGE_OVERRIDE}"; IMAGE_SRC="--image"
elif [ -n "${VLLM_SR_ROUTER_IMAGE:-}" ]; then
  IMAGE="${VLLM_SR_ROUTER_IMAGE}"; IMAGE_SRC="env"
elif _v="$(read_env_var "${SCRIPT_DIR}/versions.env" VLLM_SR_ROUTER_IMAGE)" && [ -n "${_v}" ]; then
  IMAGE="${_v}"; IMAGE_SRC="versions.env"
elif _v="$(read_env_var "${SCRIPT_DIR}/versions.env.example" VLLM_SR_ROUTER_IMAGE)" && [ -n "${_v}" ]; then
  IMAGE="${_v}"; IMAGE_SRC="versions.env.example"
fi

echo "==> [ci-check] router image pin"
if [ -z "${IMAGE}" ]; then
  gate "no VLLM_SR_ROUTER_IMAGE pin found (versions.env / versions.env.example); running :latest risks schema drift"
else
  echo "    pin = ${IMAGE} (from ${IMAGE_SRC})"
  if [[ "${IMAGE}" == \[* || "${IMAGE}" == *']('* || "${IMAGE}" == http://* || "${IMAGE}" == https://* ]]; then
    err "VLLM_SR_ROUTER_IMAGE is not a bare image ref (looks like a URL/Markdown link)"
  elif [[ "${IMAGE}" =~ @sha256:[0-9a-fA-F]{64}$ ]]; then
    note "pinned to an immutable digest (good — no :latest drift)"
  else
    gate "VLLM_SR_ROUTER_IMAGE is not a @sha256 digest pin (drift risk): ${IMAGE}"
  fi
fi

# --- 2) Validate the config schema (R2 fail-fast) ----------------------------
echo "==> [ci-check] config schema validation"
if [ "${FLEET_SKIP_VALIDATE:-0}" = "1" ]; then
  warn "FLEET_SKIP_VALIDATE=1; skipping schema validation"
else
  VSR=""
  if command -v vllm-sr >/dev/null 2>&1; then
    VSR="vllm-sr"
  elif [ -n "${VLLM_SR_BIN:-}" ] && [ -x "${VLLM_SR_BIN%/}/vllm-sr" ]; then
    VSR="${VLLM_SR_BIN%/}/vllm-sr"
  fi
  if [ -n "${VSR}" ]; then
    note "validator: ${VSR} validate (canonical v0.3 schema)"
    if "${VSR}" validate --config "${CONFIG}"; then
      note "config schema VALID (vllm-sr)"
    else
      err "config FAILED 'vllm-sr validate' (schema mismatch — fix before deploying)"
    fi
  elif [ -f "${STRUCTURAL_VALIDATOR}" ] && python3 -c 'import yaml' >/dev/null 2>&1; then
    note "validator: structural validate_poc_config.py (PyYAML)"
    if python3 "${STRUCTURAL_VALIDATOR}" "${CONFIG}"; then
      note "config structurally VALID"
    else
      err "config FAILED structural validation (validate_poc_config.py)"
    fi
  else
    gate "no config validator available (need 'vllm-sr' on PATH/VLLM_SR_BIN, or python3+PyYAML for validate_poc_config.py)"
  fi
fi

# --- 3) Best-effort digest association (no pull) -----------------------------
if [ "${DO_IMAGE}" = "1" ] && [ -n "${IMAGE}" ] && command -v docker >/dev/null 2>&1; then
  echo "==> [ci-check] local image digest (best-effort, no pull)"
  if docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    _dg="$(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "${IMAGE}" 2>/dev/null || true)"
    note "router image present locally"
    [ -n "${_dg}" ] && note "repo digest: ${_dg}"
  else
    note "router image not present locally (skipping; it is pulled at serve time)"
  fi
fi

echo "==> [ci-check] summary: image=${IMAGE:-<none>} src=${IMAGE_SRC:-n/a} warnings=${warns}"
if [ "${fail}" -ne 0 ]; then
  echo "CI-CHECK: FAIL"
  exit 1
fi
if [ "${warns}" -ne 0 ]; then
  echo "CI-CHECK: PASS (with ${warns} warning(s))"
else
  echo "CI-CHECK: PASS"
fi
exit 0
