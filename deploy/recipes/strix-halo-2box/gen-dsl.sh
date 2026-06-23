#!/usr/bin/env bash
# Generate and validate the routing DSL from poc-client-edge.yaml.
#
# The router's routing surface (routing.*) can be expressed as a compact DSL.
# This script decompiles poc-client-edge.yaml into poc-client-edge.dsl and then
# validates that DSL with the Go cmd/dsl tool. The .dsl is a GENERATED artifact:
# it is produced here on the Strix Halo (where Go exists) and is NOT committed
# (see .gitignore). Only routing.* is encoded in the DSL; providers/global stay
# in YAML.
#
# Note: the committed poc-client-edge.yaml keeps a literal HALO_B_IP placeholder
# in providers.* (backend endpoints). The DSL only encodes routing.*, so the
# placeholder does not affect decompile/validate; this is a static check of the
# routing surface only.
#
# Usage (from anywhere):
#   bash deploy/recipes/strix-halo-2box/gen-dsl.sh
#
# Requires: Go (for `go run ./cmd/dsl ...`). This does NOT run the router and
# does NOT need a GPU; it is the Go-side static check for the edge config.
set -euo pipefail

# Resolve paths relative to this script so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
ROUTER_DIR="${REPO_ROOT}/src/semantic-router"

# The cmd/dsl tool is CGO-linked against the Rust candle binding, so the
# dynamic loader (and the go-run link step) must be told where the prebuilt
# .so files live. This matches the convention in tools/make/build-run-test.mk.
# Note: handle an unset LD_LIBRARY_PATH safely under `set -u`.
export LD_LIBRARY_PATH="${REPO_ROOT}/candle-binding/target/release:${REPO_ROOT}/ml-binding/target/release:${REPO_ROOT}/nlp-binding/target/release${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export CGO_LDFLAGS="-L${REPO_ROOT}/candle-binding/target/release -L${REPO_ROOT}/ml-binding/target/release -L${REPO_ROOT}/nlp-binding/target/release"

# Guard: the candle binding must be built first, otherwise the loader emits a
# cryptic "cannot open shared object file" error. Convert that into an
# actionable instruction.
CANDLE_LIB="${REPO_ROOT}/candle-binding/target/release/libcandle_semantic_router.so"
if [[ ! -f "${CANDLE_LIB}" ]]; then
  echo "ERROR: missing ${CANDLE_LIB}" >&2
  echo "       The cmd/dsl tool is CGO-linked against the Rust candle binding." >&2
  echo "       Build the bindings first, then re-run this script:" >&2
  echo "         make rust      # GPU/CUDA build" >&2
  echo "         make rust-ci   # CPU-only build" >&2
  exit 1
fi

# Paths relative to ROUTER_DIR, matching the runbook's documented invocation.
YAML_REL="../../deploy/recipes/strix-halo-2box/poc-client-edge.yaml"
DSL_REL="../../deploy/recipes/strix-halo-2box/poc-client-edge.dsl"

if ! command -v go >/dev/null 2>&1; then
  echo "ERROR: 'go' not found. Run this on the Strix Halo where Go is installed." >&2
  exit 1
fi

cd "${ROUTER_DIR}"

echo "==> [1/2] Decompiling poc-client-edge.yaml -> poc-client-edge.dsl"
go run ./cmd/dsl decompile -o "${DSL_REL}" "${YAML_REL}"

echo "==> [2/2] Validating poc-client-edge.dsl"
go run ./cmd/dsl validate "${DSL_REL}"

echo
echo "DSL generated and validated: ${SCRIPT_DIR}/poc-client-edge.dsl"
echo "(This .dsl is a generated artifact and is intentionally not committed.)"
