#!/usr/bin/env bash
#
# make-vram-resident-models.sh -- build "<tag>-vram" Modelfile VARIANTS that pin a
# model 100% into the VRAM carveout by baking in `num_gpu 999` + `use_mmap false`.
#
# WHY: on a unified-memory APU with a large VRAM carveout (e.g. Halo-B at 96 GiB),
# Ollama's auto layer-estimate sizes GPU layers to OS-visible SYSTEM RAM (only ~30
# GiB at 96 GiB carveout), so by default it CPU-offloads big models even though tens
# of GiB of VRAM sit free -- collapsing decode throughput. Overriding with
# `num_gpu=999` (pin every layer on GPU) + `use_mmap=false` (load weights straight
# into the carveout instead of mmap'ing from disk) makes the model 100% VRAM-resident
# and fast. Per-request options prove this (see maxmodel-sweep.sh NUM_GPU/USE_MMAP),
# but to make it the DEFAULT for a model we persist it in a Modelfile variant.
#
# For each source tag it writes a tiny derived Modelfile -- `FROM <tag>` +
# `PARAMETER num_gpu 999` + `PARAMETER use_mmap false` -- and runs `ollama create
# <tag>-vram -f <file>`. `FROM <tag>` (ollama's documented derive form) INHERITS the
# base template/params and just overlays our two PARAMETERs, so create is instant
# (it dedupes the existing weight layers). NB: deriving from the raw blob path that
# `ollama show --modelfile` prints instead makes ollama 0.30.10 re-validate the GGUF
# (`llama-quantize failed`) for MXFP4/Q8 models, and `-f -` (stdin) is not accepted,
# so we use `FROM <tag>` + a real file. The ORIGINAL tag is left untouched
# (non-destructive; the auto-estimate behavior stays available for A/Bs).
#
# It then (VERIFY=1, default) loads each variant with a 1-token decode and reads
# /api/ps to confirm the split is 100% GPU (size_vram == size), and prints the
# card VRAM used from sysfs when available.
#
# SAFETY: `ollama create` is cheap (a new manifest over the SAME weight blobs -- it
# does NOT duplicate the ~60-80 GB of weights). The VERIFY step DOES load the model
# into VRAM, so run it when the box has the headroom (e.g. during a maintenance
# window), not alongside another heavy stack.
#
# Env (all optional):
#   TAGS            space-separated source tags to convert
#                   (default "gpt-oss:120b llama3.1:70b-instruct-q8_0")
#   SUFFIX          variant tag suffix                 (default "-vram")
#   NUM_GPU         baked num_gpu PARAMETER            (default 999)
#   USE_MMAP        baked use_mmap PARAMETER            (default false)
#   OLLAMA_URL      direct inference-server base       (default http://localhost:11434)
#   OLLAMA_CONTAINER  ollama container name            (default ollama)
#   VERIFY          1 => load each variant + check /api/ps is 100% GPU (default 1)
#   VRAM_CARD       DRM card for the sysfs VRAM read   (default card1 on Halo-B;
#                   amd-smi reports it as GPU 0 but the compute card is card1)
#   KEEP_LOADED     1 => leave the last verified variant loaded (default 0 = unload)
#
# Usage (on the box, Ollama up):
#   bash make-vram-resident-models.sh
#   TAGS="gpt-oss:120b" VERIFY=0 bash make-vram-resident-models.sh   # create only
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=/dev/null
source "${RECIPE_DIR}/fleet_common.sh"
PY_BIN="$(fleet_pybin)"

TAGS="${TAGS:-gpt-oss:120b llama3.1:70b-instruct-q8_0}"
SUFFIX="${SUFFIX:--vram}"
NUM_GPU="${NUM_GPU:-999}"
USE_MMAP="${USE_MMAP:-false}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-ollama}"
VERIFY="${VERIFY:-1}"
VRAM_CARD="${VRAM_CARD:-card1}"
KEEP_LOADED="${KEEP_LOADED:-0}"

echo "==> [make-vram-resident] tags: ${TAGS}"
echo "    variant suffix='${SUFFIX}'  PARAMETER num_gpu ${NUM_GPU}  PARAMETER use_mmap ${USE_MMAP}"
echo "    ollama=${OLLAMA_URL} (container '${OLLAMA_CONTAINER}')  verify=${VERIFY}"

if ! curl -fsS --max-time 5 "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
  echo "ERROR: Ollama is not answering on ${OLLAMA_URL} -- bring the backend up first." >&2
  exit 1
fi

read_vram_used_gib() {  # best-effort sysfs read of the compute card's VRAM (GiB)
  local path="/sys/class/drm/${VRAM_CARD}/device/mem_info_vram_used" bytes
  bytes="$(docker exec "${OLLAMA_CONTAINER}" cat "${path}" 2>/dev/null || cat "${path}" 2>/dev/null || true)"
  if [[ "${bytes}" =~ ^[0-9]+$ ]]; then
    awk -v b="${bytes}" 'BEGIN{printf "%.2f", b/1073741824}'
  else
    echo "n/a"
  fi
}

created=()
failed=()
mmap_accepted="unknown"

for tag in ${TAGS}; do
  vtag="${tag}${SUFFIX}"
  echo "==> [make-vram-resident] ${tag} -> ${vtag}"

  if ! docker exec "${OLLAMA_CONTAINER}" ollama show "${tag}" >/dev/null 2>&1; then
    echo "    SKIP: source model '${tag}' not present (pull it first)." >&2
    failed+=("${tag} (missing)")
    continue
  fi

  # Derive from the TAG (not the blob path): inherits template/params, overlays ours.
  # ollama create needs a real file (no stdin), so stage the Modelfile in-container.
  cmf="/tmp/vram-$(echo "${vtag}" | tr '/:.' '___').Modelfile"
  printf 'FROM %s\nPARAMETER num_gpu %s\nPARAMETER use_mmap %s\n' \
    "${tag}" "${NUM_GPU}" "${USE_MMAP}" \
    | docker exec -i "${OLLAMA_CONTAINER}" sh -c "cat > '${cmf}'"

  create_out="$(docker exec "${OLLAMA_CONTAINER}" ollama create "${vtag}" -f "${cmf}" 2>&1)"
  create_rc=$?
  docker exec "${OLLAMA_CONTAINER}" rm -f "${cmf}" >/dev/null 2>&1 || true
  if [[ ${create_rc} -eq 0 ]]; then
    echo "    created ${vtag}"
  else
    echo "    ERROR: 'ollama create ${vtag}' failed:" >&2
    printf '%s\n' "${create_out}" | tail -3 | sed 's/^/      /' >&2
    failed+=("${vtag} (create failed)")
    continue
  fi

  # Verify the persisted Modelfile really kept both PARAMETERs. If use_mmap is
  # missing, this Ollama build rejected/dropped the PARAMETER (the thing the plan
  # asks us to confirm on 0.30.10).
  created_mf="$(docker exec "${OLLAMA_CONTAINER}" ollama show --modelfile "${vtag}" 2>/dev/null)"
  has_numgpu="no"; has_mmap="no"
  printf '%s\n' "${created_mf}" | grep -qiE '^[[:space:]]*PARAMETER[[:space:]]+num_gpu[[:space:]]' && has_numgpu="yes"
  printf '%s\n' "${created_mf}" | grep -qiE '^[[:space:]]*PARAMETER[[:space:]]+use_mmap[[:space:]]' && has_mmap="yes"
  echo "    persisted PARAMETERs: num_gpu=${has_numgpu} use_mmap=${has_mmap}"
  if [[ "${has_mmap}" == "yes" ]]; then
    mmap_accepted="yes"
  elif [[ "${mmap_accepted}" != "yes" ]]; then
    mmap_accepted="no"
    echo "    WARNING: this Ollama build did NOT persist 'PARAMETER use_mmap' -- pass" >&2
    echo "             use_mmap=false as a per-request option instead (see maxmodel-sweep)." >&2
  fi
  created+=("${vtag}")

  if [[ "${VERIFY}" == "1" ]]; then
    echo "    verifying residency (loading ${vtag}; may take ~30 s for a big model) ..."
    vram_before="$(read_vram_used_gib)"
    # Load with the variant's baked PARAMETERs (no per-request num_gpu/use_mmap, so
    # this proves the Modelfile itself pins the model on GPU).
    curl -sS --max-time 600 "${OLLAMA_URL}/api/generate" \
      -d "{\"model\":\"${vtag}\",\"prompt\":\"ok\",\"stream\":false,\"options\":{\"num_predict\":1}}" \
      >/dev/null 2>&1 || echo "    WARNING: load/generate call did not return cleanly" >&2
    vram_after="$(read_vram_used_gib)"

    # /api/ps: size_vram/size == 1.0 means the whole model sits in VRAM (100% GPU).
    ps_json="$(curl -fsS --max-time 15 "${OLLAMA_URL}/api/ps" 2>/dev/null || echo '{}')"
    echo "    ollama ps:"
    docker exec "${OLLAMA_CONTAINER}" ollama ps 2>/dev/null | sed 's/^/      /' || true
    printf '%s' "${ps_json}" | "${PY_BIN}" - "${vtag}" <<'PYEOF'
import json, sys
vtag = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
row = next((m for m in data.get("models", []) if m.get("name") == vtag), None)
if not row:
    print("    /api/ps: %s not resident (load may have failed)" % vtag)
    sys.exit(0)
size = row.get("size") or 0
vram = row.get("size_vram") or 0
gpu_pct = (100.0 * vram / size) if size else 0.0
verdict = "100% GPU" if size and vram >= size else ("%.0f%% GPU / %.0f%% CPU" % (gpu_pct, 100 - gpu_pct))
print("    /api/ps split: %s  (size=%.2f GiB, size_vram=%.2f GiB)"
      % (verdict, size / 1073741824, vram / 1073741824))
PYEOF
    echo "    sysfs ${VRAM_CARD} VRAM used: ${vram_before} -> ${vram_after} GiB"

    if [[ "${KEEP_LOADED}" != "1" ]]; then
      curl -sS --max-time 30 "${OLLAMA_URL}/api/generate" \
        -d "{\"model\":\"${vtag}\",\"keep_alive\":0}" >/dev/null 2>&1 || true
      docker exec "${OLLAMA_CONTAINER}" ollama stop "${vtag}" >/dev/null 2>&1 || true
    fi
  fi
done

echo "==> [make-vram-resident] summary"
echo "    created variants : ${created[*]:-<none>}"
echo "    failed / skipped : ${failed[*]:-<none>}"
echo "    use_mmap PARAMETER accepted by this Ollama build: ${mmap_accepted}"
if [[ ${#created[@]} -eq 0 ]]; then
  exit 1
fi
