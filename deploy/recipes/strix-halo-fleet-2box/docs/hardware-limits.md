# 我們離硬體極限還有多遠 / How far are we from the hardware limit (Halo-B)

> **Every number on this page is measured [M]** on **Halo-B** (Ryzen AI Max+ 395,
> gfx1151, 128 GiB LPDDR5X, **BIOS 96 GiB VRAM carveout**, headless, full vllm-sr stack
> co-resident), unless explicitly flagged **[E] extrapolation**. Raw data:
> [`perf/quant-frontier/`](../perf/quant-frontier/). Companion analysis in
> [`perf-report.md` §11.2](perf-report.md) and [`halo-b-maxmodel.md`](halo-b-maxmodel.md),
> and the interactive canvas `strix-halo-hardware-limits`.

The box is bounded by three walls. This page pushes each one with a real experiment and
reports where it *actually* sits — not where we previously guessed:

- **Capacity** — does the model fit inside the 96 GiB VRAM carveout?
- **Bandwidth** — LPDDR5X decode, where `tok/s ≈ mem-bandwidth / bytes-per-token`.
- **Power** — the socket graphics package pins to a fixed ~100–133 W envelope.

The short answer: at 96 GiB, **capacity is now characterized to the carveout edge — bandwidth
is the binding wall, and power sits right at the TDP envelope.** The largest real model we have
loaded is VRAM-resident at **94.59 GiB** (~1.4 GiB shy of the 96 GiB carveout) and still does not
decode any faster — bigger weights fit, they just stay bandwidth-bound.

## 1. The three walls — verdict [M]

| Wall | Verdict | Measured evidence | Headroom |
| --- | --- | --- | --- |
| **Capacity** (fit 96 GiB carveout) | **Characterized to the carveout edge** | Largest *real* model resident = `mixtral:8x22b-...-q5_K_M` (141B MoE) at **94.59 GiB, 100% GPU, GTT 0.05**, 7.80 tok/s, usable | **~1.4 GiB** to the 96 GiB carveout — the residency break is now at/above the carveout itself, so the carveout *is* the capacity limit |
| **Bandwidth** (LPDDR5X decode) | **Hit / hard** | dense 70B decodes **3.0–5.1 tok/s** (Q8→Q4); 7B concurrency saturates at **~128 tok/s** (p8); the quant sweep is monotonic in footprint | none — intrinsic to LPDDR5X |
| **Power** (socket TDP) | **Near TDP** | dense 70B-Q4 draws **~133 W mean** (137 W peak) under sustained decode — the highest we measured | little — the socket pins to a ~100–133 W ceiling |

**Takeaway:** the old ~90 GiB extrapolated break point is now superseded by a real **94.59 GiB**
measurement — a 141B MoE loaded 100% VRAM-resident and still usable only ~1.4 GiB below the
carveout, so **capacity is characterized essentially to the carveout edge** (the 96 GiB carveout
itself is the capacity limit). The wall we *do* hit is **bandwidth**, and **power is already near
the socket envelope**.

## 2. Halo-B measured limits [M]

**Capacity — residency ceiling moved 81.2 → 94.59 GiB.** The new top rung
`mixtral:8x22b-instruct-v0.1-q5_K_M` (141B MoE) loads **100% VRAM-resident at 94.59 GiB**
(`size_vram/size` = 1.0, peak GTT 0.05 GiB, peak system RAM 6.42 GiB, TTFT ~180 ms) and decodes
**7.80 tok/s** at MMLU-Pro **45.2% (19/42)** — verdict **usable**, `first_unusable = null`. That
replaces the previous largest *measured* resident footprint (81.2 GiB,
`mixtral:8x22b-...-q4_K_M`) and sits only **~1.4 GiB below the 96 GiB carveout**. The old
**~90 GiB weight-ceiling extrapolation is now superseded by this real 94.59 GiB measurement** —
still 100% resident and usable, so the residency break is at/above the carveout itself and
**capacity is characterized essentially to the carveout edge**.

**Dense usable edge ≈ 70B.** Across Q4→Q8 the dense `llama3.1:70b` stays VRAM-resident and
**≥3 tok/s** (5.1 → 3.0 tok/s). The next dense step up (`qwen2.5:72b-instruct-q8_0`, 72.6 GiB)
still *fits* (GTT ~0, no CPU offload) but decodes only **2.94 tok/s**, a hair under the 3 tok/s
usable floor. The dense edge is therefore set by **bandwidth, not capacity**.

**MoE frontier — bigger *and* faster than dense.** At similar or larger footprints the MoE
models beat dense on speed, because only the active experts are read per token:

| Model | Params (active) | Peak VRAM | Decode tok/s |
| --- | --- | --- | --- |
| `gemma4:26b` (Q4_K_M) [M] | 25B (~3.8B) | 21.6 GiB | **58.4** |
| `gpt-oss:120b` (MXFP4) [M] | 120B (~5.1B) | 60.5 GiB | **~36.5** |
| `mixtral:8x22b-...-q3_K_M` | 141B (~39B) | 64.6 GiB | **10.8** |
| `mixtral:8x22b-...-q4_K_M` | 141B (~39B) | 81.2 GiB | 9.03 |
| `mixtral:8x22b-...-q5_K_M` | 141B (~39B) | **94.59 GiB** | **7.80** |
| `llama3.1:70b-instruct-q8_0` (dense) | 70B (70B) | 70.7 GiB | 3.0 |

The new **`gemma4:26b` [M]** rung shows the MoE speed-at-footprint edge not only *holds* at small
total size but *sharpens*: a 25B-total MoE (~3.8B active) decodes **58.4 tok/s** VRAM-resident at
just **21.6 GiB** — **the fastest MoE measured on this box**, ahead of `gpt-oss:120b` (~36.5) —
because it reads only ~3.8B active params/token. The same-box dense `gemma4:31b` manages **11.3
tok/s** at a similar 19.4 GiB footprint (see §5), a ~5× gap that confirms *MoE is big **and** fast*
holds down at 25B total.

**Concurrency ceiling — ~128 tok/s at `OLLAMA_NUM_PARALLEL=8` (was ~107 at p4).** Re-running
the 7B (`qwen2.5:7b`) concurrency sweep with 8 parallel slots raises the plateau and moves the
**knee c4 → c8**:

| c | p4 agg tok/s (§5) | **p8 agg tok/s** | p8 TTFT p95 |
| --- | --- | --- | --- |
| 1 | 41.7 | 42.27 | 154 ms |
| 2 | 66.5 | 69.11 | 374 ms |
| 4 | 100.0 | 103.05 | 416 ms |
| 8 | 107.3 | **120.28** | 825 ms |
| 16 | 107.2 | **127.76** | 8129 ms |

The knee is now **c8** (120.28 tok/s, TTFT p95 825 ms); pushing to c16 buys only ~6% more
throughput (127.76 tok/s) while TTFT p95 explodes to 8.1 s. Same silicon, same 7B — this is a
**higher bandwidth plateau, not a different wall**: decode stays bandwidth-bound.

**Power / per-watt (96 GiB, forced-resident).** The socket pins to a fixed envelope, so
efficiency tracks throughput — and the dense 70B-Q4 is both the worst per-watt point and the
closest to the TDP ceiling:

| Model | Decode tok/s | Mean load W | tok/s per W |
| --- | --- | --- | --- |
| `gpt-oss:120b` (120B MoE) | 36.51 | 95.6 | **0.382** |
| `llama3.1:70b-instruct-q4_K_M` (dense) | 5.07 | **133** | **0.0381** |

Prior Halo-A baselines (§12): 7B **0.41**, 32B **0.093** tok/s/W. The MoE is ~**10× more
energy-efficient per token** than the dense 70B-Q4 (0.382 vs 0.0381) *and* draws less power
(95.6 vs 133 W). **Dense 70B-Q4 pulls ~133 W mean (137 W peak) — the highest we measured, right
at the TDP envelope.**

## 3. Recommended local defaults — and why (each reason is measured) [M]

- **Balanced local/default model: `gemma4:26b-a4b-it-q8_0`.** It is the best default blend in the
  current frontier: **44.6 tok/s**, **25.3 GiB**, **71.4% (30/42)** MMLU-Pro, and **0.418 tok/s/W**.
  It beats `gpt-oss:120b` on quality, speed, footprint, and default suitability while keeping the
  Gemma 26B MoE interaction feel.
- **Throughput/demo default: `gemma4:26b` Q4_K_M.** This is the best-feeling demo path:
  **58.4 tok/s**, **21.6 GiB**, **69.0% (29/42)**, **0.481 tok/s/W** — the fastest high-quality MoE
  point in the measured table.
- **Compact/fast edge: `gemma4:26b-a4b-it-qat`.** Use it when footprint or raw throughput matters
  most: **65.0 tok/s**, **13.8 GiB**, **64.3% (27/42)**, **0.400 tok/s/W**. It is fast and compact,
  but the quality drop is real, so it is not the balanced default.
- **Quality-only local rung: `gemma4:31b-it-qat`.** It has the best measured local quality
  (**78.6%**, 33/42) and stays compact (**18.5 GiB**), but at **12.3 tok/s** / **0.090 tok/s/W** it is
  too slow to be the default unless the deployment is explicitly quality-first.
- **120B capacity/reference: `gpt-oss:120b`, not the default.** It remains important as a big-MoE /
  120B capacity story (**60.5 GiB**, **~36.5 tok/s**, **64.3%**, **0.382 tok/s/W**) and as the
  reference model for the residency/serving matrix, but the Gemma 26B MoE rungs are better defaults
  on speed, quality, VRAM, and energy.
- **Operational carveout: 64 GiB for Gemma default, 96 GiB for capacity work.** The Gemma 26B
  defaults peak at only **13.8–25.3 GiB**, so day-to-day local serving should prefer a **64 GiB**
  BIOS carveout to regain ~62 GiB OS-visible system RAM and keep Ollama's default budgeting sane.
  Keep **96 GiB** for capacity/frontier work: it is what lets >60 GiB reference models and the
  **94.59 GiB** `mixtral:8x22b-q5_K_M` resident-footprint record fit.
- **Headless + forced residency for big references.** Headless frees the carveout; `-vram` variants
  (`num_gpu=999` + `use_mmap=false`) avoid Ollama's 96 GiB auto-budget trap, where `gpt-oss:120b`
  regresses **36.8 → 5.7 tok/s** by CPU-offloading against the 30 GiB system-RAM budget.
- **`OLLAMA_NUM_PARALLEL=8` for 7B-class concurrency; size slots to workload for larger models.**
  The measured 7B knee moved c4 → c8 and the ceiling rose to **~120–128 tok/s**. For 120B capacity
  runs, parallelism is a latency/throughput trade rather than a free default.
- **llama.cpp (rocm) as the serving backend** — the fastest / lowest-TTFT server on this box.
- **Router semantic cache threshold 0.92 + exact-repeat.**
- **Classifiers pinned to CPU** — GPU offload is blocked on gfx1151; CPU-pinned they add only
  ~8.5 GiB system RAM and never shrink the VRAM carveout the models load into.

- **Candidate sweep update (Halo-B, 2026-07-15) [M].** A broad P0 + capped P1/P2 sweep did **not** displace Gemma 4. The best speed candidate, `qwen3-coder:30b`, hit **71.0 tok/s** in **18.1 GiB** but only **54.8% (23/42)**. `qwen3-next:80b` was fast enough for default consideration (**49.6 tok/s**, **47.4 GiB**) but scored **61.9% (26/42)**. `qwen3.6:27b` matched the Gemma Q4 quality sample (**69.0%**, 29/42) but was much slower (**13.5 tok/s**) and inefficient (**0.082 tok/s/W**). Lower-priority measured candidates also missed the default bar (`mistral-small:24b` **15.2 tok/s / 54.8%**, `deepseek-r1:32b` **11.0 tok/s / 50.0%**); EXAONE (**50.0%**, 21/42) and Phi-4 reasoning plus (**57.1%**, 24/42) quality were later completed in the operating-profiles run (both below the `gemma4:31b-it-qat` quality pick of 78.6%; see [`profiles-summary-halo-b.md`](../perf/quant-frontier/profiles-summary-halo-b.md)), while OpenThinker/Magistral quality remain pending; EXAONE is research-only/non-commercial, GLM-4.5-Air and DeepSeek-R1 70B were skipped to keep the sweep bounded, and Falcon-H1 manifests were unavailable. Raw data and skip notes: [`candidate-summary-halo-b.json`](../perf/quant-frontier/candidate-summary-halo-b.json) / [`candidate-summary-halo-b.md`](../perf/quant-frontier/candidate-summary-halo-b.md).

## 3.1 Operating profiles — which model per workload [M]

The single-"best-model" question is the wrong one for a customer: the right model depends on the
**workload**. The matrix below turns the measured frontier (§3/§5) into a recommendation per
operating profile. Every headline number is measured on Halo-B and matches the frontier JSON in
[`perf/quant-frontier/`](../perf/quant-frontier/); the profile-specific follow-up measurements
(agentic tool-call, per-model multiagent concurrency, EXAONE/Phi-4 quality completion) are
collected in [`profiles-summary-halo-b.md`](../perf/quant-frontier/profiles-summary-halo-b.md).

| Profile | Recommended model | Route | Measured headline | Carveout |
| --- | --- | --- | --- | ---: |
| **Single-turn** request/response | `gemma4:26b-a4b-it-q8_0` (balanced); `gemma4:26b` Q4 for fastest demo feel | auto (default) | Q8 **44.6 tok/s**, **71.4% (30/42)**, 25.3 GiB, 0.418 tok/s/W, TTFT ~0.37 s · Q4 **58.4 tok/s**, 69.0%, 21.6 GiB | 64 GiB |
| **Agentic / tool-calling** | `gemma4:26b-a4b-it-q8_0` default (mixed reason+tool); `qwen3-coder:30b` for tool-call-heavy volume | auto (default) + by-name | On the frozen 15-task tool-call probe **all three score 100% (15/15) valid+correct**, so speed decides: Qwen3-Coder **1.28 s/step @ 72.2 tok/s** vs Q8 **2.63 s/step @ 41.4 tok/s**; Q8 keeps the higher generic quality (71.4% vs 54.8%) for mixed reasoning | 64 GiB |
| **Multiagent / concurrent** | `gemma4:26b` Q4 (throughput) or Q8 (quality), `OLLAMA_NUM_PARALLEL=8` | by-name + server flag | 7B concurrency plateau **~120–128 tok/s**, knee **c8**, TTFT p95 ~825 ms @ c8; per-model Q4/Q8/Qwen c1/c2/c4/c8 in summary | 64 GiB |
| **Quality-only** local | `gemma4:31b-it-qat` | by-name | **78.6% (33/42)** — best local quality — at 18.5 GiB, but **12.3 tok/s** / 0.090 tok/s/W (too slow to default) | 64 GiB |
| **Capacity / reference demo** | `gpt-oss:120b` (`-vram` variant), explicit by-name, never auto-routed | by-name only | **~36.5 tok/s**, **64.3% (27/42)**, 60.5 GiB, 0.382 tok/s/W — the "this box holds a 120B MoE" story | **96 GiB** |

**Scorecards (what each profile is judged on, and the measured evidence):**

- **Single-turn** — TTFT, decode tok/s, 42Q quality, power, VRAM, 64 GiB fit. `gemma4:26b-a4b-it-q8_0`
  wins the blend (44.6 tok/s, 71.4%, 25.3 GiB, 0.418 tok/s/W, TTFT ~0.37 s); switch to `gemma4:26b`
  Q4 when the demo should feel fastest (58.4 tok/s, 69.0%).
- **Agentic / tool-calling** — JSON/tool-call validity, tool-selection + argument correctness, latency
  per step, failure rate. Measured on a small frozen 15-task set
  ([`agentic-toolcall-tasks.json`](../perf/data/agentic-toolcall-tasks.json), scored by
  [`agentic_toolcall.py`](../perf/agentic_toolcall.py)): `gemma4:26b-a4b-it-q8_0`, `qwen3-coder:30b`,
  and `gemma4:31b-it-qat` **all scored 100% (15/15)** valid JSON + correct tool + correct args, so
  tool-call validity does not separate them on this probe — **speed** does (Qwen3-Coder 1.28 s/step,
  Q8 2.63 s/step, 31B-QAT 5.28 s/step). Recommendation: `qwen3-coder:30b` for tool-call-heavy,
  high-volume agent loops; keep balanced **Q8** as the default when the agent also needs general
  reasoning (its 71.4% generic MMLU-Pro beats Qwen3-Coder's 54.8%). n=15 is indicative, not a broad
  agent eval.
- **Multiagent / concurrent** — aggregate tok/s, per-agent tok/s, TTFT p50/p95 across c1/c2/c4/c8.
  The measured 7B curve sets the shape (knee **c8**, ceiling **~128 tok/s**, TTFT p95 825 ms @ c8);
  the per-model Q4/Q8/Qwen3-Coder sweep (`OLLAMA_NUM_PARALLEL=8`, forced-resident) quantifies the
  Q4-vs-Q8 throughput/latency trade for concurrent local agents.
- **Quality-only** — 42Q MMLU-Pro (plus the EXAONE 4.0 32B / Phi-4-reasoning-plus completion that the
  capped candidate sweep could not finish). `gemma4:31b-it-qat` is the best measured local quality
  (**78.6%**) and stays compact (18.5 GiB), but 12.3 tok/s makes it quality-first only. EXAONE is
  **research-only / non-commercial** and never a default.
- **Capacity / reference** — capacity, residency, customer wow factor (explicitly *not* default
  suitability). `gpt-oss:120b` fully VRAM-resident is the 120B story; it needs the **96 GiB** carveout,
  whereas the Gemma serving rungs run in **64 GiB** with ~62 GiB system RAM to spare.

**Carveout rule:** the four Gemma rungs peak at **13.8–25.3 GiB**, so day-to-day local serving of any
single-turn / agentic / multiagent / quality-only profile should use the **64 GiB** BIOS carveout;
**96 GiB** is reserved for the capacity/reference profile (>60 GiB resident models). This split is
mirrored in [`poc-strix.yaml`](../../strix-halo-poc/poc-strix.yaml) (balanced Q8 is the auto-routed
default; Q4 serves the fast lane; compact/quality/capacity are explicit by-name).

## 4. 120B capacity/reference A/B (not the local default) [M]

This A/B remains useful for the **120B capacity story**, but it is no longer the local/default
recommendation. The default conclusion now comes from §3/§5: use Gemma 4 26B MoE for balanced,
throughput, or compact local serving; keep `gpt-oss:120b` as the big-MoE reference.

The 120B levers were run **head-to-head, end-to-end** on Halo-B (96 GiB carveout, headless,
full vllm-sr stack co-resident, 2026-07-13): **resident/reference** (`gpt-oss:120b-vram` forced
resident + `OLLAMA_NUM_PARALLEL=8` + semantic cache 0.92/exact-repeat; llama.cpp recommended for
TTFT, the big-model decode measured on ollama here) vs a **naive plain-tag baseline**
(`gpt-oss:120b` auto layer-estimate, `NUM_PARALLEL=1`, no cache, ollama). Each row is one lever,
with the measured delta and why the resident reference wins. Data:
[`perf/quant-frontier/bestcfg-halo-b.json`](../perf/quant-frontier/bestcfg-halo-b.json).

| Lever | BEST | DEFAULT | Delta | Why BEST wins |
| --- | --- | --- | --- | --- |
| **Residency** (`-vram`: `num_gpu=999`,`use_mmap=false` vs auto) | **36.6 tok/s @ 100% GPU** (`size_vram/size`=1.0, peak VRAM 61.65 GiB) | auto layer-estimate CPU-offloads — 0.177 tok/s @ 38.3% GPU this run (disk-pressure; see caveat) | **~6.4×** (clean-run reference 36.8 vs 5.7, §11.1) | Auto-estimate sizes GPU layers to the 30 GiB OS-visible system RAM, not the 96 GiB carveout → CPU-offloads a 60 GiB MoE; forcing residency pins 100% on GPU |
| **Concurrency** (`OLLAMA_NUM_PARALLEL=8` vs 1, 7B) | **121.1 tok/s agg @ c8** (ceiling 128.7 @ c16), TTFT p95 826 ms | 43.9 tok/s agg @ c8 (flat ~43), TTFT p95 20,444 ms | **~2.76×** @ c8 (**~2.93×** ceiling), **~25×** better TTFT p95 @ c8 | One decode slot serializes (aggregate flat, TTFT explodes with the queue); 8 slots scale to the LPDDR5X bandwidth plateau, knee c8 |
| **Semantic cache** (0.92 + exact-repeat vs none) | exact-repeat hit ~1–2 ms; semantic-0.92 hit ~0.7–0.9 s | miss ~1.2–1.56 s (full embed+classify+route+decode) | **>100×** on repeats/paraphrases | A hit skips the upstream LLM leg; an exact-repeat pre-routing cache skips embed+classify+routing entirely |
| **Server** (llama.cpp rocm vs ollama) | TTFT ~28 ms | TTFT ~142 ms | **~5×** lower TTFT | llama.cpp is the fastest / lowest-TTFT server measured on gfx1151 |
| **Architecture** (MoE vs dense big model) | MoE reference: `gpt-oss:120b` 36.5 tok/s; local default: Gemma 26B MoE 44.6–58.4 tok/s | dense Q8 70B 3.0 tok/s | **~12×** for 120B reference vs dense Q8; larger for Gemma 26B vs dense Q8 | MoE reads only the active experts per token, so bandwidth-bound decode is far faster than a same-size dense model |
| **Quant** (dense Q4_K_M vs Q8_0) | 5.1 tok/s, ~30 GiB smaller | 3.0 tok/s | **~1.7×** decode, ~30 GiB VRAM saved, quality flat | Fewer bytes/token → faster bandwidth-bound decode; MMLU-Pro flat within the 42Q noise |
| **Per-watt** (resident MoE vs dense 70B-Q4) | 0.366 tok/s/W (36.5 @ 99.9 W) | 0.0381 tok/s/W (5.07 @ 133 W) | **~10×** more energy-efficient per token *and* less power | Socket pins to ~100–133 W; efficiency tracks throughput, so the resident MoE wins ~10× per token |

**Net for the 120B reference:** forcing VRAM residency + `NUM_PARALLEL=8` + cache + llama.cpp + MoE
turns a naive plain-tag baseline (auto CPU-offload, serialized, ~20 s p95 TTFT) into **36.6 tok/s**
resident, ~121 tok/s 7B-class concurrent capacity, sub-second p95 on that concurrency probe, and
~10× the energy efficiency vs dense 70B-Q4. That is the capacity/reference story; the day-to-day
local default is Gemma 4 26B MoE.

**Caveats (honest, from the run):**

- **The DEFAULT residency baseline decoded 0.177 tok/s because the box's disk was ~98% full** (25 GiB
  free) — the CPU-offloaded weights exceed the 30 GiB system RAM and paged from that near-full disk,
  so decode thrashed. That would read as a **~207× gap**, which we deliberately **do not headline**:
  the honest residency delta is the clean-run **~6.4×** (§11.1, 36.8 vs 5.7 tok/s). Treat 0.177 tok/s
  as a disk-pressure artifact, not the representative CPU-offload speed (~3–5.7 tok/s on a healthy disk).
- **The concurrency A/B is a real container toggle** — `OLLAMA_NUM_PARALLEL` was recreated
  `unset(=1)` → `=8` for BEST, then **restored** to the original (env unset → server default 1); env,
  restart policy, devices, and volume were verified identical to the pre-run inspect (15 models intact).
- **gpt-oss per-model TTFT was not captured** — its reasoning stream returns first tokens outside
  the probe's response field; the representative resident-model TTFT reference is mixtral-q4
  **142 ms** (§2).
- **The cache lever is reused from perf-report §7, not freshly re-measured on the live path.** The live
  config was confirmed enabled (`stores.semantic_cache` `enabled:true`) but not mutated, and route-local
  plugin coverage varies — so the >100× cache delta is carried over from perf-report §7.1/§7.5.

## 5. Quantization frontier — footprint × speed × quality (96 GiB) [M]

All rungs are **100% VRAM-resident** (`size_vram/size` = 1.0), forced `num_gpu=999` /
`use_mmap=false`, `num_ctx=4096`. Quality = 42 stratified MMLU-Pro questions (a small,
indicative sample — treat ±~7 pp as noise, not a real quality ranking). **New top rung: the
`mixtral-q5_K_M` row at 94.59 GiB — the largest real footprint measured, ~1.4 GiB below the
carveout.**

| Model (quant) | Type | Peak VRAM | Decode tok/s | MMLU-Pro (42Q) |
| --- | --- | --- | --- | --- |
| `llama3.1:70b-instruct-q4_K_M` | 70B dense | 41.0 GiB | **5.1** | 52.4% (22/42) |
| `llama3.1:70b-instruct-q5_K_M` | 70B dense | 47.8 GiB | 4.4 | 50.0% (21/42) |
| `llama3.1:70b-instruct-q6_K` | 70B dense | 55.0 GiB | 3.9 | 50.0% (21/42) |
| `llama3.1:70b-instruct-q8_0` | 70B dense | 70.7 GiB | 3.0 | 50.0% (21/42) |
| `mixtral:8x22b-instruct-v0.1-q3_K_M` | 141B MoE | 64.6 GiB | **10.8** | 42.9% (18/42) |
| `mixtral:8x22b-instruct-v0.1-q4_K_M` | 141B MoE | 81.2 GiB | 9.03 | 42.9% (18/42) |
| **`mixtral:8x22b-instruct-v0.1-q5_K_M`** | **141B MoE** | **94.59 GiB** | **7.80** | **45.2% (19/42)** |
| `gpt-oss:120b` [M] | 120B MoE MXFP4 | 60.5 GiB | **~36.5** | 64.3% (27/42) |
| `gemma4:26b` [M] | 25B MoE Q4_K_M | 21.6 GiB | **58.4** | 69.0% (29/42) |
| `gemma4:26b-a4b-it-q8_0` [M] | 25B MoE | 25.3 GiB | 44.6 | 71.4% (30/42) |
| `gemma4:26b-a4b-it-qat` [M] | 25B MoE | 13.8 GiB | **65.0** | 64.3% (27/42) |
| `gemma4:31b` [M] | 31B dense Q4_K_M | 19.4 GiB | 11.3 | 73.8% (31/42) |
| `gemma4:31b-it-q8_0` [M] | 31B dense | 32.4 GiB | 7.1 | 76.2% (32/42) |
| `gemma4:31b-it-qat` [M] | 31B dense | 18.5 GiB | **12.3** | **78.6% (33/42)** |

- **Monotonic in footprint (bandwidth-bound):** same 70B weights, decode climbs Q8 3.0 → Q6 3.9
  → Q5 4.4 → Q4 5.1 tok/s (~1.7× from Q8 to Q4) as bytes/token shrink.
- **Q4 is the dense sweet spot:** ~1.7× the Q8 speed, ~30 GiB smaller, and MMLU-Pro flat within
  the 42Q noise. Prefer `Q4_K_M` over Q8 for a dense 70B here.
- **MoE dominates the frontier:** the three mixtral MoE rungs (7.8–10.8 tok/s) and gpt-oss
  (~36.5 tok/s) sit above the dense line at every footprint. The **mixtral-q5 row is the largest
  real footprint measured (94.59 GiB, ~1.4 GiB below the carveout)** and still decodes 7.80 tok/s,
  100% VRAM-resident.
- MMLU differences reflect the base model/quant, not the architecture; the MoE point is *speed at
  footprint*, not MCQ score.
- **`gpt-oss:120b` quality now measured [M].** The resident 120B MoE keeps its 120B
  capacity/reference role (60.5 GiB, ~36.5 tok/s) and scores **64.3% (27/42)** on the same small
  MMLU-Pro set — below the balanced Gemma 26B default and equal to the compact Gemma 26B QAT rung,
  but well above the older mixtral rungs. As elsewhere, 42Q is indicative rather than a precise
  ranking.
- **Gemma 4 [M] — a modern MoE-vs-dense point at small size.** The 25B-total MoE `gemma4:26b`
  (~3.8B active) decodes **58.4 tok/s** at just 21.6 GiB — **the fastest MoE in this frontier**
  (ahead of `gpt-oss:120b` ~36.5) — and its `-qat` sibling hits **65.0 tok/s** at 13.8 GiB, while
  the dense `gemma4:31b` stays bandwidth-bound at 7.1–12.3 tok/s (Q8 7.1 < Q4 11.3, monotonic in
  footprint). Dense MMLU-Pro runs a touch higher (73.8–78.6%) than the MoE (64.3–71.4%), and
  `gemma4:31b-it-qat` is the standout — **78.6% (33/42), the highest here, at 18.5 GiB / 12.3
  tok/s**. All six sit well above the older rungs (mixtral 42.9%, llama3.1:70b 50–52%), but 42Q is
  a small indicative sample (±~7 pp), so read Gemma as *speed-at-footprint + modern MoE-vs-dense*,
  not an MMLU ranking.
- **Default conclusion from this frontier:** use Gemma 4 26B MoE for local/default serving:
  `gemma4:26b-a4b-it-q8_0` for the balanced default, `gemma4:26b` Q4_K_M for the throughput/demo
  default, and `gemma4:26b-a4b-it-qat` for compact/fast edge. Keep `gemma4:31b-it-qat` for
  quality-only local runs and `gpt-oss:120b` for 120B capacity/reference comparisons.

## 6. Remaining frontiers / next steps

- **Capacity is now characterized essentially to the carveout edge.** Largest real footprint =
  **94.59 GiB** (`mixtral:8x22b-...-q5_K_M`, 100% VRAM-resident, usable) — only **~1.4 GiB below
  the 96 GiB carveout** and still not the break, so the residency limit is now at/above the
  carveout itself rather than a ~90 GiB extrapolation. Pushing capacity further now means raising
  the BIOS carveout (firmware) or fitting a model into the last ~1.4 GiB — from here **decode
  bandwidth, not residency, is the binding wall**.
- **GTT overflow is still unused.** GTT stayed **~0** on every rung — ROCm/llama.cpp CPU-offloads
  rather than spilling weights to GTT, so the 48 GiB GTT pool is unexploited on Ollama (it may
  still matter for other runtimes such as Lemonade/vLLM).
- **The concurrency ceiling (~128 tok/s) is not fully mapped.** p8 lifts the plateau vs p4, but
  c16 already blows TTFT p95 to 8.1 s. Sweep intermediate `OLLAMA_NUM_PARALLEL` values (and
  llama.cpp / vLLM slotting) to locate the true bandwidth-bound throughput ceiling.
- **Power is near TDP.** Dense 70B-Q4 already pulls ~133 W, so there is little room to trade
  watts for speed — future speedups must come from *fewer bytes/token* (lower quant, MoE), not
  more power.

## 7. Related

- [`perf-report.md` §11.2](perf-report.md) — quantization frontier (footprint × speed × quality),
  with §11.1 (96 GiB re-test), §5 (concurrency p4 baseline) and §12 (per-watt baselines).
- [`halo-b-maxmodel.md`](halo-b-maxmodel.md) — max-model sweep, the 96 GiB carveout decision, and
  the `-vram` variant workflow.
- [`perf/quant-frontier/`](../perf/quant-frontier/) — raw `sweep-*.json`, `quality-*.json`,
  `pw-*.json`, and the p8 concurrency sweep.
- Companion interactive canvas **`strix-halo-hardware-limits`** — quant frontier, capacity
  headroom, MoE vs dense, concurrency p4/p8, and per-watt views.
