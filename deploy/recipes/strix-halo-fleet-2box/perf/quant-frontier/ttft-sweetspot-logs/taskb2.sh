#!/usr/bin/env bash
#
# taskb2_cache_overlay.sh -- Task B (llama.cpp-specific): repoint the model that
# model:auto actually selects for general Q&A -- qwen/qwen3.5-rocm, the router's
# default_model used by fast_qa -- at the live llama-server (llama.cpp resident
# 120B, --parallel 8), so cache MISSES are served by llama.cpp. Then measure
# miss -> exact-repeat-hit -> semantic-0.92-hit TTFT through the router path via
# bestcfg_matrix.py cache-overlay. EXIT trap ALWAYS restores the original config.
set -uo pipefail

PERF=/home/test001/gemma-bench/strix-halo-fleet-2box/perf
CFG=/tmp/vllm-sr-fleet/gateway/.vllm-sr/runtime-config.yaml
OUT=/home/test001/ttft-sweetspot
LOGS="${OUT}/logs"
mkdir -p "${OUT}" "${LOGS}"
ROUTER_URL=http://localhost:8899/v1
ROUTER_HASH_URL=http://localhost:8080/config/hash
ROUTER_CTR=vllm-sr-router-container
ALIAS=qwen/qwen3.5-rocm
LLAMA_ENDPOINT=llama-server:8080
EXPECT_HASH=a78aebc5fd5fa570fadb9e000abcef3072abf02813762fa968aac73b9fa244cf

stamp(){ date -u +%FT%TZ; }
say(){ echo "[$(stamp)] $*"; }
llama_reqs(){ docker logs --tail 6000 llama-server 2>/dev/null | grep -c 'POST /v1/chat/completions'; }

BAK="${CFG}.taskb2-bak"
cp -f "${CFG}" "${BAK}"
restore(){
  say "restore: writing original runtime-config back (same inode)"
  cat "${BAK}" > "${CFG}" 2>/dev/null || true
  sleep 4
  say "restore: config hash now $(curl -s ${ROUTER_HASH_URL} 2>/dev/null)"
  if grep -q '\- type: semantic-cache' "${CFG}"; then say "WARN: semantic-cache injection STILL present"; else say "restore: clean (no cache injection)"; fi
  if grep -q "endpoint: ${LLAMA_ENDPOINT}" "${CFG}"; then say "WARN: llama-server endpoint STILL present (repoint not undone)"; else say "restore: no llama-server endpoint remains (repoint undone)"; fi
}
trap restore EXIT

say "TASKB2_START pid=$$ alias=${ALIAS}"

# 1) llama-server health.
ok=0
for i in $(seq 1 200); do
  curl -fsS --max-time 5 http://localhost:8081/health >/dev/null 2>&1 && { ok=1; say "llama-server healthy"; break; }
  docker ps --format '{{.Names}}' | grep -q '^llama-server$' || { say "llama-server gone"; break; }
  sleep 2
done
[ "${ok}" = 1 ] || { say "ABORT: llama-server not healthy"; exit 1; }
LLAMA_MODEL="$(curl -fsS --max-time 10 http://localhost:8081/v1/models 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null || echo gpt-oss-120b)"
say "llama-server served id: ${LLAMA_MODEL}"

# 2) Repoint the default_model at llama-server.
say "repoint ${ALIAS} -> ${LLAMA_ENDPOINT} (${LLAMA_MODEL})"
python3 "${PERF}/repoint_backend.py" --config "${CFG}" --alias "${ALIAS}" \
  --endpoint "${LLAMA_ENDPOINT}" --model "${LLAMA_MODEL}" 2>&1 | sed 's/^/    /'
since=$(date +%s)
for i in $(seq 1 40); do
  docker logs --since "${since}" "${ROUTER_CTR}" 2>&1 | grep -q '"config_reloaded"' && { say "router reloaded after repoint"; break; }
  sleep 1
done
sleep 3

# 3) Routing pre-check: confirm model:auto now reaches llama-server.
rb=$(llama_reqs); say "routing pre-check (llama reqs before=${rb}):"
curl -s -D - -o /dev/null -X POST "${ROUTER_URL}/chat/completions" \
  -H 'Content-Type: application/json' -H 'x-vsr-debug: true' \
  -d '{"model":"auto","messages":[{"role":"user","content":"What is the capital of France? (taskb2-precheck)"}],"max_tokens":8}' 2>&1 \
  | grep -iE '^x-vsr-selected-(decision|model)' | sed 's/^/    hdr: /'
sleep 3
ra=$(llama_reqs); say "llama reqs after precheck=${ra} (delta $((ra - rb)))"

# 4) Cache overlay -> llama.cpp-specific miss/hit/semantic.
say "running cache-overlay (server=llamacpp, threshold=0.92)"
python3 "${PERF}/bestcfg_matrix.py" cache-overlay \
  --router-url "${ROUTER_URL}" --server llamacpp --cell-id llamacpp-resident-p8 \
  --config "${CFG}" --container "${ROUTER_CTR}" --threshold 0.92 \
  --reload-timeout 45 --reload-settle 3 --out "${OUT}/cache-llamacpp.json" \
  >"${LOGS}/cache-llamacpp2.log" 2>&1 && say "cache-overlay OK" || say "cache-overlay FAILED"
rf=$(llama_reqs); say "llama reqs total after overlay=${rf} (overlay delta $((rf - ra)))"

# 5) Router OOM check.
rstate="$(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}}' ${ROUTER_CTR} 2>/dev/null)"
say "router state: ${rstate}"
echo "${rstate}" | grep -q 'running' || { say "WARN router not running; starting"; docker start "${ROUTER_CTR}" >/dev/null 2>&1 || true; sleep 8; }

say "TASKB2_DONE"
h=$(curl -s ${ROUTER_HASH_URL} 2>/dev/null)
say "final config hash: ${h}"
[ "${h}" = "{\"hash\":\"${EXPECT_HASH}\"}" ] && say "HASH_OK" || say "HASH_MISMATCH expected ${EXPECT_HASH}"
