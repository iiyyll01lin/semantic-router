# TD049: Multi-Candidate Decision Did Not Engage the Selection Algorithm at Runtime (multi_factor Counter Stayed 0 Despite Two Candidates)

## Status

Open

## Owner Plan

PL0035 Strix PoC PII Hardening

## Release Relevance

None - PoC-surfaced routing-observability/selection gap

## Scope

The runtime model-selection path for a decision that declares more than one
candidate `modelRefs` plus an `algorithm` block: how per-decision candidates are
resolved into selection inputs, where `selectModelFromCandidates` decides to run
an algorithm versus short-circuit, and the lack of an authoritative runtime
signal (counter or response header) that the chosen algorithm actually executed.

## Summary

The 2-box PoC gave the `reasoning_deep` decision two candidates
(`google/gemini-3.1-pro` and `google/gemini-2.5-flash-lite`) and an
`algorithm: type: multi_factor` block, and `multi_factor` registers at startup
(`tier=supported, dependencies=none`). Routing still reaches `reasoning_deep`
and resolves to `google/gemini-3.1-pro` on Halo-B. However, runtime telemetry
does **not** confirm the `multi_factor` algorithm ran a candidate comparison:

- After 2-3 hard requests that matched `reasoning_deep`
  (`llm_decision_match_total{decision_name="reasoning_deep"}=2`), the selection
  counter `llm_model_selection_total{method="multi_factor"}` stayed **0** (only
  `_init` placeholder series existed for all methods).
- No `[ModelSelection] Selected ... (method=multifactor ...)` info log was
  emitted; the router log only showed the startup registration.
- The request resolved to the first candidate with an **empty** selection
  method - not `single`, and not `multi_factor`.

The most likely cause is an early return inside the candidate-selection path
(for example only one live candidate reaching selection time, which both skips
the algorithm and leaves the counter at 0). There is also no
selection-method response header (even with `x-vsr-debug: true`), so the demo
cannot positively show the method that was used; the only intended evidence
sources are the `decision_model_selected` DEBUG log and the
`llm_model_selection_*` metrics, and neither showed `multi_factor` executing.

## Evidence

- [src/semantic-router/pkg/extproc/req_filter_classification.go](../../../src/semantic-router/pkg/extproc/req_filter_classification.go) - lines ~87-90: single-candidate decisions short-circuit to `single`; the multi-candidate path is what must engage the algorithm.
- `selectModelFromCandidates` (model-selection path invoked from the classification filter) - the suspected early-return / short-circuit site to investigate; needs confirmation that both declared candidates are live selection inputs at selection time.
- [deploy/recipes/strix-halo-2box/poc-client-edge.yaml](../../../deploy/recipes/strix-halo-2box/poc-client-edge.yaml) - the `reasoning_deep` decision now declares two `modelRefs` plus `algorithm: type: multi_factor` (weights quality/latency/cost/load, latency_percentile 95, on_no_candidates cheapest).
- [docs/poc/07-client-server-topology.md](../../poc/07-client-server-topology.md) section 6.6.2 - the second-round run record documenting the 0-counter, empty-method observation as a KNOWN GAP.
- `.agent-harness/experiments/2box-topology/phase2/SUMMARY.md` and `headers-hard.txt` - the captured headers and metric snapshot behind the observation (local evidence, not committed).

## Why It Matters

- A core selling point of the router is multi-candidate, algorithm-driven model
  selection; the PoC currently cannot demonstrate that an algorithm executed,
  only that a multi-candidate config and registration exist.
- The empty selection method (neither `single` nor `multi_factor`) suggests the
  selection path takes an unobserved branch, which is a correctness/observability
  gap, not just a missing demo header.
- Without an authoritative runtime signal, future selection-algorithm work
  cannot be validated end-to-end from a deployment.

## Desired End State

- A decision that declares >=2 live candidates plus a supported `algorithm`
  provably runs that algorithm: `llm_model_selection_total{method=...}`
  increments and a `decision_model_selected` / `[ModelSelection] Selected`
  signal is emitted with the method.
- The selection method is observable from a deployment (for example a
  `x-vsr-selected-method` response header under `x-vsr-debug: true`), so the
  multi-candidate demo can be shown without raising router log level.
- The candidate-resolution path is understood and documented: how per-decision
  `modelRefs` become selection inputs and under what conditions selection
  short-circuits versus runs the algorithm.

## Exit Criteria

- For the `reasoning_deep` two-candidate + `multi_factor` config, a hard request
  increments `llm_model_selection_total{method="multi_factor",decision="reasoning_deep"}`
  above 0 and emits a selection-method signal.
- The reason the counter stayed 0 (early return / single live candidate / other)
  is root-caused in `selectModelFromCandidates` and either fixed or documented as
  intended behavior with the correct method label (not empty).
- The selection method is retrievable from a live deployment without changing the
  router log level.
