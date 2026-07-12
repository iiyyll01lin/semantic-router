# Preserved perf-run reports

These are point-in-time report artifacts from the Strix Halo fleet perf harness,
copied verbatim out of the ephemeral `/tmp/vllm-sr-fleet/` run bundles so they
survive reboot/cleanup. They are the raw source behind the narrative in
[`../perf-report.md`](../perf-report.md); numbers here are reproducible via
`perf/collect-report-data.sh` (see perf-report.md §13).

| File | Source run bundle | Box(es) | Run date (local) |
| --- | --- | --- | --- |
| [`customer-report.md`](customer-report.md) | `report-run-20260712-123240` | Halo-A (`aup-HP-Z2-Mini-G1a`) | 2026-07-12 12:32 |
| [`report-data.md`](report-data.md) | `report-run-20260712-123240` | Halo-A (`aup-HP-Z2-Mini-G1a`) | 2026-07-12 12:32 |
| [`perf-summary-2box.md`](perf-summary-2box.md) | `report-run-2box-20260712-153904` | Halo-A + Halo-B (fleet) | 2026-07-12 15:39 |

**Provenance notes.**

- `customer-report.md` / `report-data.md` are the single-box (Halo-A) run
  `report-run-20260712-123240`: co-location overhead, inference-server comparison,
  concurrency sweep, and the semantic-cache threshold sweep.
- `perf-summary-2box.md` is the two-box fleet run `report-run-2box-20260712-153904`
  (Halo-A + Halo-B), i.e. the symmetric Test 1 / Test 2 across both boxes.
- Copied unmodified (byte-for-byte) from the run bundles; only `perf-summary.md`
  from the two-box bundle was renamed to `perf-summary-2box.md` to avoid a name
  clash with the single-box summary embedded in `report-data.md`.
