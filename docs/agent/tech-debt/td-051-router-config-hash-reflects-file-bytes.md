# TD051: Router `/config/hash` Reflects Config File Bytes, Not the Loaded Config

## Status

Closed - Option B implemented and validated on the 2-box Strix Halo fleet.

## Owner Plan

PL0036 Edge Fleet Config Control Plane

## Release Relevance

None - edge-fleet control-plane convergence/rollback correctness. The router now
keeps `/config/hash` as the file-byte signal and exposes `/config/loaded-hash`
for the last successfully loaded runtime config.

## Scope

The router config-reload observability surface the edge fleet agent polls to
decide convergence and auto-rollback: `GET /config/hash` (the file-byte
drift/convergence signal), `GET /config/loaded-hash` (the runtime loaded-config
signal), and the fsnotify reload-failure path (what the router does when a
hot-reload cannot parse the new config).

## Summary

`GET /config/hash` computes `sha256` over the config **file bytes on disk**, not
over the config the router actually parsed and is serving. On an invalid
hot-reload the router logs the failure and keeps serving the last-good in-memory
config **without reverting the on-disk file**, so `/config/hash` still reports
the hash of the bad bytes. Any consumer that treats "`/config/hash` == desired
hash" as "the router adopted the desired config" is therefore wrong for a config
the router refused to load: the file converged but the running config did not.

This directly broke the edge fleet's R8 auto-rollback. The agent pushed an
invalid-YAML config, `/config/hash` immediately matched the (bad) desired hash,
the router kept answering 200, and the agent recorded `applied` instead of
`rolled_back` - so auto-rollback could never fire on the real gateway. It was
closed at the fleet layer (Option A) by having the agent additionally require
`GET /config/router == 200` (that endpoint parses the active config and returns
500 on invalid YAML) before treating an apply as healthy. That workaround makes
rollback fire, but the underlying router signal is still misleading:
convergence/rollback correctness now depends on a second endpoint plus an
agent-side gate rather than `/config/hash` meaning what its name implies.

Option B is now implemented: `/config/hash` intentionally remains file-byte
scoped for compatibility, while `/config/loaded-hash` hashes the current loaded
runtime config from `currentConfig()`. The fleet agent now waits on that loaded
signal before reporting an apply; if the loaded hash never advances, it rolls
back unloadable config and reports `rolled_back`. Semantically unchanged but
loadable byte edits are handled by a delayed compatibility fallback so comment
or formatting-only convergence remains valid.

## Evidence

- [src/semantic-router/pkg/apiserver/route_config_deploy.go](../../../src/semantic-router/pkg/apiserver/route_config_deploy.go) - `handleConfigHash` (lines ~260-276): `data, err := os.ReadFile(paths.sourcePath)` then `hash := sha256.Sum256(data)` - the reported hash is over the on-disk file bytes, independent of whether the router could parse/load them.
- [src/semantic-router/pkg/apiserver/route_config_deploy.go](../../../src/semantic-router/pkg/apiserver/route_config_deploy.go) - `handleLoadedConfigHash` / `loadedConfigHash`: `GET /config/loaded-hash` hashes the canonical loaded runtime config returned by `currentConfig()` and returns `{"hash":"<64 hex>","source":"loaded"}`. It does not read the config file bytes.
- [src/semantic-router/pkg/apiserver/routes.go](../../../src/semantic-router/pkg/apiserver/routes.go) - registers `GET /config/loaded-hash` beside `GET /config/hash`.
- [deploy/recipes/strix-halo-fleet-2box/fleet_agent.py](../../../deploy/recipes/strix-halo-fleet-2box/fleet_agent.py) - the apply path waits for `/config/hash` file-byte convergence and then waits for `/config/loaded-hash`; unloadable configs now fail with a loaded-hash mismatch path and restore `.bak`.
- Hardware evidence: `/tmp/vllm-sr-fleet/run-20260717-142520` plus `/tmp/vllm-sr-fleet/verify-hardening-20260717-142737.log`. Results: `verify_local.py` 20/20, `PASS: deploy + verify completed`, `verify-hardening summary: 8 passed, 0 failed, 1 skipped`, R8 `[PASS] auto-rollback`, and final fleet status `desired_version=v33` with both `halo-a` and `halo-b` `applied` at hash `298d9463cdb3`.

## Why It Matters

- `/config/hash` is named and used as a "did the reload complete" signal, but it
  only proves the file was written, not that the router loaded it. Any tooling
  (fleet CCP agents, tuning pipelines) that trusts it can mistake an unadopted
  config for a live one.
- The edge fleet's auto-rollback (R8) must distinguish "bytes written" from
  "runtime adopted"; otherwise an invalid config can be reported as `applied`
  even though every box kept serving the previous config.
- Consumers now have explicit contracts: `/config/hash` for file-byte drift and
  `/config/loaded-hash` for loaded runtime config adoption.

## Desired End State

- A router-level signal a consumer can trust to mean "the running config is the
  desired config", so fleet convergence and auto-rollback do not depend on
  cross-checking a second endpoint from the agent. For example (Option B) a
  distinct hash computed over the successfully-loaded config, kept separate from
  the file-bytes hash the tuning pipeline uses; or (Option C) reverting the
  on-disk file to the last-good bytes on a parse failure so `/config/hash`
  returns to the good hash and the failed apply is observable as non-convergence.
- Or an explicit, reviewed decision that the agent-side `/config/router` gate is
  the accepted design, documented as such, with `/config/hash`'s file-bytes
  semantics called out so no future consumer is surprised.

## Exit Criteria

- Either the router exposes a loaded-config signal (Option B) or reverts the file
  on parse failure (Option C) such that the fleet agent's rollback no longer
  needs the extra `GET /config/router` gate to distinguish an unloadable config
  from an applied one - or that agent-side gate is explicitly ratified as the
  design and the `/config/hash` file-bytes semantics are documented for future
  consumers.
- The R8 auto-rollback path is re-validated under the chosen router-level
  behavior (2-box `verify-hardening` R8 `[PASS]`, CCP audit shows `rolled_back`
  for an invalid config), without relying on an undocumented coincidence of
  endpoint semantics.
