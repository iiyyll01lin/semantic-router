#!/usr/bin/env bash
#
# make-mtls-certs.sh — provision the opt-in mTLS material for the strix-halo fleet
# (Part C1, completes R5). Uses openssl ONLY (no Python/third-party deps) to mint:
#
#   * a self-signed CA — the single trust anchor for BOTH directions,
#   * a CCP server certificate (SAN-bound to the Halo-A host/IP + localhost) that
#     the agents verify, and
#   * one client certificate per agent box that each agent PRESENTS to the CCP
#     (so a CCP started with CCP_TLS_CLIENT_CA accepts the connection).
#
# It only WRITES files and prints exactly which env var each maps to; it wires
# nothing on. mTLS stays OPT-IN — you enable it by pointing the CCP_TLS_* (server)
# and FLEET_TLS_* (client) vars at these files. See docs/security-hardening.md.
#
# Usage:
#   bash make-mtls-certs.sh --host <halo-a-host-or-ip> [options]
#
# Options (env fallback in parentheses):
#   --host   H        CCP host/IP the agents connect to; added to the server cert
#                     SAN so hostname verification passes.   (MTLS_HOST / HALO_A_IP)
#   --agents "a b c"  space-separated agent box ids to mint a client cert for.
#                     (MTLS_AGENTS)                          [default: "halo-a halo-b"]
#   --out    DIR      output directory for the generated material.
#                     (MTLS_OUT_DIR)                         [default: ./mtls-certs]
#   --days   N        validity in days for every certificate.
#                     (MTLS_DAYS)                            [default: 825]
#   --force           regenerate the CA even if one already exists in --out
#                     (re-run WITHOUT --force to just add more agent certs).
#   -h, --help        show this help and exit.
#
set -euo pipefail

OUT_DIR="${MTLS_OUT_DIR:-./mtls-certs}"
DAYS="${MTLS_DAYS:-825}"
HOST="${MTLS_HOST:-${HALO_A_IP:-}}"
AGENTS="${MTLS_AGENTS:-halo-a halo-b}"
FORCE=0

usage() {
  cat <<'USAGE'
Usage: bash make-mtls-certs.sh --host <halo-a-host-or-ip> [options]

  --host   H        CCP host/IP for the server-cert SAN   (MTLS_HOST / HALO_A_IP)
  --agents "a b c"  agent box ids to mint client certs for (MTLS_AGENTS)
                                                          [default: "halo-a halo-b"]
  --out    DIR      output directory                       (MTLS_OUT_DIR) [./mtls-certs]
  --days   N        certificate validity in days           (MTLS_DAYS)    [825]
  --force           regenerate the CA even if one exists
  -h, --help        show this help
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --host)   HOST="${2:?--host needs a value}"; shift 2 ;;
    --agents) AGENTS="${2:?--agents needs a value}"; shift 2 ;;
    --out)    OUT_DIR="${2:?--out needs a value}"; shift 2 ;;
    --days)   DAYS="${2:?--days needs a value}"; shift 2 ;;
    --force)  FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

command -v openssl >/dev/null 2>&1 || { echo "ERROR: openssl not found on PATH" >&2; exit 1; }
if [ -z "${HOST}" ]; then
  echo "ERROR: set the CCP host/IP via --host (or MTLS_HOST / HALO_A_IP)." >&2
  usage; exit 2
fi

mkdir -p "${OUT_DIR}"
ABS_OUT="$(cd "${OUT_DIR}" && pwd)"

# Scratch dir for CSRs + ext files (never part of the deliverable material).
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mtls-certs.XXXXXX")"
cleanup() { rm -rf "${TMP_DIR}"; }
trap cleanup EXIT

CA_KEY="${OUT_DIR}/ca-key.pem"
CA_CERT="${OUT_DIR}/ca-cert.pem"
CA_SERIAL="${OUT_DIR}/ca-cert.srl"

# --- 1. self-signed CA (reused across runs unless --force) --------------------
if [ -s "${CA_CERT}" ] && [ -s "${CA_KEY}" ] && [ "${FORCE}" != "1" ]; then
  echo "==> reusing existing CA: ${CA_CERT} (pass --force to regenerate)"
else
  echo "==> generating self-signed CA (${DAYS}d validity)"
  # Mark it a real CA (basicConstraints CA:TRUE + keyCertSign); OpenSSL 3.x
  # rejects a chain whose issuer lacks these when the leaves carry keyUsage.
  openssl req -x509 -newkey rsa:2048 -nodes -days "${DAYS}" \
    -keyout "${CA_KEY}" -out "${CA_CERT}" \
    -subj "/CN=strix-halo-fleet-ca" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1
  chmod 600 "${CA_KEY}"
fi

# Sign a CSR with the CA. $1=csr $2=out-cert $3=ext-file
_sign() {
  openssl x509 -req -in "$1" -out "$2" \
    -CA "${CA_CERT}" -CAkey "${CA_KEY}" -CAcreateserial -CAserial "${CA_SERIAL}" \
    -days "${DAYS}" -extfile "$3" >/dev/null 2>&1
}

# --- 2. CCP server cert (SAN = host + localhost) ------------------------------
SAN="DNS:localhost,IP:127.0.0.1"
if printf '%s' "${HOST}" | grep -Eq '^[0-9]+(\.[0-9]+){3}$'; then
  SAN="${SAN},IP:${HOST}"
else
  SAN="${SAN},DNS:${HOST}"
fi
echo "==> issuing CCP server cert (CN=ccp, SAN=${SAN})"
printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n' \
  "${SAN}" >"${TMP_DIR}/server.ext"
openssl req -newkey rsa:2048 -nodes -subj "/CN=ccp" \
  -keyout "${OUT_DIR}/ccp-key.pem" -out "${TMP_DIR}/ccp.csr" >/dev/null 2>&1
_sign "${TMP_DIR}/ccp.csr" "${OUT_DIR}/ccp-cert.pem" "${TMP_DIR}/server.ext"
chmod 600 "${OUT_DIR}/ccp-key.pem"

# --- 3. per-agent client certs -----------------------------------------------
printf 'extendedKeyUsage=clientAuth\nkeyUsage=digitalSignature\n' >"${TMP_DIR}/client.ext"
read -ra _agents <<<"${AGENTS}"
for agent in "${_agents[@]}"; do
  echo "==> issuing client cert for agent '${agent}' (CN=${agent})"
  openssl req -newkey rsa:2048 -nodes -subj "/CN=${agent}" \
    -keyout "${OUT_DIR}/${agent}-client-key.pem" -out "${TMP_DIR}/${agent}.csr" >/dev/null 2>&1
  _sign "${TMP_DIR}/${agent}.csr" "${OUT_DIR}/${agent}-client-cert.pem" "${TMP_DIR}/client.ext"
  chmod 600 "${OUT_DIR}/${agent}-client-key.pem"
done

# --- summary: exactly where each file goes -----------------------------------
cat <<EOF

mTLS material written to ${ABS_OUT}

  Trust anchor (CA)
    ca-cert.pem   CCP    -> CCP_TLS_CLIENT_CA=${ABS_OUT}/ca-cert.pem   (verify agent client certs)
                  agents -> FLEET_TLS_CA=${ABS_OUT}/ca-cert.pem        (verify the CCP server cert)
    ca-key.pem    SECRET -> keep OFFLINE; only needed to mint more certs (never deploy it)

  CCP server cert (stays on Halo-A)
    ccp-cert.pem  CCP    -> CCP_TLS_CERT=${ABS_OUT}/ccp-cert.pem
    ccp-key.pem   CCP    -> CCP_TLS_KEY=${ABS_OUT}/ccp-key.pem          (SECRET; never leaves Halo-A)

  Per-agent client certs (stage each on ITS OWN box, at the path you export there)
EOF
for agent in "${_agents[@]}"; do
  printf '    %-26s %s -> FLEET_TLS_CLIENT_CERT\n' "${agent}-client-cert.pem" "${agent}"
  printf '    %-26s %s -> FLEET_TLS_CLIENT_KEY   (SECRET)\n' "${agent}-client-key.pem" "${agent}"
done
cat <<EOF

Enable mTLS end-to-end (all OPT-IN; unset = today's HMAC-over-HTTP):
  # On the CCP (Halo-A):
  export CCP_TLS_CERT=${ABS_OUT}/ccp-cert.pem CCP_TLS_KEY=${ABS_OUT}/ccp-key.pem
  export CCP_TLS_CLIENT_CA=${ABS_OUT}/ca-cert.pem
  # On EACH agent box (stage that box's client cert + the CA locally first):
  export FLEET_TLS_CA=<ca-cert.pem> \\
         FLEET_TLS_CLIENT_CERT=<this-box-client-cert.pem> \\
         FLEET_TLS_CLIENT_KEY=<this-box-client-key.pem>
  # Then run deploy-fleet-2box.sh as usual: it forwards the FLEET_TLS_* vars to
  # remotes and builds https:// CCP URLs automatically once CCP_TLS_CERT/KEY are set.

These are secret key material — keep them out of git (already covered by .gitignore).
EOF
