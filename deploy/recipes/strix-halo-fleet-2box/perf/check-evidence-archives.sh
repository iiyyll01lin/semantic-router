#!/usr/bin/env bash
# Periodic integrity check for the preserved Strix Halo agentic-context evidence.
#
# Re-derives the immutable backup archive and the controller prefill manifest
# from their raw bytes (via the report validator's --backup-archive and
# --prefill-manifest paths) and fails if anything drifted. Intended to run on
# the controller (where ~/vllm-sr-evidence lives) from cron or the sibling
# systemd --user timer (systemd/vllm-sr-evidence-check.timer).
#
# Safe to run anywhere: it always runs the tracked-only report consistency
# check, adds each raw check only when its evidence is present, and fails only
# on actual drift. Override the evidence root with VLLM_SR_EVIDENCE_ROOT and the
# interpreter with AGENT_PYTHON.
set -euo pipefail

RECIPE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVIDENCE_ROOT="${VLLM_SR_EVIDENCE_ROOT:-$HOME/vllm-sr-evidence}"
PY="${AGENT_PYTHON:-python3}"

backup="$EVIDENCE_ROOT/archives/demo-002-evidence-backup-20260723.tar.gz"
prefill="$EVIDENCE_ROOT/agentic-prefill-20260722/campaign-checksums.sha256"

args=()
[ -f "$backup" ] && args+=(--backup-archive "$backup")
[ -f "$prefill" ] && args+=(--prefill-manifest "$prefill")

echo "[$(date -Is)] evidence integrity check"
echo "  recipe=$RECIPE_DIR"
echo "  evidence=$EVIDENCE_ROOT"
if [ "${#args[@]}" -eq 0 ]; then
  echo "  note: no preserved raw evidence found; running tracked-only check"
fi

exec "$PY" "$RECIPE_DIR/perf/validate_agentic_context_reports.py" "${args[@]}"
