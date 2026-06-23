#!/usr/bin/env bash
#
# 2-box Strix Halo PoC: router-replay -> fleet-sim trace exporter.
#
# Pages the gateway's read-only router-replay API and reshapes the records into
# fleet-sim's `semantic_router` JSONL format, so the PoC's REAL per-request
# routing decisions can drive a fleet-sim capacity/TCO simulation instead of a
# synthetic workload. This is the scripted version of the curl+jq recipe in
# docs/poc/03-strix-halo-runbook.md section 9 ("Fleet-sim / TCO").
#
# fleet-sim's loader (src/fleet-sim/fleet_sim/workload/trace.py) expects each
# JSONL row to have: timestamp (epoch seconds), prompt_tokens, generated_tokens,
# selected_model. The replay store emits completion_tokens (not
# generated_tokens) and an RFC3339 timestamp, and prompt/completion tokens are
# nullable for cached/streamed requests -- so we rename, convert, and filter.
#
# Inputs (env vars, all optional):
#   BASE_URL   gateway listener base URL.   Default: http://localhost:8899
#   OUT        output trace file.           Default: poc-trace.jsonl
#   MAX_OFFSET highest offset to page to.   Default: 1000 (i.e. up to 1100 rows)
#   PAGE       page size per request.       Default: 100
#
# Usage (from anywhere):
#   bash export-replay-trace.sh
#   BASE_URL=http://halo-a:8899 OUT=/tmp/poc-trace.jsonl bash export-replay-trace.sh
#
# This is a READ-ONLY exporter: it only issues GET requests to the replay API
# and writes the local OUT file. It does NOT serve, deploy, or mutate anything.
set -euo pipefail

# Resolve this script's directory so relative hints work from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

BASE_URL="${BASE_URL:-http://localhost:8899}"
OUT="${OUT:-poc-trace.jsonl}"
MAX_OFFSET="${MAX_OFFSET:-1000}"
PAGE="${PAGE:-100}"

for bin in curl jq; do
  if ! command -v "${bin}" >/dev/null 2>&1; then
    echo "ERROR: required command '${bin}' not found." >&2
    exit 1
  fi
done

RAW="$(mktemp "${TMPDIR:-/tmp}/replay-raw.XXXXXX.jsonl")"
cleanup() { rm -f "${RAW}" 2>/dev/null || true; }
trap cleanup EXIT

echo "==> [1/2] Paging router-replay from ${BASE_URL}/v1/router_replay (page=${PAGE}, max offset=${MAX_OFFSET})"
: > "${RAW}"
for (( off=0; off<=MAX_OFFSET; off+=PAGE )); do
  # Each page returns {total, data:[...]}; collect the per-record objects. A
  # short page (fewer than PAGE rows) means we have reached the end.
  page_json="$(curl -fsS "${BASE_URL}/v1/router_replay?limit=${PAGE}&offset=${off}")" || {
    echo "ERROR: failed to query ${BASE_URL}/v1/router_replay?limit=${PAGE}&offset=${off}" >&2
    echo "       Is the gateway serving and is router_replay enabled?" >&2
    exit 1
  }
  count="$(printf '%s' "${page_json}" | jq '.data | length')"
  printf '%s' "${page_json}" | jq -c '.data[]' >> "${RAW}"
  echo "    offset=${off}: ${count} record(s)"
  if [[ "${count}" -lt "${PAGE}" ]]; then
    break
  fi
done

raw_rows="$(wc -l < "${RAW}" | tr -d ' ')"
echo "    collected ${raw_rows} raw record(s)"

echo "==> [2/2] Reshaping into fleet-sim semantic_router JSONL -> ${OUT}"
# Rename completion_tokens -> generated_tokens (fleet-sim's required field),
# convert RFC3339 timestamp -> epoch seconds, and drop rows missing token
# counts (cached/streamed requests). category is an optional signal.
jq -c 'select(.prompt_tokens != null and .completion_tokens != null)
       | {timestamp: (.timestamp | sub("\\.[0-9]+Z$";"Z") | fromdateiso8601),
          prompt_tokens: .prompt_tokens,
          generated_tokens: .completion_tokens,
          selected_model: .selected_model,
          category: (.category // "prose")}' \
  "${RAW}" > "${OUT}"

out_rows="$(wc -l < "${OUT}" | tr -d ' ')"
echo "    wrote ${out_rows} usable trace row(s) to ${OUT}"
if [[ "${out_rows}" -eq 0 ]]; then
  echo "WARNING: 0 usable rows. The replay store may be empty, or all rows lack" >&2
  echo "         token counts. Send some traffic through the gateway first." >&2
fi

echo
echo "Next: run fleet-sim against the exported trace."
echo "    pip install -e ${REPO_ROOT}/src/fleet-sim"
echo "    python3 ${REPO_ROOT}/src/fleet-sim/examples/semantic_router_trace_replay.py ${OUT} selected_model"
echo
echo "CAVEAT: fleet-sim's GPU pools are hardcoded NVIDIA (a100/a10g), so the"
echo "        resulting \$/yr and node/GPU counts are a PIPELINE DEMONSTRATION"
echo "        with default profiles -- NOT an Instinct-calibrated TCO."
