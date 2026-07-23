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

## Campaign result ledger

| Host | Run family | Role | Planned / recorded result | Transport / correctness result | Evidence |
| --- | --- | --- | --- | --- | --- |
| `demo-002` | Runtime context proof | Qualification | Configured proof passed; two loaded proofs passed at context 65,536 | Model loaded fully on GPU; allocation proof only | `.agent-harness/experiments/runtime-context-proof/demo-002/20260722T005250Z-configured.json`, `...005306Z-loaded.json`, `...005400Z-loaded.json` |
| `demo-002` | Direct canary | Superseded qualification | 1/1 cell recorded, failed overall | Calibration and HTTP worked; this early canary was superseded by the milestone/final capacity runs | `.agent-harness/experiments/capacity-matrix/20260722T005852Z/direct-canary-summary.json` |
| `demo-002` | Early direct spine | Superseded partial | 3 cells: 1 success, 2 failed | Exploratory 2K/8K/16K checkpoints; superseded | `.agent-harness/experiments/capacity-matrix/20260722T005852Z/active-direct-spine.jsonl` |
| `demo-002` | Ollama milestone profile | Acceptance | 8/8: five spine contexts (2K, 8K, 16K, 32K, 65,152), one 32K/reuse90, two 8K concurrency cells | All 8 succeeded; focused pytest 14 passed and agent lint passed | `.agent-harness/experiments/capacity-matrix/20260722T023157Z-milestones/milestone-summary.json` |
| `demo-002` | Router path for milestone | Blocked | Not run | No canonical router checkout/image/listener on the measurement host; unrelated K3s/JupyterHub was preserved | `.agent-harness/experiments/capacity-matrix/20260722T023157Z-milestones/router-blocker.json` |
| Halo-A | vLLM primary pilots | Qualification | 8/8 pilot cells completed | 16/16 HTTP; 16/16 markers | `/home/aup/vllm-sr-evidence/agentic-prefill-20260722/vllm-primary-pilot*.summary.json` |
| Halo-A | vLLM primary coverage, output 64 | Acceptance (v1 transport semantics) | 60/60 cells completed: contexts 512/2K/8K/16K/32K × reuse 0/50/90 × c1/2/4/8 | 450/450 HTTP; 180/450 markers (24 cells had full marker accuracy, 36 had zero) | `.../vllm-primary-coverage-out64.summary.json` |
| Halo-A | vLLM representative output 256 | Acceptance (v1 transport semantics) | 6/6 cells completed across reuse0/c1 and reuse90/c8 at 2K/16K/32K | 54/54 HTTP; 18/54 markers | `.../vllm-primary-out256-*.summary.json` |
| Halo-A | vLLM batching/APC ablation | Acceptance (v1 transport semantics) | 25/25 dedicated cells completed across batch 2048/8192/16384 and APC on/off; the batch8192/APC-on baseline is the primary coverage run | 260/260 HTTP; 80/260 markers | `.../vllm-b*-*.summary.json` |
| Halo-A | vLLM native tools | Acceptance | 22 tasks / 25 tools | 22/22 valid JSON and tool names; 21/22 correct arguments/steps; 0 transport failures | `.../vllm-native-tools.json` |
| Halo-A | vLLM real-agent smoke | Acceptance | 4 tasks, 16 requests | 16/16 HTTP; 1/4 exact task passes; no tool execution, portability, or tool-loop switch violations | `.../vllm-real-agent-smoke/summary.json` |
| Halo-A | llama.cpp pilot | Qualification | 1/1 cell succeeded | 2/2 HTTP; 2/2 markers | `.../llamacpp-pilot.summary.json` |
| Halo-A | llama.cpp Q4_K_M coverage, output 64 | Partial acceptance | 60 unique cells: 51 transport-success, 2 bounded-timeout failures, 7 explicit skips; one duplicate raw checkpoint row is excluded | 374/374 executed requests returned HTTP success; 41/374 markers | `.../llamacpp-coverage-out64.summary.json` and append-only JSONL |
| Halo-A | llama.cpp representative output 256 | Partial | 6 unique checkpoints: 4 failed, 2 delegated/skipped; one 16K/c8 cell timed out | 20/20 executed requests returned HTTP success; 0/20 markers | `.../llamacpp-out256-r0-c1.jsonl`, `...r90-c8.jsonl` |
| Halo-A | llama.cpp native tools | Acceptance | 22 tasks / 25 tools | 22/22 valid JSON; 2/22 correct names/arguments/steps; 0 transport failures | `.../llamacpp-native-tools.json` |
| Halo-A | llama.cpp real-agent smoke | Acceptance | 4 tasks, 16 requests | 16/16 HTTP; 3/4 exact task passes; no tool execution, portability, or tool-loop switch violations | `.../llamacpp-real-agent-smoke/summary.json` |
| Halo-A | Ollama pilot | Qualification | 1/1 cell succeeded | 2/2 HTTP; 2/2 markers | `.../ollama-pilot.summary.json` |
| Halo-A | Ollama Qwen2.5-7B Q4 coverage, output 64 | Partial | 60 cells: 12 success, 31 correctness-failed, 17 skipped when work moved to `demo-002` | 314/314 executed requests returned HTTP success; 90/314 markers | `.../ollama-coverage-out64.summary.json` |
| `demo-002` | Pinned vLLM v0.25.1 long-context matrix | Acceptance | 48/48 cells checkpointed: batch 2048/8192/16384 × APC on/off × 16K/32K × reuse0/90 × c4/8 | 435/448 HTTP (97.1%); 0 correct markers; 8 bounded-timeout cells; no OOM/restart | `/home/demo-002/vllm-sr-evidence/agentic-prefill-20260722/analysis/vllm-out64-ablation-summary.json` |
| `demo-002` | Pinned vLLM output 256 | Acceptance result (failed quality gate) | 4/4 cells checkpointed | 40/48 HTTP; 0 markers | `.../raw/vllm-b8192-apc-on-large-out256.summary.json` |
| `demo-002` | Pinned vLLM native tools | Acceptance | 22 tasks / 25 tools | 22/22 valid JSON and names; 21/22 correct arguments/steps | `.../raw/vllm-native-tools.json` |
| `demo-002` | Pinned vLLM real-agent smoke | Acceptance | 4 tasks, 16 requests | 16/16 HTTP; 1/4 exact task passes | `.../raw/vllm-real-agent-smoke/summary.json` |
| `demo-002` | Final direct Ollama Gemma Q8 capacity | Acceptance | 17/17 cells: 7 success, 10 correctness-failed | 174/174 HTTP; 150/174 markers; both 65,152-token c2/c4 cells passed completely | `/home/demo-002/vllm-sr-evidence/agentic-context-customer-20260722/analysis/final-selected-scope-summary.json` |
| `demo-002` | Replay preflight while vLLM owned GPU | Blocked | No replay request sent | GPU ownership fail-closed; services remained healthy | `.agent-harness/experiments/capacity-matrix/20260722T023157Z-milestones/agentic-replay/.../limitations.json` |
| `demo-002` | Replay v1, three repetitions | Measured failure | 3/3 repetitions ended rapidly; no acceptance pass | All 256/1024/4096 payload calibrations returned HTTP 400 with missing authoritative usage; runtime stability gates passed | `.../replay-direct-rep{1,2,3}/summary/agentic-replay-profile.json` |
| `demo-002` | Replay v2 repetition 1 | Partial, user-scope abort | Fixed replay passed (28 regular / 32 total tool turns); branch replay passed; quality not run | 106 resource samples preserved; repetitions 2/3 not run | `.../replay-direct-v2-rep1/`, `.../limitations/replay-v2-aborted-user-scope-20260723.json` |
| `demo-002` | Replay v3 repetition 1 | Partial, user-scope abort | Fixed and branch replay passed; quality not run | 69 resource samples preserved; repetitions 2/3 not run | `.../replay-direct-v3-rep1/`, `.../limitations/replay-v3-aborted-user-scope-20260723.json` |
| `demo-002` | New same-host llama.cpp comparison | Deferred | Not run | Existing Halo-A llama.cpp evidence is retained; no `demo-002` parity claim | User scope decision in `.../limitations/user-scope-reduction-20260723.json` |

## Canonical conclusions

1. **Pinned vLLM works on gfx1151 for the measured stack**, but transport support
   is not agentic quality and is not blanket support for arbitrary images/models.
2. **Long-context instruction adherence is the dominant failure mode.** HTTP
   success remains high even where marker/quality gates fail.
3. **llama.cpp is not an unqualified winner for this campaign.** Its existing
   low-TTFT server results remain valid, but this Qwen2.5-7B agentic campaign
   records only 2/22 correct native-tool steps and 3/4 real-agent smoke tasks.
4. **Ollama's 65,536-token allocation executed the final selected workload**
   without transport failure; 7/17 cells passed every gate and both near-limit
   concurrency cells passed.
5. **No full long-horizon replay acceptance exists.** The v1 repetitions failed
   calibration; v2/v3 proved fixed+branch semantics only and were stopped before
   quality by the explicit scope decision.

## Evidence integrity

- Halo-A campaign root:
  `/home/aup/vllm-sr-evidence/agentic-prefill-20260722`
- `demo-002` customer/replay root:
  `/home/demo-002/vllm-sr-evidence/agentic-context-customer-20260722`
- At ledger generation, the controller campaign manifest covered **217 files** with
  SHA-256 `d9dd7ecf6ebd1e72bdcacf1bca4f6f6a4690a12307d9182729da06338ee7ebc6`.
- The final compact `demo-002` archive on the controller covered **41 files**
  with SHA-256 `57268733bc1734228aaad832124b391fca09547b5f5593191a349f84e0b084fc`;
  the full remote campaign manifest is retained alongside it.
- Large raw JSONL/request evidence remains outside git; this ledger records the
  result families and canonical evidence paths.

## 2026-07-23 finalization: direct capacity 17/17, replay, four-proof

This section closes the campaign with the demo-002 Ollama direct-path customer run (gemma4:26b-a4b-it-q8_0, Q8_0). Raw evidence lives outside git at demo-002:/home/demo-002/vllm-sr-evidence/agentic-context-customer-20260722 and is mirrored to Halo-A at ~/vllm-sr-evidence/agentic-context-customer-20260722 (151-file SHA256SUMS-final.txt; tarball sha256 in archives/ARCHIVE-SHA256.txt). Machine-readable status is committed alongside this ledger as agentic-context-customer-20260722-four-proof-status.json and agentic-context-customer-20260722-evidence-index.json.

| Host | Run family | Role | Planned / recorded result | Transport / correctness result | Evidence |
| --- | --- | --- | --- | --- | --- |
| demo-002 | Direct capacity spine/reuse/concurrency (OpenAI /v1) | Acceptance transport / Partial marker | 17/17 cells; 7 fully green, 10 marker-gate-only failures | Transport 17/17 (100 percent) and exact prompt-token usage 17/17 (100 percent); the 10 failures are marker-accuracy under synthetic filler, not serving failures | capacity-direct-openai/summary/direct-spine.json + direct-concurrency.json + direct-reuse.json |
| demo-002 | Replay v1 three reps | Superseded failure | 0 usable; immediate HTTP 400 | Harness sent OpenAI tool_calls.function.arguments as JSON strings; Ollama native /api/chat rejected them; fixed in commit 0f8b7f2b | replay-direct-rep1..3 |
| demo-002 | Replay v2 and v3 rep1 | Partial | fixed + branch passed; quality never ran | fixed checkpoints 8K/16K/32K/65152 marker-preserved, 32 tool turns, branch pass; quality 0 rows | replay-direct-v3-rep1/direct/summary/fixed-replay.json + branch-replay.json |
| demo-002 | Long-horizon quality suite | Blocked | Not completed across three reps | Background runner stopped when its launching SSH session closed; demo-002 lacks loginctl linger | four-proof-status.json quality section |
| demo-002 | Reliability soak/fault/side-effect | Deferred | Not run | No soak, fault-injection, or idempotency test this round | four-proof-status.json reliability section |

### Four-proof verdict (2026-07-23)

- Capacity: PARTIAL PASS. 64K serving window verified-loaded (ollama ps 100 percent GPU, CONTEXT 65536, 0 restart / 0 OOM, peak VRAM about 30.7 GB). Exact-token spine 2K/8K/16K/32K/64K healthy on all cells.
- Performance: MEASURED (no agreed SLO). TTFT p50 prefill-bound: 2K 3.8s, 8K 5.9s, 16K 12.6s, 32K 30.4s, 64K 83s. On the Ollama direct path warm equalled cold (no prefix-cache speedup, cached ratio not reported); the 32K 144.3s to 30.8s reuse figure comes only from the sibling VLLM APC run.
- Quality: NOT ACHIEVED. Tool schema fidelity strong (JSON 100, name 100, args 95.45) and replay integrity passed, but end-to-end agent task success was 25 percent and the three-rep long-horizon quality suite never completed.
- Reliability: NOT RUN.

### Remaining high-value work

1. Enable loginctl linger (or a detached container) on demo-002 and re-run the three-rep quality suite with the applied Ollama-argument fix.
2. Build or install the router on demo-002 for a matched router-versus-direct A/B.
3. One 32K/64K soak plus a fault-recovery run.
