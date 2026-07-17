# TD051: Router `/config/hash` Reflects Config File Bytes, Not the Loaded Config

## Status

Open

## Owner Plan

PL0036 Edge Fleet Config Control Plane

## Release Relevance

None - edge-fleet control-plane convergence/rollback correctness (an agent-side
gate has landed as a workaround; the router-level signal is still misleading)

## Scope

The router config-reload observability surface the edge fleet agent polls to
decide convergence and auto-rollback: `GET /config/hash` (the drift/convergence
signal), the fsnotify reload-failure path (what the router does when a hot-reload
cannot parse the new config), and `GET /config/router` (the only endpoint that
reflects whether the active config actually parsed). Out of scope: the fleet
agent's own apply/rollback loop, which now compensates for this gap.

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

## Evidence

- [src/semantic-router/pkg/apiserver/route_config_deploy.go](../../../src/semantic-router/pkg/apiserver/route_config_deploy.go) - `handleConfigHash` (lines ~260-276): `data, err := os.ReadFile(paths.sourcePath)` then `hash := sha256.Sum256(data)` - the reported hash is over the on-disk file bytes, independent of whether the router could parse/load them.
- [src/semantic-router/pkg/extproc/server_config_watch.go](../../../src/semantic-router/pkg/extproc/server_config_watch.go) - `reload()` (lines ~169-180) calls `reloadRouterFromFile`; on error it calls `logReloadFailure` (lines ~199-208, which emits the `config_reload_failed` event) and returns. It does not revert the on-disk file and does not stop serving, so the router keeps the last-good in-memory config while the bad bytes stay on disk (and thus in `/config/hash`).
- [src/semantic-router/pkg/apiserver/route_config_deploy.go](../../../src/semantic-router/pkg/apiserver/route_config_deploy.go) - `handleConfigGet` (lines ~204-224): parses the active config with `yaml.Unmarshal` and returns HTTP 500 `PARSE_ERROR` on invalid YAML. This is the only endpoint that reflects load success, and the signal the fleet agent's Option A gate relies on.
- [deploy/recipes/strix-halo-fleet-2box/fleet_agent.py](../../../deploy/recipes/strix-halo-fleet-2box/fleet_agent.py) - `_router_health` (lines ~191-224): the landed agent-side workaround now requires `GET /config/router == 200` after hash convergence, so an unloadable config is detected and rolled back (`config/router unloadable`) instead of being recorded as `applied`.

## Why It Matters

- `/config/hash` is named and used as a "did the reload complete" signal, but it
  only proves the file was written, not that the router loaded it. Any tooling
  (fleet CCP agents, tuning pipelines) that trusts it can mistake an unadopted
  config for a live one.
- The edge fleet's auto-rollback (R8) could not fire against the real gateway
  until the agent added a second check; without that workaround an invalid
  config would be reported fleet-wide as `applied` even though every box kept
  serving the previous config.
- Correctness now depends on `/config/hash`, `/config/router`, and an agent-side
  gate agreeing across two subsystems, instead of one truthful convergence
  signal - fragile if either endpoint's semantics change.

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
