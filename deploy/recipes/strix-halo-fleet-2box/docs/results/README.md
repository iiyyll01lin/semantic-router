# Preserved perf-run reports

These are point-in-time report artifacts from the Strix Halo fleet perf harness,
copied verbatim out of the ephemeral `/tmp/vllm-sr-fleet/` run bundles so they
survive reboot/cleanup. They are the raw source behind the narrative in
[`../perf-report.md`](../perf-report.md); numbers here are reproducible via
`perf/collect-report-data.sh` (see perf-report.md §13).

| File | Source run bundle | Box(es) | Run date (local) |
| --- | --- | --- | --- |
| [`agentic-prefill-campaign-20260722.md`](agentic-prefill-campaign-20260722.md) | complete agentic-prefill/capacity campaign ledger | Halo-A + `demo-002` | 2026-07-22/23 |
| [`customer-report.md`](customer-report.md) | hand-authored fleet brief (synthesizes `report-run-20260712-123240` + fleet + 96 GiB re-test) | Halo-A + Halo-B | 2026-07-13 (updated) |
| [`customer-onepager.md`](customer-onepager.md) | hand-authored executive one-pager (companion to `customer-report.md`) | Halo-A + Halo-B | 2026-07-13 |
| [`report-data.md`](report-data.md) | `report-run-20260712-123240` | Halo-A (`aup-HP-Z2-Mini-G1a`) | 2026-07-12 12:32 |
| [`perf-summary-2box.md`](perf-summary-2box.md) | `report-run-2box-20260712-153904` | Halo-A + Halo-B (fleet) | 2026-07-12 15:39 |

**Provenance notes.**

- `agentic-prefill-campaign-20260722.md` is a **hand-authored completeness ledger**, not a raw bundle copy. It maps every qualification, acceptance, partial, blocked, superseded, and deferred run family to its external evidence path and keeps HTTP/transport separate from marker/agentic correctness.
- `report-data.md` is the single-box (Halo-A) run `report-run-20260712-123240`:
  co-location overhead, inference-server comparison, concurrency sweep, and the
  semantic-cache threshold sweep — copied byte-for-byte from the run bundle.
- `customer-report.md` and `customer-onepager.md` are **hand-authored** customer briefs
  (not raw bundle copies): they synthesize the fleet numbers from `report-data.md` +
  `perf-summary-2box.md` + the technical [`../perf-report.md`](../perf-report.md), and were
  updated for the Halo-B **96 GiB carveout re-test** (2026-07-13). Keep them in sync with
  `../perf-report.md`.
- `perf-summary-2box.md` is the two-box fleet run `report-run-2box-20260712-153904`
  (Halo-A + Halo-B), copied byte-for-byte; only `perf-summary.md` from the two-box bundle
  was renamed to `perf-summary-2box.md` to avoid a clash with the summary in `report-data.md`.

## Later additions

- agentic-context-customer-onepager-20260722.md is the one-page customer brief for the 2026-07-23 finalization (demo-002 Ollama direct 64K customer run). Machine-readable companions in this folder: agentic-context-customer-20260722-four-proof-status.json and agentic-context-customer-20260722-evidence-index.json. The same finalization is folded into agentic-prefill-campaign-20260722.md. Raw evidence stays out of git at demo-002 and Halo-A ~/vllm-sr-evidence/agentic-context-customer-20260722 (151-file checksum manifest).
