#!/usr/bin/env bash
#
# 2-box Strix Halo PoC: WAN-latency contrast experiment.
#
# Strengthens the "router at the edge" argument. On the LAN the per-hop cost of
# escalating to Halo-B is ~0.2 ms, which understates the edge advantage. This
# script injects synthetic WAN latency on Halo-B with `tc netem` and re-measures
# the network hop from Halo-A, so the demo can show how the cost of a datacenter
# escalation grows under WAN conditions while routine (edge-served) traffic --
# 0 network hops -- stays unaffected.
#
# For each delay D in {0, 20, 50} ms it:
#   1. applies `tc qdisc replace dev <iface> root netem delay ${D}ms` on Halo-B
#      (over SSH; iface auto-detected from `ip route`, or set HALO_B_IFACE),
#   2. measures the pure network round-trip from Halo-A: median of N curls to
#      Halo-B's Ollama /api/tags,
#   3. optionally times a couple of gateway chat requests (if GATEWAY_URL is up),
# then prints a table of D vs measured per-hop latency.
#
# A cleanup trap ALWAYS removes the netem qdisc on Halo-B on exit (even on
# error / Ctrl-C), so Halo-B is never left with injected latency.
#
# Inputs (env vars):
#   HALO_B_IP        (required) data-plane Ollama address of Halo-B.
#   HALO_B_SSH       control address user@host; defaults its host to HALO_B_IP.
#   HALO_B_SSH_PORT  optional SSH port for Halo-B.
#   HALO_B_SSH_KEY   optional SSH identity file for Halo-B.
#   HALO_B_IFACE     optional NIC on Halo-B to shape (else auto-detect).
#   DELAYS           optional space-separated delays in ms. Default: "0 20 50".
#   SAMPLES          optional curls per measurement. Default: 10.
#   GATEWAY_URL      optional gateway base URL for end-to-end chat timing.
#
# Requires sudo on Halo-B (for `tc`) and SSH access from Halo-A. Run on Halo-A.
#
# Usage:
#   HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 bash wan-latency-experiment.sh
set -euo pipefail

OLLAMA_PORT="11434"
DELAYS="${DELAYS:-0 20 50}"
SAMPLES="${SAMPLES:-10}"

# --------------------------------------------------------------------------
if [[ -z "${HALO_B_IP:-}" ]]; then
  echo "ERROR: HALO_B_IP is not set (the data-plane address of Halo-B)." >&2
  echo "       Re-run with, e.g.:" >&2
  echo "         HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 bash wan-latency-experiment.sh" >&2
  exit 1
fi
HALO_B_SSH="${HALO_B_SSH:-${HALO_B_IP}}"

for bin in ssh curl; do
  if ! command -v "${bin}" >/dev/null 2>&1; then
    echo "ERROR: required command '${bin}' not found on Halo-A." >&2
    exit 1
  fi
done

# Shared SSH options (same convention as deploy-2box.sh: multiplexed master,
# optional identity file, separate port flag).
SSH_CTRL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vllm-sr-wan-ssh.XXXXXX")"
SSH_BASE_OPTS=(
  -o ControlMaster=auto
  -o ControlPath="${SSH_CTRL_DIR}/cm-%r@%h:%p"
  -o ControlPersist=2m
)
SSH_PORT_OPTS=()
if [[ -n "${HALO_B_SSH_KEY:-}" ]]; then
  SSH_BASE_OPTS+=(-i "${HALO_B_SSH_KEY}")
fi
if [[ -n "${HALO_B_SSH_PORT:-}" ]]; then
  SSH_PORT_OPTS=(-p "${HALO_B_SSH_PORT}")
fi

ssh_b() {
  ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" "${HALO_B_SSH}" "$@"
}

echo "==> Opening SSH master to ${HALO_B_SSH} (you may be prompted once)"
if ! ssh_b true; then
  echo "ERROR: cannot SSH to ${HALO_B_SSH}. Install your key once with:" >&2
  if [[ -n "${HALO_B_SSH_PORT:-}" ]]; then
    echo "         ssh-copy-id -p ${HALO_B_SSH_PORT} ${HALO_B_SSH}" >&2
  else
    echo "         ssh-copy-id ${HALO_B_SSH}" >&2
  fi
  exit 1
fi

# Resolve the NIC to shape on Halo-B: explicit override, else the iface of the
# default route (the one carrying traffic back to Halo-A).
IFACE="${HALO_B_IFACE:-}"
if [[ -z "${IFACE}" ]]; then
  IFACE="$(ssh_b "ip route show default 2>/dev/null | awk '/default/ {print \$5; exit}'")" || true
fi
if [[ -z "${IFACE}" ]]; then
  echo "ERROR: could not auto-detect Halo-B's default-route NIC." >&2
  echo "       Set it explicitly: HALO_B_IFACE=eth0 ... bash wan-latency-experiment.sh" >&2
  exit 1
fi
echo "    shaping NIC on Halo-B: ${IFACE}"

# ALWAYS remove the netem qdisc on Halo-B on exit (idempotent; failure ignored).
cleanup() {
  echo "==> Cleaning up: removing netem qdisc on Halo-B (${IFACE})"
  ssh_b "sudo tc qdisc del dev ${IFACE} root netem" >/dev/null 2>&1 || true
  ssh "${SSH_BASE_OPTS[@]}" "${SSH_PORT_OPTS[@]}" -O exit "${HALO_B_SSH}" >/dev/null 2>&1 || true
  rm -rf "${SSH_CTRL_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

# Median of SAMPLES network round-trips (curl total time, ms) to a URL.
measure_median_ms() {
  local url="$1" times=() t
  for _ in $(seq 1 "${SAMPLES}"); do
    t="$(curl -o /dev/null -s -w '%{time_total}' --max-time 10 "${url}" 2>/dev/null || echo "")"
    [[ -n "${t}" ]] && times+=("${t}")
  done
  if [[ "${#times[@]}" -eq 0 ]]; then
    echo "NA"
    return
  fi
  printf '%s\n' "${times[@]}" | sort -n | awk '
    { v[NR]=$1 }
    END {
      n=NR
      if (n % 2) m=v[(n+1)/2]
      else m=(v[n/2]+v[n/2+1])/2
      printf "%.1f", m*1000
    }'
}

TAGS_URL="http://${HALO_B_IP}:${OLLAMA_PORT}/api/tags"
declare -a RESULTS=()

echo "==> Running WAN-latency sweep: delays = [${DELAYS}] ms, samples = ${SAMPLES}"
for D in ${DELAYS}; do
  if [[ "${D}" == "0" ]]; then
    echo "    [D=0ms] baseline: removing any netem qdisc"
    ssh_b "sudo tc qdisc del dev ${IFACE} root netem" >/dev/null 2>&1 || true
  else
    echo "    [D=${D}ms] applying: tc qdisc replace dev ${IFACE} root netem delay ${D}ms"
    if ! ssh_b "sudo tc qdisc replace dev ${IFACE} root netem delay ${D}ms"; then
      echo "ERROR: failed to apply netem on Halo-B (${IFACE}). Needs sudo/tc on Halo-B." >&2
      exit 1
    fi
  fi
  hop_ms="$(measure_median_ms "${TAGS_URL}")"
  echo "        median network hop to Halo-B Ollama: ${hop_ms} ms"

  chat_ms="-"
  if [[ -n "${GATEWAY_URL:-}" ]]; then
    chat_ms="$(measure_median_ms "${GATEWAY_URL}")"
    echo "        median gateway round-trip (${GATEWAY_URL}): ${chat_ms} ms"
  fi
  RESULTS+=("${D}|${hop_ms}|${chat_ms}")
done

echo
echo "============================================================"
echo "WAN-latency contrast (Halo-A -> Halo-B, NIC ${IFACE})"
echo "------------------------------------------------------------"
printf '%-12s %-22s %-22s\n' "netem(ms)" "net hop median(ms)" "gateway median(ms)"
for row in "${RESULTS[@]}"; do
  IFS='|' read -r d hop chat <<< "${row}"
  printf '%-12s %-22s %-22s\n' "${d}" "${hop}" "${chat}"
done
echo "------------------------------------------------------------"
echo "Edge-served (routine) requests take 0 network hops and are unaffected by"
echo "the injected delay; only datacenter escalations pay the added WAN cost."
echo "============================================================"
