# Preserved perf-run reports

These are point-in-time report artifacts from the Strix Halo fleet perf harness,
copied verbatim out of the ephemeral `/tmp/vllm-sr-fleet/` run bundles so they
survive reboot/cleanup. They are the raw source behind the narrative in
[`../perf-report.md`](../perf-report.md); numbers here are reproducible via
`perf/collect-report-data.sh` (see perf-report.md §13).

| File | Source run bundle | Box(es) | Run date (local) |
| --- | --- | --- | --- |
| [`agentic-prefill-campaign-20260722.md`](agentic-prefill-campaign-20260722.md) | complete agentic-prefill/capacity campaign ledger | Halo-A + `demo-002` | 2026-07-22/23 |
| [`agentic-context-customer-onepager-20260722.md`](agentic-context-customer-onepager-20260722.md) | focused agentic-context customer brief | `demo-002` | 2026-07-22/23 |
| [`agentic-context-customer-20260722-four-proof-status.json`](agentic-context-customer-20260722-four-proof-status.json) | normalized machine-readable four-proof status | `demo-002` | 2026-07-23 |
| [`agentic-context-customer-20260722-evidence-index.json`](agentic-context-customer-20260722-evidence-index.json) | normalized machine-readable evidence index | `demo-002` | 2026-07-23 |
| [`customer-report.md`](customer-report.md) | hand-authored fleet brief (synthesizes `report-run-20260712-123240` + fleet + 96 GiB re-test) | Halo-A + Halo-B | 2026-07-13 (updated) |
| [`customer-onepager.md`](customer-onepager.md) | hand-authored executive one-pager (companion to `customer-report.md`) | Halo-A + Halo-B | 2026-07-13 |
| [`report-data.md`](report-data.md) | `report-run-20260712-123240` | Halo-A (`aup-HP-Z2-Mini-G1a`) | 2026-07-12 12:32 |
| [`perf-summary-2box.md`](perf-summary-2box.md) | `report-run-2box-20260712-153904` | Halo-A + Halo-B (fleet) | 2026-07-12 15:39 |

**Provenance notes.**

- `agentic-prefill-campaign-20260722.md` is a **hand-authored completeness ledger**, not a raw bundle copy. It maps every qualification, acceptance, partial, blocked, superseded, and deferred run family to its external evidence path and keeps HTTP/transport separate from marker/agentic correctness.
- The agentic-context one-pager and two JSON companions are the focused
  2026-07-22/23 `demo-002` addendum. They distinguish 65,536 configured/loaded
  context from the 65,152 maximum tested input, and distinguish 17 cells from
  174 requests and 150 marker passes. The technical narrative is
  [`../perf-report.md` §9](../perf-report.md).
- `report-data.md` is the single-box (Halo-A) run `report-run-20260712-123240`:
  co-location overhead, inference-server comparison, concurrency sweep, and the
  semantic-cache threshold sweep — copied byte-for-byte from the run bundle.
- `customer-report.md` and `customer-onepager.md` are **hand-authored** customer briefs
  (not raw bundle copies): they synthesize the fleet numbers from `report-data.md` +
  `perf-summary-2box.md` + the technical [`../perf-report.md`](../perf-report.md), and were
  updated for the Halo-B **96 GiB carveout re-test** (2026-07-13). Their fleet
  baseline remains date-scoped; a dated `demo-002` agentic addendum links to the
  focused brief rather than rewriting historical Halo-A/B measurements. Keep
  both scopes in sync with `../perf-report.md`.
- `perf-summary-2box.md` is the two-box fleet run `report-run-2box-20260712-153904`
  (Halo-A + Halo-B), copied byte-for-byte; only `perf-summary.md` from the two-box bundle
  was renamed to `perf-summary-2box.md` to avoid a clash with the summary in `report-data.md`.

**Consistency gate.** From the `strix-halo-fleet-2box` recipe root, run:

```bash
python3 perf/validate_agentic_context_reports.py \
  --selected-summary ~/vllm-sr-evidence/agentic-context-customer-20260722/analysis/final-selected-scope-summary.json \
  --capacity-summary-dir ~/vllm-sr-evidence/agentic-context-customer-20260722/capacity-direct-openai/summary \
  --milestone-mirror-root ~/vllm-sr-evidence/demo-002-capacity-matrix \
  --prefill-evidence-root ~/vllm-sr-evidence/agentic-prefill-20260722
python3 perf/test_validate_agentic_context_reports.py -q
```

Without the optional external paths, the gate still checks the tracked report
set. With them, it also re-derives request, marker, and usage totals from the
preserved source summaries, including the 8-cell/24-request milestone and the
six-checkpoint/20-request llama.cpp output-256 run. It rejects context-window,
cell/request/marker, proof-status, cache-attribution, stale-vLLM, evidence-path,
and evidence-generation drift.
