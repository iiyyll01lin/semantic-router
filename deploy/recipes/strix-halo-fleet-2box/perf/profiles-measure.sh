#!/usr/bin/env bash
#
# profiles-measure.sh -- autonomous, detached targeted measurements for the Strix
# Halo OPERATING-PROFILES study (agentic tool-call, multiagent concurrency, and the
# EXAONE/Phi-4 quality completion). Designed to run under nohup on Halo-B so it
# survives the controller's SSH turn dropping; it writes a progress log + per-step
# JSON and NEVER edits repo/docs and NEVER commits.
#
# Phases (each phase is independent; a failure is recorded skip-with-reason and the
# run continues):
#   0. preflight  -- ollama reachable, disk headroom, no competing bench procs
#   1. pull       -- pull only the tags this run needs (tracks what IT pulled)
#   2. concurrency-- c1/c2/c4/c8 for Q4 / Q8 / qwen3-coder with OLLAMA_NUM_PARALLEL=8
#                    (real container toggle via rename-backup + GUARANTEED restore)
#   3. agentic    -- structured-JSON / tool-call micro-benchmark (agentic_toolcall.py)
#   4. quality    -- EXAONE 4.0 32B + Phi-4 reasoning plus 42Q MMLU-Pro completion
#   5. cleanup    -- unload models; remove ONLY the tags this run pulled (disk hygiene)
#
# Residency: every measured load forces full VRAM residency (num_gpu=999 +
# use_mmap=false) so the 96 GiB-carveout system-RAM auto-budget trap cannot
# CPU-offload and pollute the numbers -- same methodology as the frontier baseline.
#
# Safety: the concurrency phase renames the LIVE ollama container aside (never
# destroys it) and brings up an identical bench container with NUM_PARALLEL=8; a
# trap restores the original on ANY exit. The full vllm-sr stack keeps resolving
# "ollama" by name on vllm-sr-network throughout (two brief blips at toggle/restore).
set -uo pipefail

PERF="${PERF:-$HOME/gemma-bench/strix-halo-fleet-2box/perf}"
OUT="${OUT:-$HOME/profiles-out}"
LOGS="${OUT}/logs"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-ollama}"
OLLAMA_IMAGE="${OLLAMA_IMAGE:-ollama/ollama:rocm}"
OLLAMA_NETWORK="${OLLAMA_NETWORK:-vllm-sr-network}"
NUM_PARALLEL="${NUM_PARALLEL:-8}"
MIN_FREE_GB="${MIN_FREE_GB:-50}"
REMOVE_PULLED="${REMOVE_PULLED:-1}"
PULL_TIMEOUT="${PULL_TIMEOUT:-5400}"
QUALITY_LIMIT="${QUALITY_LIMIT:-42}"

mkdir -p "${OUT}" "${LOGS}"
PROG="${OUT}/progress.log"
stamp() { date -u +%FT%TZ; }
say()   { echo "[$(stamp)] $*" | tee -a "${PROG}"; }

# model "tag|label|kind" specs. kind drives think handling: gemma=thinking(no-think),
# plain=omit think, reason=thinking reasoning model (no-think, with fallback).
Q4="gemma4:26b|gemma4_26b|gemma"
Q8="gemma4:26b-a4b-it-q8_0|gemma4_26b-a4b-it-q8_0|gemma"
QWEN="qwen3-coder:30b|qwen3_coder_30b|plain"
G31QAT="gemma4:31b-it-qat|gemma4_31b-it-qat|gemma"
EXAONE="ingu627/exaone4.0:32b|exaone4_0_32b|reason"
PHI4="phi4-reasoning:plus|phi4_reasoning_plus|reason"

CONC_SPECS=("${Q4}" "${Q8}" "${QWEN}")
AGENTIC_SPECS=("${Q8}" "${QWEN}" "${G31QAT}")
QUALITY_SPECS=("${EXAONE}" "${PHI4}")
PULL_SPECS=("${Q4}" "${Q8}" "${QWEN}" "${G31QAT}" "${EXAONE}" "${PHI4}")

declare -a PULLED_TAGS=()

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
tag_of()   { echo "${1%%|*}"; }
label_of() { local r="${1#*|}"; echo "${r%%|*}"; }
kind_of()  { echo "${1##*|}"; }

wait_url() { local url="$1" tries="${2:-60}" i; for ((i=0;i<tries;i++)); do curl -fsS --max-time 5 "$url" >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }

have_model() { docker exec "${OLLAMA_CONTAINER}" ollama show "$1" >/dev/null 2>&1; }

free_gb() { df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9'; }

unload() { # drop a model from memory so the next forced-resident load has the carveout
  local tag="$1"
  curl -fsS --max-time 30 "${OLLAMA_URL}/api/generate" \
    -d "{\"model\":\"${tag}\",\"keep_alive\":0}" >/dev/null 2>&1 || true
  docker exec "${OLLAMA_CONTAINER}" ollama stop "${tag}" >/dev/null 2>&1 || true
  sleep 3
}

resident_frac() { # echo size_vram/size (2dp) for a loaded tag, else n/a
  curl -fsS --max-time 15 "${OLLAMA_URL}/api/ps" 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
t=sys.argv[1]
r=next((m for m in d.get("models",[]) if m.get("name")==t), {}) or {}
sz=r.get("size") or 0; sv=r.get("size_vram") or 0
print("%.2f"%(sv/sz) if sz else "n/a")
' "$1" 2>/dev/null || echo "n/a"
}

# think:false supported? (some reasoning models reject the field on 0.30.10)
think_false_ok() {
  local tag="$1" code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 120 "${OLLAMA_URL}/api/generate" \
    -d "{\"model\":\"${tag}\",\"prompt\":\"hi\",\"stream\":false,\"think\":false,\"options\":{\"num_predict\":1}}")
  [ "${code}" = "200" ]
}

pull_one() { # spec -> pull if missing; track if WE pulled it
  local spec="$1" tag; tag="$(tag_of "${spec}")"
  if have_model "${tag}"; then say "PULL skip (present): ${tag}"; return 0; fi
  local fg; fg="$(free_gb)"; fg="${fg:-0}"
  if [ "${fg}" -lt "${MIN_FREE_GB}" ]; then
    say "PULL SKIP ${tag}: only ${fg}GB free (< ${MIN_FREE_GB}GB)"; return 1
  fi
  say "PULL start ${tag} (${fg}GB free)"
  if timeout "${PULL_TIMEOUT}" docker exec "${OLLAMA_CONTAINER}" ollama pull "${tag}" \
        >"${LOGS}/pull-$(label_of "${spec}").log" 2>&1; then
    PULLED_TAGS+=("${tag}"); say "PULL ok ${tag}"; return 0
  fi
  say "PULL FAIL ${tag} (see pull log) -- skip-with-reason"; return 1
}

# --------------------------------------------------------------------------- #
# concurrency phase: container toggle (rename-backup) + guaranteed restore
# --------------------------------------------------------------------------- #
OLLAMA_TOGGLED=0
BACKUP_NAME="${OLLAMA_CONTAINER}-prebench-$$"

toggle_parallel() {
  say "CONC toggle: OLLAMA_NUM_PARALLEL=${NUM_PARALLEL} (rename ${OLLAMA_CONTAINER}->${BACKUP_NAME}, bring up bench)"
  docker stop "${OLLAMA_CONTAINER}" >/dev/null 2>&1 || { say "CONC ERR: stop failed"; return 1; }
  docker rename "${OLLAMA_CONTAINER}" "${BACKUP_NAME}" >/dev/null 2>&1 || {
    say "CONC ERR: rename failed; restarting original"; docker start "${OLLAMA_CONTAINER}" >/dev/null 2>&1 || true; return 1; }
  OLLAMA_TOGGLED=1
  if ! docker run -d --name "${OLLAMA_CONTAINER}" --network "${OLLAMA_NETWORK}" \
        -p 11434:11434 -v ollama:/root/.ollama \
        --device=/dev/kfd --device=/dev/dri --group-add video \
        --cap-add SYS_PTRACE --security-opt seccomp=unconfined \
        -e HSA_OVERRIDE_GFX_VERSION=11.5.1 -e OLLAMA_HOST=0.0.0.0:11434 \
        -e "OLLAMA_NUM_PARALLEL=${NUM_PARALLEL}" "${OLLAMA_IMAGE}" >/dev/null 2>&1; then
    say "CONC ERR: bench run failed"; return 1
  fi
  wait_url "${OLLAMA_URL}/api/tags" 60 || { say "CONC ERR: bench ollama not healthy"; return 1; }
  say "CONC bench up (NUM_PARALLEL=${NUM_PARALLEL})"
}

restore_parallel() {
  [ "${OLLAMA_TOGGLED}" = "1" ] || return 0
  say "CONC restore: removing bench, renaming ${BACKUP_NAME}->${OLLAMA_CONTAINER}, starting original"
  docker rm -f "${OLLAMA_CONTAINER}" >/dev/null 2>&1 || true
  docker rename "${BACKUP_NAME}" "${OLLAMA_CONTAINER}" >/dev/null 2>&1 || say "CONC CRIT: rename backup->ollama failed (backup=${BACKUP_NAME})"
  docker start "${OLLAMA_CONTAINER}" >/dev/null 2>&1 || say "CONC CRIT: start original failed (name=${OLLAMA_CONTAINER})"
  wait_url "${OLLAMA_URL}/api/tags" 60 || say "CONC WARN: original not healthy after restore"
  OLLAMA_TOGGLED=0
  local np mc
  np=$(docker inspect "${OLLAMA_CONTAINER}" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -c OLLAMA_NUM_PARALLEL || true)
  mc=$(docker exec "${OLLAMA_CONTAINER}" ollama list 2>/dev/null | tail -n +2 | grep -c . || true)
  say "CONC restore verify: NUM_PARALLEL_env=${np} (expect 0)  models_listed=${mc}"
}
trap 'restore_parallel' EXIT

conc_sweep() { # spec
  local spec="$1" tag label; tag="$(tag_of "${spec}")"; label="$(label_of "${spec}")"
  if ! have_model "${tag}"; then say "CONC skip ${label}: model not present"; return 0; fi
  say "CONC sweep ${label} (${tag})"
  local first=1
  for cell in "1:8:1" "2:8:0" "4:4:0" "8:2:0"; do
    local c runs warm; c="${cell%%:*}"; runs="$(echo "${cell}" | cut -d: -f2)"; warm="$(echo "${cell}" | cut -d: -f3)"
    if timeout 1800 python3 "${PERF}/tokrate_probe.py" --backend-url "${OLLAMA_URL}" --api ollama \
          --model "${tag}" --max-tokens 128 --prompt-tokens 256 --num-ctx 4096 \
          --runs "${runs}" --concurrency "${c}" --warmup "${warm}" \
          --num-gpu 999 --no-use-mmap --label "conc-${label}-c${c}" \
          --out "${OUT}/conc-${label}-c${c}.json" >"${LOGS}/conc-${label}-c${c}.log" 2>&1; then
      say "CONC ok ${label} c${c}"
    else
      say "CONC FAIL ${label} c${c} (see log)"
    fi
    if [ "${first}" = "1" ]; then first=0; say "CONC ${label} residency=$(resident_frac "${tag}")"; fi
  done
  unload "${tag}"
}

# --------------------------------------------------------------------------- #
# agentic phase
# --------------------------------------------------------------------------- #
agentic_one() { # spec
  local spec="$1" tag label kind think; tag="$(tag_of "${spec}")"; label="$(label_of "${spec}")"; kind="$(kind_of "${spec}")"
  if ! have_model "${tag}"; then say "AGENTIC skip ${label}: model not present"; return 0; fi
  think=""; [ "${kind}" = "gemma" ] && think="--no-think"
  say "AGENTIC ${label} (${tag}) think='${think:-<omit>}'"
  # shellcheck disable=SC2086
  if timeout 2400 python3 "${PERF}/agentic_toolcall.py" --backend-url "${OLLAMA_URL}" --api ollama \
        --models "${tag}" ${think} --num-predict 512 --num-ctx 4096 --num-gpu 999 --no-use-mmap \
        --out "${OUT}/agentic-${label}.json" >"${LOGS}/agentic-${label}.log" 2>&1; then
    say "AGENTIC ok ${label} -> agentic-${label}.json"
  else
    say "AGENTIC FAIL ${label} (see log)"
  fi
  unload "${tag}"
}

# --------------------------------------------------------------------------- #
# quality phase (EXAONE / Phi-4 completion)
# --------------------------------------------------------------------------- #
quality_one() { # spec
  local spec="$1" tag label out think; tag="$(tag_of "${spec}")"; label="$(label_of "${spec}")"
  if ! have_model "${tag}"; then say "QUALITY skip ${label}: model not present"; return 0; fi
  out="${OUT}/quality-candidate-${label}.json"
  think="--no-think"
  if ! think_false_ok "${tag}"; then
    say "QUALITY ${label}: think:false NOT accepted -> omit think (last-Answer extraction still applies)"
    think=""
  fi
  say "QUALITY ${label} (${tag}) limit=${QUALITY_LIMIT} think='${think:-<omit>}'"
  # shellcheck disable=SC2086
  if timeout 9000 python3 "${PERF}/quant-quality.py" --backend-url "${OLLAMA_URL}" --api ollama \
        --models "${tag}" ${think} --num-predict 2048 --num-ctx 4096 --num-gpu 999 --no-use-mmap \
        --limit "${QUALITY_LIMIT}" --out "${out}" >"${LOGS}/quality-${label}.log" 2>&1; then
    # sanity: if every question errored (0 correct AND tiny wall) retry once w/o think
    local acc; acc=$(python3 -c "import json;d=json.load(open('${out}'));m=list(d['per_model'].values())[0];print(m.get('correct'),int(m.get('wall_s') or 0))" 2>/dev/null || echo "err 0")
    say "QUALITY ok ${label} (correct/wall=${acc}) -> quality-candidate-${label}.json"
  else
    say "QUALITY FAIL ${label} (see log) -- skip-with-reason"
  fi
  unload "${tag}"
}

# =========================================================================== #
# run
# =========================================================================== #
say "PROFILES_RUN_START pid=$$ host=$(hostname) perf=${PERF} out=${OUT}"
say "backup-container-name=${BACKUP_NAME}"

# phase 0: preflight
if ! wait_url "${OLLAMA_URL}/api/tags" 15; then say "PREFLIGHT FATAL: ollama not reachable"; exit 1; fi
say "PREFLIGHT ollama ok; free=$(free_gb)GB; models=$(docker exec "${OLLAMA_CONTAINER}" ollama list 2>/dev/null | tail -n +2 | grep -c . || true)"
# lightly wait out any competing GPU bench (bounded); the box is normally idle
for _ in $(seq 1 10); do
  if pgrep -f 'bestcfg-matrix\.sh|maxmodel-sweep\.sh|tokrate_probe\.py|quant-quality\.py|agentic_toolcall\.py' | grep -qv "^$$\$" 2>/dev/null; then
    say "PREFLIGHT waiting: a competing bench process is running"; sleep 60
  else break; fi
done

# phase 1: pull
say "PHASE pull"
for spec in "${PULL_SPECS[@]}"; do pull_one "${spec}" || true; done
say "PULL done; this run pulled: ${PULLED_TAGS[*]:-<none>}"

# phase 2: concurrency (toggle + restore)
say "PHASE concurrency"
if toggle_parallel; then
  for spec in "${CONC_SPECS[@]}"; do conc_sweep "${spec}"; done
else
  say "CONC ERR: toggle failed; skipping concurrency phase"
fi
restore_parallel

# phase 3: agentic
say "PHASE agentic"
for spec in "${AGENTIC_SPECS[@]}"; do agentic_one "${spec}"; done

# phase 4: quality
say "PHASE quality"
for spec in "${QUALITY_SPECS[@]}"; do quality_one "${spec}"; done

# phase 5: cleanup (remove ONLY tags this run pulled)
say "PHASE cleanup"
if [ "${REMOVE_PULLED}" = "1" ] && [ "${#PULLED_TAGS[@]}" -gt 0 ]; then
  for tag in "${PULLED_TAGS[@]}"; do
    if docker exec "${OLLAMA_CONTAINER}" ollama rm "${tag}" >/dev/null 2>&1; then say "CLEANUP removed ${tag}"; else say "CLEANUP could not remove ${tag}"; fi
  done
else
  say "CLEANUP: keeping pulled models (REMOVE_PULLED=${REMOVE_PULLED})"
fi
say "CLEANUP free=$(free_gb)GB"

say "PROFILES_RUN_DONE pid=$$ outputs in ${OUT}"
