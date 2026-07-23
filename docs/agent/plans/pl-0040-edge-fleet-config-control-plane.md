# PL-0040 Edge Fleet Config Control Plane (Pull Mode, 2-Box Strix Halo)

## Goal

- Close the edge-gateway central-governance gap identified in
  [docs/poc/08-topology-promotion-and-governance.md](../../poc/08-topology-promotion-and-governance.md)
  section 5: a fleet of edge AIPC gateways (Design A) has no central, audited
  way to receive one rule change across all nodes.
- Deliver a **pull-mode** central control plane (CCP) plus a per-edge pull agent
  that distributes signed router config to a fleet of edge gateways, reusing the
  existing per-node config primitives (hot-reload, `PUT/PATCH /config/router`,
  `GET /config/hash`, `/config/router/versions`, `/config/router/rollback`).
- Make it **one-click deployable, verifiable, and demoable on two Strix Halo
  (gfx1151) boxes**, mirroring the existing
  [strix-halo-2box](../../../deploy/recipes/strix-halo-2box/README.md) recipe
  pattern.
- Scope is phases P1–P4 (MVP fan-out, audit + versioning, security hardening,
  pull-agent productionization). P5 (GitOps) is out of scope.

## Scope

In scope:

- A new runnable recipe `deploy/recipes/strix-halo-fleet-2box/` with a one-click
  orchestrator, verification, demo, and teardown, mirroring
  [strix-halo-2box](../../../deploy/recipes/strix-halo-2box/) conventions
  (run on Halo-A, SSH/scp-provision a bare Halo-B, ControlMaster single
  password, logs outside the repo tree).
- A **Central Control Plane (CCP)** service that stores a desired router config,
  versions it, signs it, serves it to agents, accepts admin updates, and keeps a
  central audit log.
- A **pull agent** co-located on each edge box that polls the CCP, verifies the
  signed bundle, detects drift via `GET /config/hash`, applies via
  `PUT /config/router`, and reports status back to the CCP.
- A **two-edge-gateway topology** for this PoC: BOTH Halo-A and Halo-B run a
  router/Envoy gateway plus an agent (this is a deliberate reconception versus
  the Design A gateway+backend split, because the governance demo requires a
  fleet of gateways, not a gateway plus a plain Ollama backend).
- Reuse of the existing per-node config API and fsnotify hot-reload; no changes
  to the router's reload engine.

Out of scope:

- Kubernetes, the operator, or in-cluster ConfigMap delivery (that path already
  works; see [docs/poc/06-multi-node-and-operator.md](../../poc/06-multi-node-and-operator.md)).
- P5 GitOps integration (desired config sourced from Git); may follow later.
- Real Instinct/MI350P performance or TCO claims; both boxes are gfx1151 APUs
  (continue the honest gfx1151-not-Instinct split of poc docs).
- Changing the router's config schema, hot-reload engine, or adding auth into
  the router's HTTP API itself beyond what the agent/CCP trust boundary needs.
- Multi-tenant RBAC beyond a single admin identity for the PoC.

## Architecture

```
                 Central Control Plane (CCP, new, on Halo-A)
                 - desired/ config store + versions
                 - sign(desired) -> bundle{version, sha256, signature, config}
                 - GET  /fleet/desired   (agents pull)
                 - POST /fleet/status    (agents report; central audit log)
                 - GET  /fleet/status    (demo convergence view)
                 - POST /fleet/desired   (admin "edit once"; validate+bump+sign)
                          ^   |
            outbound pull |   | outbound pull (NAT/firewall friendly)
                          |   v
   Halo-A edge gateway            Halo-B edge gateway
   router+Envoy+agent#1           router+Envoy+agent#2
   agent -> localhost:8080        agent -> localhost:8080
     GET  /config/hash  (drift)     GET  /config/hash
     PUT  /config/router (apply)    PUT  /config/router
   fsnotify hot-reload (no restart) fsnotify hot-reload (no restart)
```

Reconcile loop (each agent): `GET CCP /fleet/desired` -> verify signature ->
`GET localhost /config/hash` -> if `hash != bundle.sha256` then
`PUT localhost /config/router` (validate -> backup -> write -> hot-reload) ->
re-read hash to confirm -> `POST CCP /fleet/status`. Pull-only: the edge box
makes no inbound-listening commitment to the CCP.

## Exit Criteria

- One command on Halo-A stands up the whole pull-mode fleet across both Strix
  Halo boxes (CCP + 2 edge gateways + 2 agents), provisioning a bare Halo-B over
  SSH/scp, and exits only after both boxes have converged to the CCP's desired
  config hash.
- Editing one rule once at the CCP (`POST /fleet/desired`) converges BOTH boxes
  to the new config hash within a bounded window (target <= 1 poll interval +
  hot-reload settle), via fsnotify hot-reload with NO container restart.
- Drift is self-healing: manually mutating one box's config directly is detected
  on the next cycle (`GET /config/hash` mismatch) and reverted to desired.
- Fleet rollback works: setting CCP desired to a previous version converges both
  boxes back, leveraging per-node `/config/router/versions` and `/rollback`.
- Signed-bundle integrity holds: a tampered or unsigned desired bundle is
  rejected by the agent and never applied; the rejection is audited.
- The CCP keeps a central audit log capturing every apply (box id, timestamp,
  from-version, to-version, result), demonstrable in the demo.
- The agent is pull-only (only outbound calls to the CCP and to localhost), so a
  NAT'd/firewalled edge AIPC needs no inbound exposure.
- A verify script asserts all of the above headlessly (PASS/FAIL), and a demo
  script narrates the edit-once-converge-everywhere flow for a stakeholder.
- Honest-boundary note recorded: the router's own `/config/*` API has no
  built-in auth today, so the agent calls it on localhost only and the CCP<->agent
  channel carries the signature/token trust boundary.

## Task List

P1 - MVP pull fan-out:

- [x] `EFC001` Create `deploy/recipes/strix-halo-fleet-2box/` skeleton mirroring
  `strix-halo-2box` (README, logs-outside-repo, ControlMaster SSH, teardown).
- [x] `EFC002` Implement the CCP service (stdlib-first): `desired/` store with
  versioning, `GET /fleet/desired`, `POST /fleet/desired` (validate via
  `vllm-sr validate` or `PUT`-dry-run), `GET/POST /fleet/status`.
- [x] `EFC003` Implement the pull agent: poll `GET /fleet/desired`, compare to
  `GET localhost:8080/config/hash`, apply via `PUT localhost:8080/config/router`
  on drift, confirm hash, `POST /fleet/status`.
- [x] `EFC004` Stand up the two-edge-gateway topology: `gateway-bring-up.sh`
  serves a real `vllm-sr serve` ROCm gateway on BOTH boxes (`--platform amd`,
  `VLLM_SR_AMD_PRESERVE_CPU=1` so agent-triggered reloads do not crash), and the
  agent manages the gateway's bind-mounted source config (GET /config/hash +
  fsnotify reload). Wired into `FLEET_MODE=gateway`; on-hardware end-to-end run
  is the remaining verification.
- [x] `EFC005` Implement `deploy-fleet-2box.sh` one-click orchestrator (preflight,
  start CCP on Halo-A, gateway+agent on Halo-A, SSH/scp-provision gateway+agent
  on Halo-B, wait for both to converge, print PASS/FAIL + log paths + teardown).

P2 - Central audit + versioning:

- [x] `EFC006` CCP central audit log: append (box id, ts, from/to version,
  result) on every `POST /fleet/status`; expose `GET /fleet/status` convergence
  view for the demo.
- [x] `EFC007` Fleet rollback: setting CCP desired to a prior version converges
  both boxes back; agent uses per-node `/config/router/versions` +
  `POST /config/router/rollback` where a local backup already matches.
- [x] `EFC008` Drift self-heal test path: mutate one box directly, assert the
  next reconcile cycle reverts it to desired and audits the correction.

P3 - Security hardening:

- [x] `EFC009` Sign the desired bundle in the CCP (ed25519 or HMAC) and verify in
  the agent; reject and audit any tampered/unsigned bundle (never apply it).
- [x] `EFC010` Add a shared token (or mTLS) on the CCP<->agent channel; agent
  authenticates pulls and status posts; document that the router `/config/*` API
  is called on localhost only as the trust boundary.
- [x] `EFC011` Record the honest-boundary note in the recipe README and link the
  router-API-no-auth fact; open a tech-debt entry if the router API should gain
  native auth beyond this PoC.

P4 - Pull-agent productionization:

- [x] `EFC012` Make the agent configurable (CCP URL, poll interval, signing key,
  box identity, backoff/jitter) via env/flags; pull-only, no inbound listener.
- [ ] `EFC013` Package the agent as a small container/binary shippable to a bare
  edge box; ensure it survives router restarts and re-converges.
- [x] `EFC014` Implement `verify-fleet.sh` (headless PASS/FAIL: baseline
  converge, edit-once converge within window + no restart, drift self-heal,
  rollback, tamper-rejected, audit-recorded).
- [x] `EFC015` Implement `demo-fleet.sh` (narrated: edit one rule at CCP -> watch
  both Strix Halo gateways hot-reload live -> show audit -> show fleet rollback)
  and `teardown-fleet-2box.sh`.

## Next Action

- Run `deploy-fleet-2box.sh` (default `FLEET_MODE=mock`) on the two Strix Halo
  boxes to validate the cross-box SSH provisioning + convergence path on real
  hardware, then complete `EFC004` (real `vllm-sr serve` gateway on both boxes
  via `FLEET_MODE=gateway`); container-packaging the agent (`EFC013`) is optional.

## Validation Notes

- Control-plane logic verified offline by `verify_local.py` (7/7): baseline
  converge, edit-once converge via hot-reload (reload_count +1, start_time
  stable = no restart), drift self-heal, fleet rollback, signed-bundle tamper
  rejection, and central audit.
- Real process entrypoints + the `fleetctl` CLI verified by launching the CCP,
  two mock routers, and two agents as separate OS processes and converging them
  through `fleetctl wait-converged` across an edit-once, with the audit log
  recording every apply.
- Shell scripts reviewed; `verify-fleet.sh` checks are `set -e` safe (the
  convergence commands run inside an `if` condition).
- A portability fix landed during bring-up: the agent writes config bytes
  exactly (binary), so the on-disk `GET /config/hash` matches the bundle hash on
  every platform (text-mode newline translation would otherwise break
  convergence on Windows-authored configs).
- Pending on the Strix Halo hardware: the cross-box SSH provisioning path and
  `FLEET_MODE=gateway` against real `vllm-sr serve` routers (`EFC004`).
- Mock mode is now verified END-TO-END on the real two Strix Halo boxes: a single
  `deploy-fleet-2box.sh` brought up the CCP, SSH-provisioned Halo-B, converged
  both boxes, and `verify-fleet.sh` (edit-once, drift self-heal, fleet rollback,
  central audit) plus `demo-fleet.sh` all passed. Two runtime bugs were fixed in
  the process (an apostrophe in a `${VAR:?...}` message; a stale `check` helper
  call in verify-fleet.sh) — both because `bash -n` was initially sandbox-blocked.
- Gateway mode (`gateway-bring-up.sh`) is implemented: it reuses the proven
  strix-halo-poc setup and the agent's file-write path works unchanged because
  `GET /config/hash` hashes the bind-mounted SOURCE config the agent writes
  (confirmed in route_config_deploy.go). Its end-to-end ROCm run on both boxes is
  the remaining hardware verification.
- Gateway hardware run progress: Halo-A now comes up **fully** in gateway mode
  (tier models pulled, host API port proactively freed, `vllm-sr serve` ready,
  `GET /config/hash` live, agent attached) — the earlier `:8080 address already
  in use` failure is resolved (stop prior router before bring-up + remove a stale
  router container before serve). Halo-B provisioning was then fixed: gateway mode
  **ships** this recipe's scripts to Halo-B (like mock) and points them at the
  repo's strix-halo-poc via an overridable `STRIX_POC_DIR`, so Halo-B does not
  need this branch checked out (it failed before with `node-bring-up.sh: No such
  file or directory` because its checkout predated the fleet recipe). A fail-fast
  Halo-B preflight (`poc-strix.yaml` present and `vllm-sr` on the SSH `PATH`) was
  added. Full two-box gateway convergence is the remaining hardware check.- Real-gateway reload root-cause fix: the router bind-mounts the config as a
  SINGLE FILE (`<host>/config.yaml:/app/config.yaml:z`, `docker_start.py`) and
  watches it with fsnotify (`server_config_watch.go`). The agent originally wrote
  via an atomic temp-file rename, which swaps in a NEW inode the container never
  sees through a file mount — so the new config would be invisible and no reload
  would fire. `fleet_agent._write_config` now overwrites the file IN PLACE
  (truncate + write + fsync, same inode), which the container observes and which
  fires the `Write`-event hot-reload; it still writes exact bytes so the on-disk
  `GET /config/hash` matches the bundle hash (`verify_local.py` stays 7/7).

## Operating Rules

- Reuse, do not reinvent: the per-node hot-reload, validate/backup/write,
  versioning, rollback, and `GET /config/hash` drift signal already exist
  ([route_config_deploy.go](../../../src/semantic-router/pkg/apiserver/route_config_deploy.go),
  [route_router_config_update.go](../../../src/semantic-router/pkg/apiserver/route_router_config_update.go),
  [server_config_watch.go](../../../src/semantic-router/pkg/extproc/server_config_watch.go)).
  This plan only adds the central distribution + audit + signing layer.
- Keep the recipe SEPARATE from `strix-halo-2box` so that working Design A recipe
  is never disturbed.
- Keep it a TOPOLOGY/governance PoC: no Instinct/MI350P performance or TCO
  claims; both boxes are gfx1151 APUs.
- Stdlib-first for CCP and agent (match `smoke_test.py` stdlib-only style) unless
  a dependency is clearly justified; the agent must stay light enough to ship to
  a bare edge box.
- Pull-only at the edge: the agent makes outbound calls only (to the CCP and to
  localhost); never require inbound exposure on the edge AIPC.
- Honest boundaries: state explicitly that the router `/config/*` API has no
  native auth today and that this PoC's trust boundary is the signed CCP bundle
  plus localhost-only application.
- Behavior-visible deploy/recipe additions follow the repo gates
  (`make agent-validate`, `make agent-lint`, `make agent-ci-gate`); docs/recipe
  edits stay lightweight per the root AGENTS.md.

## Related Docs

- [docs/poc/08-topology-promotion-and-governance.md](../../poc/08-topology-promotion-and-governance.md) (section 5: the gap and pull-mode option B)
- [docs/poc/07-client-server-topology.md](../../poc/07-client-server-topology.md) (Design A edge-gateway, the 2-box pattern)
- [docs/poc/06-multi-node-and-operator.md](../../poc/06-multi-node-and-operator.md) (in-cluster central config, the operator path this PoC complements for bare edge)
- [deploy/recipes/strix-halo-2box/README.md](../../../deploy/recipes/strix-halo-2box/README.md) (recipe pattern to mirror)
- [Execution Plans README](README.md)
- Per-node config API: [routes.go](../../../src/semantic-router/pkg/apiserver/routes.go), [route_config_deploy.go](../../../src/semantic-router/pkg/apiserver/route_config_deploy.go), [route_router_config_update.go](../../../src/semantic-router/pkg/apiserver/route_router_config_update.go)
