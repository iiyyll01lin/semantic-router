# TD050: Global `model_selection.method` Is Silently Ignored Unless a Decision Carries a Per-Decision Algorithm Block

## Status

Mitigated - the `warnGlobalModelSelectionMethodIgnored` startup validator ships
in `src/semantic-router/pkg/config/validator_model_selection.go` and is wired
into `validator.go`, so the silent dead-config case now surfaces as a boot
warning (the Part 1 remediation). The underlying single-candidate short-circuit
and global-method-not-consulted routing behavior are unchanged, so this remains
open as a warning-only mitigation rather than a full routing fix.

## Owner Plan

PL0035 Strix PoC PII Hardening

## Release Relevance

None - PoC-surfaced routing/config footgun

## Scope

The per-decision model-selection path that maps a decision to a selection
method: how `getSelectionMethod` resolves the method, why the global
`global.router.model_selection.method` value never reaches that path, and the
single-candidate short-circuit that bypasses selection (and any session policy)
entirely.

## Summary

Setting `global.router.model_selection.method` (for example `session_aware`)
looks like it should switch the router's selection algorithm, but for
per-decision routing that global value is **silent dead config**: it is never
consulted when a request is routed. Two independent short-circuits cause this.

- A decision with a single `modelRefs` entry returns **before** any selection
  algorithm runs. `selectModelFromCandidates` returns the only candidate with
  method `"single"`, so a configured `session_aware` (or any) algorithm never
  executes, no `SessionPolicy` is attached, and the `x-vsr-session-phase`
  response header is left empty.
- Even with two or more candidates, `getSelectionMethod` resolves the method
  **only** from the per-decision `algorithm.Type` (via
  `selectionMethodByAlgorithmType`) and falls back to `MethodStatic`. It never
  reads `cfg.ModelSelection.Method`. So a global method with no matching
  per-decision `algorithm` block is silently ignored, and routing runs static
  selection instead.

The practical consequence is that operators who set only the global method see
no error, no warning (until the Part 1 startup warning lands), and a router that
quietly does static selection while appearing to be configured for something
else. To actually engage a non-static method such as `session_aware`, the
decision must declare overlapping (>=2) `modelRefs` **and** carry its own
`algorithm: {type: session_aware, session_aware: {...}}` block.

This was verified by experiment: after adding both the second candidate and the
per-decision `algorithm` block, the selection method became `session_aware`,
`x-vsr-session-phase` populated on 64/64 requests (`user_turn` / `tool_loop`),
and the session-policy violation counters went from 16/8 to 0. Honest caveat:
that run collapsed to a single served model, so the 0 violations alone do not
prove a lock prevented a real model switch.

## Evidence

- [src/semantic-router/pkg/extproc/req_filter_classification.go](../../../src/semantic-router/pkg/extproc/req_filter_classification.go) - lines ~86-90: `if len(selCtx.CandidateModels) == 1 { ... return defaultCandidateModelRef, "single" }` short-circuits before selection runs, so a single-candidate decision never engages the algorithm and never sets a session policy or `x-vsr-session-phase`.
- [src/semantic-router/pkg/extproc/req_filter_classification.go](../../../src/semantic-router/pkg/extproc/req_filter_classification.go) - lines ~448-455: `getSelectionMethod` maps only the per-decision `algorithm.Type` and returns `selection.MethodStatic` otherwise; it never reads `cfg.ModelSelection.Method`, so the global method is unreachable for per-decision routing.
- [src/semantic-router/pkg/extproc/req_filter_classification_runtime.go](../../../src/semantic-router/pkg/extproc/req_filter_classification_runtime.go) - lines ~16-28: `selectionMethodByAlgorithmType` is the only mapping consulted, keyed by the per-decision algorithm type (`session_aware`, `multi_factor`, `static`, etc.).
- [src/semantic-router/pkg/config/validator_model_selection.go](../../../src/semantic-router/pkg/config/validator_model_selection.go) - the Part 1 remediation: `globalModelSelectionMethodIgnored` predicate plus `warnGlobalModelSelectionMethodIgnored` startup warning that surfaces this dead-config case without blocking boot.
- [deploy/recipes/strix-halo-2box/experiments/session-aware-multicandidate.yaml](../../../deploy/recipes/strix-halo-2box/experiments/session-aware-multicandidate.yaml) - the experiment config (owned by a parallel effort) that adds overlapping `modelRefs` plus a per-decision `session_aware` algorithm block to actually engage the method.
- [docs/poc/07-client-server-topology.md](../../poc/07-client-server-topology.md) section 6.6.6 - the run record documenting the root cause and how to engage and verify `session_aware`; section 6.6.2 is the original KNOWN GAP this entry root-causes.
- [TD049 Multi-Candidate Decision Did Not Engage the Selection Algorithm at Runtime](td-049-multi-candidate-selection-not-engaged.md) - the observed symptom (selection counter stayed 0, empty method) that this dead-config / short-circuit behavior explains.

## Why It Matters

- The global `model_selection.method` knob is a natural place for an operator to
  configure routing behavior, but setting it alone has no effect on per-decision
  routing and produces no error - a silent footgun that makes the router look
  misconfigured-but-working.
- Selection-algorithm features (session locks, multi-factor scoring) cannot be
  demonstrated or trusted from a deployment when the path that would run them is
  silently bypassed, which directly produced the unproven-selection gap in
  TD049.
- The single-candidate short-circuit also means session-aware behavior
  (`SessionPolicy`, `x-vsr-session-phase`) is impossible for any decision with
  one candidate, even when the global method is set, which is surprising for a
  feature advertised as session-aware.

## Desired End State

- Configuring a non-static selection method is not silently ignored: either the
  global `model_selection.method` participates in routing as a documented
  fallback, or the operator is clearly told (startup warning) that a per-decision
  `algorithm` block is required for the method to take effect.
- The conditions under which selection short-circuits (single candidate) versus
  runs the configured algorithm are documented, so engaging `session_aware` (or
  any method) is a predictable, discoverable configuration rather than a
  reverse-engineered one.

## Exit Criteria

- A config that sets only `global.router.model_selection.method` to a non-static
  method, with no per-decision `algorithm` block, either engages that method at
  runtime or emits the `warnGlobalModelSelectionMethodIgnored` startup warning so
  the dead config is visible (the Part 1 fix is wired into config validation and
  fires on boot).
- The single-candidate short-circuit and the per-decision-precedence behavior of
  `getSelectionMethod` are documented for operators (in the PoC run record and/or
  the selection config docs), including the requirement that `session_aware`
  needs >=2 candidates plus a per-decision algorithm block.
- An optional reviewed fallback (global method consulted when no per-decision
  algorithm is present) is either implemented or explicitly recorded as declined
  in favor of the warning-only approach.
