#!/usr/bin/env bash
#
# taskb_cache_overlay.sh -- Task B: llama.cpp semantic-cache repeat-TTFT overlay.
# Repoints the router alias google/gemini-2.5-flash-lite at the live llama-server
# (llama.cpp resident 120B, --parallel 8) on vllm-sr-network, then measures
# miss -> exact-repeat-hit -> semantic-0.92-hit TTFT through the router path via
# bestcfg_matrix.py cache-overlay. An EXIT trap ALWAYS restores the original
# runtime-config (no leftover repoint/cache/threshold injection), even if killed.
set -uo pipefail

PERF=/home/test001/gemma-bench/strix-halo-fleet-2box/perf
CFG=/tmp/vllm-sr-fleet/gateway/.vllm-sr/runtime-config.yaml
OUT=/home/test001/ttft-sweetspot
LOGS="${OUT}/logs"
mkdir -p "${OUT}" "${LOGS}"
ROUTER_URL=http://localhost:8899/v1
ROUTER_HASH_URL=http://localhost:8080/config/hash
ROUTER_CTR=vllm-sr-router-container
ALIAS=google/gemini-2.5-flash-lite
LLAMA_ENDPOINT=llama-server:8080
LLAMA_MODEL=gpt-oss-120b
EXPECT_HASH=a78aebc5fd5fa570fadb9e000abcef3072abf02813762fa968aac73b9fa244cf

stamp(){ date -u +%FT%TZ; }
say(){ echo "[$(stamp)] $*"; }

BAK="${CFG}.taskb-bak"
cp -f "${CFG}" "${BAK}"
restore(){
  say "restore: writing original runtime-config back (same inode)"
  # Truncate-write same inode so fsnotify hot-reload fires (never a rename).
  cat "${BAK}" > "${CFG}" 2>/dev/null || true
  sleep 4
  say "restore: config hash now $(curl -s ${ROUTER_HASH_URL} 2>/dev/null)"
  grep -q '\- type: semantic-cache' "${CFG}" && say "WARN: semantic-cache injection STILL present after restore" || say "restore: no semantic-cache injection remains (clean)"
}
trap restore EXIT

say "TASKB_START pid=$$"

# 1) Wait for llama-server health (misses must hit the 120B).
say "waiting for llama-server /health ..."
ok=0
for i in $(seq 1 200); do
  if curl -fsS --max-time 5 http://localhost:8081/health >/dev/null 2>&1; then ok=1; say "llama-server healthy after $((i*2))s"; break; fi
  docker ps --format '{{.Names}}' | grep -q '^llama-server$' || { say "llama-server container gone during load"; break; }
  sleep 2
done
[ "${ok}" = 1 ] || { say "ABORT: llama-server never healthy"; exit 1; }
LLAMA_MODEL="$(curl -fsS --max-time 10 http://localhost:8081/v1/models 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null || echo gpt-oss-120b)"
say "llama-server served model id: ${LLAMA_MODEL}"

# 2) Repoint the alias at llama-server (in-place, same inode -> hot-reload).
say "repoint ${ALIAS} -> ${LLAMA_ENDPOINT} (${LLAMA_MODEL})"
python3 "${PERF}/repoint_backend.py" --config "${CFG}" --alias "${ALIAS}" \
  --endpoint "${LLAMA_ENDPOINT}" --model "${LLAMA_MODEL}" 2>&1 | sed 's/^/    /'
# wait for the router to log a reload
since=$(date +%s)
for i in $(seq 1 30); do
  docker logs --since "${since}" "${ROUTER_CTR}" 2>&1 | grep -q '"config_reloaded"' && { say "router reloaded after repoint"; break; }
  sleep 1
done
sleep 3

# 3) Routing pre-check: does model:auto for a general-knowledge query reach
#    llama-server? Dump router debug headers + capture llama-server request count.
say "routing pre-check (model=auto, debug headers):"
req_before=$(docker logs --tail 2000 llama-server 2>&1 | grep -c 'POST /v1/chat/completions' || echo 0)
curl -s -D - -o /dev/null -X POST "${ROUTER_URL}/chat/completions" \
  -H 'Content-Type: application/json' -H 'x-vsr-debug: true' \
  -d '{"model":"auto","messages":[{"role":"user","content":"What is the capital of France? (taskb-precheck)"}],"max_tokens":8}' 2>&1 \
  | grep -iE '^(x-vsr|x-gateway|x-selected|server:)' | sed 's/^/    hdr: /'
sleep 2
req_after=$(docker logs --tail 2000 llama-server 2>&1 | grep -c 'POST /v1/chat/completions' || echo 0)
say "llama-server chat requests: before=${req_before} after=${req_after} (delta=$((req_after - req_before)))"

# 4) Run the cache overlay (miss/exact-hit/semantic-hit @ threshold 0.92).
say "running bestcfg_matrix.py cache-overlay (server=llamacpp, threshold=0.92)"
python3 "${PERF}/bestcfg_matrix.py" cache-overlay \
  --router-url "${ROUTER_URL}" --server llamacpp --cell-id llamacpp-resident-p8 \
  --config "${CFG}" --container "${ROUTER_CTR}" --threshold 0.92 \
  --reload-timeout 45 --reload-settle 3 --out "${OUT}/cache-llamacpp.json" \
  >"${LOGS}/cache-llamacpp.log" 2>&1 && say "cache-overlay OK" || say "cache-overlay FAILED (see log)"

# 5) llama-server request evidence over the whole overlay.
req_final=$(docker logs --tail 4000 llama-server 2>&1 | grep -c 'POST /v1/chat/completions' || echo 0)
say "llama-server chat requests total-tail: ${req_final}"

# 6) Router health / OOM check.
rstate="$(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}}' ${ROUTER_CTR} 2>/dev/null)"
say "router state: ${rstate}"
case "${rstate}" in
  *exit=137*|exited*) say "WARN: router not running (possible OOM exit 137); starting it"; docker start "${ROUTER_CTR}" >/dev/null 2>&1 || true; sleep 8 ;;
esac

# restore runs via trap here.
say "TASKB_DONE"
h=$(curl -s ${ROUTER_HASH_URL} 2>/dev/null)
say "final config hash: ${h}"
[ "${h}" = "{\"hash\":\"${EXPECT_HASH}\"}" ] && say "HASH_OK (matches expected)" || say "HASH_CHECK: expected ${EXPECT_HASH}"
