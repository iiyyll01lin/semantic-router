#!/usr/bin/env bash
# Generate and validate the PoC routing DSL from poc-strix.yaml.
#
# The router's routing surface (routing.*) can be expressed as a compact DSL.
# This script decompiles poc-strix.yaml into poc-strix.dsl and then validates
# that DSL with the Go cmd/dsl tool. The .dsl is a GENERATED artifact: it is
# produced here on the Strix Halo (where Go exists) and is NOT committed
# (see .gitignore). Only routing.* is encoded in the DSL; providers/global
# stay in YAML.
#
# Usage (from anywhere):
#   bash deploy/recipes/strix-halo-poc/gen-dsl.sh
#
# Requires: Go (for `go run ./cmd/dsl ...`). This does NOT run the router and
# does NOT need a GPU; it is the Go-side static check for the PoC config.
set -euo pipefail

# Resolve paths relative to this script so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
ROUTER_DIR="${REPO_ROOT}/src/semantic-router"

# Paths relative to ROUTER_DIR, matching the runbook's documented invocation.
YAML_REL="../../deploy/recipes/strix-halo-poc/poc-strix.yaml"
DSL_REL="../../deploy/recipes/strix-halo-poc/poc-strix.dsl"

if ! command -v go >/dev/null 2>&1; then
  echo "ERROR: 'go' not found. Run this on the Strix Halo where Go is installed." >&2
  exit 1
fi

cd "${ROUTER_DIR}"

echo "==> [1/2] Decompiling poc-strix.yaml -> poc-strix.dsl"
go run ./cmd/dsl decompile -o "${DSL_REL}" "${YAML_REL}"

echo "==> [2/2] Validating poc-strix.dsl"
go run ./cmd/dsl validate "${DSL_REL}"

echo
echo "DSL generated and validated: ${SCRIPT_DIR}/poc-strix.dsl"
echo "(This .dsl is a generated artifact and is intentionally not committed.)"
