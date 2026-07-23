# TD048: No First-Class Router-Replay to Fleet-Sim Exporter, and Fleet-Sim GPU Pools Are Hardcoded NVIDIA So Instinct TCO Is Uncalibrated

## Status

Open

## Owner Plan

PL0035 Strix PoC PII Hardening

## Release Relevance

None - PoC-surfaced TCO-pipeline and tooling debt

## Scope

The path from the router's recorded replay store to a fleet-sim
capacity/TCO simulation: the replay read API
(`GET :8899/v1/router_replay`), the absence of a first-class exporter that
reshapes those records into fleet-sim's trace format, and the hardcoded NVIDIA
GPU pool/profile assumptions inside fleet-sim that make any Instinct/MI350P TCO
number uncalibrated.

## Summary

There is no first-class, supported exporter that turns the router's replay
records into fleet-sim's `semantic_router` JSONL. Today the only options are the
curl+jq recipe in [03-strix-halo-runbook.md](../../poc/03-strix-halo-runbook.md)
section 9 and the new recipe-local
[export-replay-trace.sh](../../../deploy/recipes/strix-halo-2box/export-replay-trace.sh)
script, both of which hand-reshape the records (rename `completion_tokens` ->
`generated_tokens`, RFC3339 -> epoch seconds, drop null-token rows) outside any
versioned product surface.

Even once a trace is exported, fleet-sim's GPU pools are hardcoded NVIDIA
(`a100`/`a10g`), so the `$/yr` and node/GPU counts it produces are a pipeline
demonstration with default profiles, **not** an Instinct/MI350P-calibrated TCO.
The 2-box PoC second-round run recorded 396 replayed rows -> 28 GPUs (20x
A100-80GB + 8x A10G), ~$458K/yr, P99 8.1 ms, SLO 100% explicitly under this
caveat. This ties directly to the "measure-then-simulate" honest boundary in
[02-poc-plan.md](../../poc/02-poc-plan.md) section 12 (Boundary B): cross-box
aggregate throughput and fleet cost are extrapolated, never measured on real
Instinct.

## Evidence

- [deploy/recipes/strix-halo-2box/export-replay-trace.sh](../../../deploy/recipes/strix-halo-2box/export-replay-trace.sh) - the recipe-local exporter that is the only scripted bridge; it lives in a deploy recipe, not a product/tooling surface, and it prints the hardcoded-NVIDIA caveat itself.
- [src/fleet-sim/fleet_sim/workload/trace.py](../../../src/fleet-sim/fleet_sim/workload/trace.py) - lines ~55-62: the `semantic_router` loader requires `timestamp` (epoch), `prompt_tokens`, `generated_tokens`, `selected_model`, which is the shape the exporter has to hand-produce.
- [src/fleet-sim/run_sim.py](../../../src/fleet-sim/run_sim.py) and `src/fleet-sim/examples/semantic_router_trace_replay.py` - the GPU pools/profiles are hardcoded NVIDIA (`a100`/`a10g`), so no Instinct profile is available to calibrate against.
- [docs/poc/03-strix-halo-runbook.md](../../poc/03-strix-halo-runbook.md) section 9 - the pre-existing curl+jq recipe that the script formalizes but does not replace as a first-class surface.
- [docs/poc/07-client-server-topology.md](../../poc/07-client-server-topology.md) section 6.6.3 - the second-round run record reporting the pipeline-demo numbers under the explicit uncalibrated caveat.
- [docs/poc/02-poc-plan.md](../../poc/02-poc-plan.md) section 12 (Boundary B) - the measure-then-simulate honest boundary this debt is anchored to.

## Why It Matters

- Anyone reproducing the TCO closer must rediscover the reshape contract
  (token-field rename, timestamp conversion, null filtering) from a deploy
  recipe or a runbook snippet, because no versioned exporter owns it.
- The headline `$/yr`/GPU-count numbers are easy to mistake for an
  Instinct/MI350P result; without a calibrated profile they are only a default
  NVIDIA-profile pipeline demonstration, which undercuts the AMD-aligned story.
- The replay store already holds the real per-request routing decisions, so the
  missing piece is a supported export + a calibrated GPU profile, not new data.

## Desired End State

- A first-class, versioned exporter (in a product or tooling surface, not a
  deploy recipe) turns router-replay records into fleet-sim's trace format with
  a documented, tested contract.
- Fleet-sim ships at least one Instinct/MI350P GPU profile so the simulation can
  produce an Instinct-calibrated TCO instead of a default-NVIDIA pipeline demo.
- The 2-box recipe consumes the first-class exporter instead of carrying its own
  reshape script.

## Exit Criteria

- A supported exporter produces a fleet-sim-loadable trace from router-replay
  with no hand-written jq reshape, covered by a test.
- Fleet-sim can be pointed at an Instinct/MI350P profile, and the run record's
  TCO numbers can drop the "default-NVIDIA-profile, not Instinct-calibrated"
  caveat for that profile.
- The recipe-local `export-replay-trace.sh` can be removed or reduced to a thin
  wrapper over the first-class exporter.
