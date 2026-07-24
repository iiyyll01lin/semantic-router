# Agentic prefill and capacity campaign — 2026-07-22/23

This is the completeness ledger for the Strix Halo agentic-prefill campaign that
ran across Halo-A (`aup-HP-Z2-Mini-G1a`) and `demo-002`. It complements the
narrative in [`../perf-report.md`](../perf-report.md); it does not copy large raw
prompts or responses into git.

## How to read this ledger

- **Acceptance** means the planned family completed and has a canonical summary.
- **Qualification** means a pilot/preflight proved one part of the harness; it is
  not added to acceptance totals.
- **Partial** means valid checkpoints are preserved but the family did not reach
  its full acceptance boundary.
- **Blocked/deferred** means no acceptance result is claimed.
- The older `agentic-prefill-cell/v1` schema used `status=success` for completed
  transport even when the response marker was wrong. Therefore this report keeps
  **HTTP/transport** and **marker/agentic correctness** as separate columns.
- Repeated append-only checkpoint rows are de-duplicated by `cell_id`, keeping the
  latest record. The llama.cpp coverage file has one such duplicate row.
- In `agentic-prefill-matrix/v2` JSONL, each top-level row is a cell checkpoint,
  not a request. Request totals are reconstructed from the nested `cold_requests`
  and `warm_requests` arrays; the six llama.cpp output-256 checkpoints contain
  20 executed requests.

## Campaign result ledger

| Host | Run family | Role | Planned / recorded result | Transport / correctness result | Evidence |
| --- | --- | --- | --- | --- | --- |
| `demo-002` | Runtime context proof | Qualification | Configured proof passed; two loaded proofs passed at context 65,536 | Model loaded fully on GPU; allocation proof only | `.agent-harness/experiments/runtime-context-proof/demo-002/20260722T005250Z-configured.json`, `...005306Z-loaded.json`, `...005400Z-loaded.json` |
| `demo-002` | Direct canary | Superseded qualification | 1/1 cell recorded, failed overall | Calibration and HTTP worked; this early canary was superseded by the milestone/final capacity runs | `/home/aup/vllm-sr-evidence/demo-002-capacity-matrix/20260722T005852Z/proof/direct-canary-summary.json` |
| `demo-002` | Early direct spine | Superseded partial | 3 cells: 1 success, 2 failed | Exploratory 2K/8K/16K checkpoints; superseded | `/home/aup/vllm-sr-evidence/demo-002-capacity-matrix/20260722T005852Z/proof/direct-profile/raw/direct-spine.jsonl` |
| `demo-002` | Ollama milestone profile | Acceptance | 8/8: five spine contexts (2K, 8K, 16K, 32K, 65,152), one 32K/reuse90, two 8K concurrency cells | All 8 succeeded; 24/24 executed requests returned HTTP success and preserved markers | `/home/aup/vllm-sr-evidence/demo-002-capacity-matrix/20260722T023157Z-milestones/proof/spine/summary/direct-spine.json`, `.../proof/load/summary/direct-reuse.json`, `.../direct-concurrency.json` |
| `demo-002` | Router path for milestone | Blocked | Not run | No `vllm-sr` CLI, router image/container, or listeners on 8899/8080 during read-only preflight | `/home/aup/vllm-sr-evidence/agentic-context-customer-20260722/blockers/pre-hardware-blockers.json` |
| Halo-A | vLLM primary pilots | Qualification | 8/8 pilot cells completed | 16/16 HTTP; 16/16 markers | `/home/aup/vllm-sr-evidence/agentic-prefill-20260722/vllm-primary-pilot*.summary.json` |
| Halo-A | vLLM primary coverage, output 64 | Acceptance (v1 transport semantics) | 60/60 cells completed: contexts 512/2K/8K/16K/32K × reuse 0/50/90 × c1/2/4/8 | 450/450 HTTP; 180/450 markers (24 cells had full marker accuracy, 36 had zero) | `.../vllm-primary-coverage-out64.summary.json` |
| Halo-A | vLLM representative output 256 | Acceptance (v1 transport semantics) | 6/6 cells completed across reuse0/c1 and reuse90/c8 at 2K/16K/32K | 54/54 HTTP; 18/54 markers | `.../vllm-primary-out256-*.summary.json` |
| Halo-A | vLLM batching/APC ablation | Acceptance (v1 transport semantics) | 25/25 dedicated cells completed across batch 2048/8192/16384 and APC on/off; the batch8192/APC-on baseline is the primary coverage run | 260/260 HTTP; 80/260 markers | `.../vllm-b*-*.summary.json` |
| Halo-A | vLLM native tools | Acceptance | 22 tasks / 25 tools | 22/22 valid JSON and tool names; 21/22 correct arguments/steps; 0 transport failures | `.../vllm-native-tools.json` |
| Halo-A | vLLM real-agent smoke | Acceptance | 4 tasks, 16 requests | 16/16 HTTP; 1/4 exact task passes; no tool execution, portability, or tool-loop switch violations | `.../vllm-real-agent-smoke/summary.json` |
| Halo-A | llama.cpp pilot | Qualification | 1/1 cell succeeded | 2/2 HTTP; 2/2 markers | `.../llamacpp-pilot.summary.json` |
| Halo-A | llama.cpp Q4_K_M coverage, output 64 | Partial acceptance | 60 unique cells: 51 transport-success, 2 bounded-timeout failures, 7 explicit skips; one duplicate raw checkpoint row is excluded | 374/374 executed requests returned HTTP success; 41/374 markers | `.../llamacpp-coverage-out64.summary.json` and append-only JSONL |
| Halo-A | llama.cpp representative output 256 | Partial | 6 unique checkpoints: 4 failed, 2 delegated/skipped; one 16K/c8 cell timed out | 20/20 requests nested in those checkpoint records returned HTTP success; 0/20 markers | `/home/aup/vllm-sr-evidence/agentic-prefill-20260722/llamacpp-out256-r0-c1.jsonl`, `.../llamacpp-out256-r90-c8.jsonl` |
| Halo-A | llama.cpp native tools | Acceptance | 22 tasks / 25 tools | 22/22 valid JSON; 2/22 correct names/arguments/steps; 0 transport failures | `.../llamacpp-native-tools.json` |
| Halo-A | llama.cpp real-agent smoke | Acceptance | 4 tasks, 16 requests | 16/16 HTTP; 3/4 exact task passes; no tool execution, portability, or tool-loop switch violations | `.../llamacpp-real-agent-smoke/summary.json` |
| Halo-A | Ollama pilot | Qualification | 1/1 cell succeeded | 2/2 HTTP; 2/2 markers | `.../ollama-pilot.summary.json` |
| Halo-A | Ollama Qwen2.5-7B Q4 coverage, output 64 | Partial | 60 cells: 12 success, 31 correctness-failed, 17 skipped when work moved to `demo-002` | 314/314 executed requests returned HTTP success; 90/314 markers | `.../ollama-coverage-out64.summary.json` |
| `demo-002` | Pinned vLLM v0.25.1 long-context matrix | Acceptance | 48/48 cells checkpointed: batch 2048/8192/16384 × APC on/off × 16K/32K × reuse0/90 × c4/8 | 435/448 HTTP (97.1%); 0 correct markers; 8 bounded-timeout cells; no OOM/restart | `/home/aup/vllm-sr-evidence/agentic-prefill-20260722/demo-002/vllm/vllm-out64-ablation-summary.json` |
| `demo-002` | Pinned vLLM output 256 | Acceptance result (failed quality gate) | 4/4 cells checkpointed | 40/48 HTTP; 0 markers | `.../raw/vllm-b8192-apc-on-large-out256.summary.json` |
| `demo-002` | Pinned vLLM native tools | Acceptance | 22 tasks / 25 tools | 22/22 valid JSON and names; 21/22 correct arguments/steps | `.../raw/vllm-native-tools.json` |
| `demo-002` | Pinned vLLM real-agent smoke | Acceptance | 4 tasks, 16 requests | 16/16 HTTP; 1/4 exact task passes | `.../raw/vllm-real-agent-smoke/summary.json` |
| `demo-002` | Final direct Ollama Gemma Q8 capacity | Acceptance transport / partial correctness | 17 cells: 7 fully green, 10 marker-only failed | 174 measured requests: 174/174 HTTP and exact backend prompt usage; 150/174 markers; 65,152-token c1/c2/c4 passed transport/usage and c2/c4 passed every gate | `/home/aup/vllm-sr-evidence/agentic-context-customer-20260722/analysis/final-selected-scope-summary.json` |
| `demo-002` | Replay preflight while vLLM owned GPU | Blocked | No replay request sent | GPU ownership fail-closed; services remained healthy | `/home/aup/vllm-sr-evidence/demo-002-capacity-matrix/20260722T023157Z-milestones/agentic-replay/20260722T063300Z-remote-subphase-3b/limitations.json` |
| `demo-002` | Replay v1, three repetitions | Measured failure | 3/3 repetitions ended rapidly; no acceptance pass | All 256/1024/4096 payload calibrations returned HTTP 400 with missing authoritative usage; runtime stability gates passed | `.../replay-direct-rep{1,2,3}/summary/agentic-replay-profile.json` |
| `demo-002` | Replay v2 repetition 1 | Partial, user-scope abort | Fixed replay passed (28 regular + 4 checkpoint/padding = 32 total tool turns); branch passed; quality rows 0 | Stopped to enforce the explicit user scope decision; 106 resource samples preserved; repetitions 2/3 not run | `.../replay-direct-v2-rep1/`, `.../limitations/replay-v2-aborted-user-scope-20260723.json` |
| `demo-002` | Replay v3 repetition 1 | Partial, user-scope abort | Fixed and branch replay passed (32 total tool turns); quality rows 0 | Stopped to enforce the explicit user scope decision; 69 resource samples preserved; repetitions 2/3 not run | `.../replay-direct-v3-rep1/`, `.../limitations/replay-v3-aborted-user-scope-20260723.json` |
| `demo-002` | New same-host llama.cpp comparison | Deferred | Not run | Existing Halo-A llama.cpp evidence is retained; no `demo-002` parity claim | User scope decision in `.../limitations/user-scope-reduction-20260723.json` |

## Canonical conclusions

1. **Pinned vLLM works on gfx1151 for the measured stack**, but transport support
   is not agentic quality and is not blanket support for arbitrary images/models.
2. **Long-context instruction adherence is the dominant failure mode.** HTTP
   success remains high even where marker/quality gates fail.
3. **llama.cpp is not an unqualified winner for this campaign.** Its existing
   low-TTFT server results remain valid, but this Qwen2.5-7B agentic campaign
   records only 2/22 correct native-tool steps and 3/4 real-agent smoke tasks.
4. **Ollama's configured and loaded-verified 65,536-token allocation executed
  the final selected workload.** The maximum tested input was 65,152; adding
  256 output and 128 reserved headroom gives 65,536 total. Across 17 cells and
  174 requests, transport and backend-reported prompt usage passed completely;
  150 markers passed, 7 cells passed every gate, and the ten failed cells missed
  only the response-marker gate for this probe.
5. **No full long-horizon replay acceptance exists.** The v1 repetitions failed
  calibration; v2/v3 proved fixed+branch semantics only before being stopped to
  enforce the explicit user scope decision. Quality rows remained zero and
  repetitions 2/3 were not run. Missing login linger remains a future unattended-
  rerun durability risk, not the recorded scope-abort reason.

## Evidence integrity

- Halo-A campaign root:
  `/home/aup/vllm-sr-evidence/agentic-prefill-20260722`
- `demo-002` customer/replay root:
  `/home/demo-002/vllm-sr-evidence/agentic-context-customer-20260722`
- Controller mirror of the `demo-002` milestone/capacity evidence:
  `/home/aup/vllm-sr-evidence/demo-002-capacity-matrix`
- The 128-file milestone/capacity mirror was verified byte-for-byte against the
  source before scratch cleanup. With paths sorted under `LC_ALL=C`, the
  per-file SHA-256 manifest hash is
  `8241bfba5ba85516fe0ab7d507b409b42c4080121bffb03128dfb5c4a6c7b6de`.
- The controller prefill manifest has **217 entries**; the manifest file itself
  hashes to `d9dd7ecf6ebd1e72bdcacf1bca4f6f6a4690a12307d9182729da06338ee7ebc6`.
- The earlier `demo-002/archive-checksums.sha256` is an **interim 41-entry
  manifest**, not the final tarball; that manifest file hashes to
  `57268733bc1734228aaad832124b391fca09547b5f5593191a349f84e0b084fc`.
- The final customer manifest has **151 entries** and verified **151/151 OK**;
  its manifest-file hash is
  `bffa040234ed81af022a022bcad3a4a6cc7d3bc0ba7521d82b53b0ef92d5c019`.
- Immutable v1 tarball hashes are customer
  `d86f9cf206b83a908a2d1eaf11e2047747cbaf89401e2033220979e7d5138a7c`
  and prefill
  `68e9811ca088a93c0683804f40b7c5d529967ea828277901fa3f618126d45b73`.
- The controller-only `demo-002-capacity-matrix` mirror (plus its host-prep,
  clone-uncommitted, and provenance siblings) is now captured in a **deterministic
  immutable backup**, `demo-002-evidence-backup-20260723.tar.gz` (**185 files**),
  built with `tar --sort=name --owner=0 --group=0 --numeric-owner
  --mtime=2026-07-23T00:00:00Z | gzip -n` and byte-identical across two rebuilds.
  Its archive hash is
  `9ab55b53e4639beb0ca7d7787137722125ad56b64cc7839dff9bbde8a290d81e` and its
  per-file SHA-256 manifest hashes to
  `67fe363e3f8f60ce9579b15a206b927ec644280c8ff4567a06d0087acd0be7f4`. It lives on
  the controller at `/home/aup/vllm-sr-evidence/archives/` with an independent,
  hash-verified replica on a separate host/filesystem so the new mirror is no
  longer single-copy. `perf/validate_agentic_context_reports.py --backup-archive`
  re-derives both hashes from the archive contents.
- **Ongoing integrity checks.** `perf/check-evidence-archives.sh` re-derives the
  immutable backup archive and the 217-entry controller prefill manifest
  (`agentic-prefill-20260722/campaign-checksums.sha256`, hash
  `d9dd7ecf6ebd1e72bdcacf1bca4f6f6a4690a12307d9182729da06338ee7ebc6`) from their
  raw bytes and fails on any drift. Schedule it weekly with the reference
  `perf/systemd/vllm-sr-evidence-check.{service,timer}` units (`systemctl --user`,
  no root required). For off-box durability, additionally copy the archive to
  independent object storage; the controller disk plus the single hash-verified
  replica are otherwise the only two copies.
- Large raw JSONL/request evidence remains outside git; this ledger records the
  result families and canonical evidence paths.

## Final four-proof status (2026-07-23)

- **Capacity — PARTIAL PASS:** full transport/usage, mixed marker correctness.
- **Performance — MEASURED, no agreed SLO:** direct Ollama cold TTFT p50 was
  3.8s / 5.9s / 12.6s / 30.4s / 83.2s at 2,048 / 8,192 / 16,384 / 32,768 /
  65,152 input tokens. Ollama showed no reuse acceleration; the separate vLLM
  APC 32K c4 result was 144.3s cold → 30.8s warm.
- **Quality — NOT ACHIEVED:** native tools 21/22 arguments/steps (95.45%);
  real-agent smoke 1/4 tasks (25%); long-horizon quality unrun.
- **Reliability — NOT RUN:** no soak, recovery, or side-effect idempotency test.

Canonical reader views are the [focused customer brief](agentic-context-customer-onepager-20260722.md)
and [structured four-proof status](agentic-context-customer-20260722-four-proof-status.json).
Remaining work is three-repetition quality with a durable runner, matched
router/direct A/B, then a representative 32K/65,152 soak and recovery run.
