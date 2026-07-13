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
| `gpt-oss:120b` (MXFP4) | 120B (~5.1B) | 60.5 GiB | **~36.5** |
| `mixtral:8x22b-...-q3_K_M` | 141B (~39B) | 64.6 GiB | **10.8** |
| `mixtral:8x22b-...-q4_K_M` | 141B (~39B) | 81.2 GiB | 9.03 |
| `mixtral:8x22b-...-q5_K_M` | 141B (~39B) | **94.59 GiB** | **7.80** |
| `llama3.1:70b-instruct-q8_0` (dense) | 70B (70B) | 70.7 GiB | 3.0 |

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

## 3. Best configuration — and why (each reason is measured) [M]

- **BIOS 96 GiB VRAM carveout.** Only 96 GiB holds the >60 GiB models VRAM-resident that we now
  want as defaults (measured: mixtral-q4 **81.2 GiB**, dense-Q8-70B 70.7 GiB, gpt-oss 60.5 GiB);
  64 GiB physically cannot (its usable ceiling was ~56 GiB). Cost: OS-visible system RAM drops to
  ~30 GiB — verified acceptable co-resident with the stack.
- **Headless.** Frees the whole carveout (idle VRAM 0.14 GiB). This is the lever that moved the
  reliable ceiling 32B → 120B — *not* the enlarged GTT.
- **`-vram` model variants (`num_gpu=999` + `use_mmap=false`).** Mandatory at 96 GiB: Ollama's
  auto layer estimate sizes to the 30 GiB *system* RAM (not the carveout) and CPU-offloads —
  `gpt-oss:120b` *regresses* 36.8 → 5.7 tok/s by default. Forcing residency restores 100% GPU.
- **Large models → MoE; dense → Q4_K_M.** MoE is measured **bigger *and* faster** (mixtral-q4
  81.2 GiB @ 9.03 tok/s and gpt-oss 60.5 GiB @ ~36.5 tok/s both beat dense-Q8-70B's 70.7 GiB @
  3.0 tok/s). For dense, **Q4_K_M** decodes ~1.7× faster than Q8 (5.1 vs 3.0 tok/s), uses ~30 GiB
  less VRAM, and MMLU-Pro is flat within the 42Q noise.
- **`OLLAMA_NUM_PARALLEL=8` (raised from 4).** Measured knee c4 → c8 and ceiling ~107 →
  **~120–128 tok/s** for 7B concurrency. Run concurrency ≈ 8 for the best throughput/TTFT
  trade-off (TTFT p95 stays ~825 ms at c8; c16 blows out to 8.1 s).
- **llama.cpp (rocm) as the serving backend** — the fastest / lowest-TTFT server on this box.
- **Router semantic cache threshold 0.92 + exact-repeat.**
- **Classifiers pinned to CPU** — GPU offload is blocked on gfx1151; CPU-pinned they add only
  ~8.5 GiB system RAM and never shrink the VRAM carveout the models load into.

## 4. Best configuration — validated A/B (BEST vs DEFAULT) [M]

The §3 levers were also run **head-to-head, end-to-end** on Halo-B (96 GiB carveout, headless,
full vllm-sr stack co-resident, 2026-07-13): **BEST** (`gpt-oss:120b-vram` forced resident +
`OLLAMA_NUM_PARALLEL=8` + semantic cache 0.92/exact-repeat; llama.cpp recommended for TTFT, the
big-model decode measured on ollama here) vs a **naive DEFAULT** (`gpt-oss:120b` auto
layer-estimate, `NUM_PARALLEL=1`, no cache, ollama). Each row is one lever, with the measured
delta and why BEST wins. Data:
[`perf/quant-frontier/bestcfg-halo-b.json`](../perf/quant-frontier/bestcfg-halo-b.json).

| Lever | BEST | DEFAULT | Delta | Why BEST wins |
| --- | --- | --- | --- | --- |
| **Residency** (`-vram`: `num_gpu=999`,`use_mmap=false` vs auto) | **36.6 tok/s @ 100% GPU** (`size_vram/size`=1.0, peak VRAM 61.65 GiB) | auto layer-estimate CPU-offloads — 0.177 tok/s @ 38.3% GPU this run (disk-pressure; see caveat) | **~6.4×** (clean-run reference 36.8 vs 5.7, §11.1) | Auto-estimate sizes GPU layers to the 30 GiB OS-visible system RAM, not the 96 GiB carveout → CPU-offloads a 60 GiB MoE; forcing residency pins 100% on GPU |
| **Concurrency** (`OLLAMA_NUM_PARALLEL=8` vs 1, 7B) | **121.1 tok/s agg @ c8** (ceiling 128.7 @ c16), TTFT p95 826 ms | 43.9 tok/s agg @ c8 (flat ~43), TTFT p95 20,444 ms | **~2.76×** @ c8 (**~2.93×** ceiling), **~25×** better TTFT p95 @ c8 | One decode slot serializes (aggregate flat, TTFT explodes with the queue); 8 slots scale to the LPDDR5X bandwidth plateau, knee c8 |
| **Semantic cache** (0.92 + exact-repeat vs none) | exact-repeat hit ~1–2 ms; semantic-0.92 hit ~0.7–0.9 s | miss ~1.2–1.56 s (full embed+classify+route+decode) | **>100×** on repeats/paraphrases | A hit skips the upstream LLM leg; an exact-repeat pre-routing cache skips embed+classify+routing entirely |
| **Server** (llama.cpp rocm vs ollama) | TTFT ~28 ms | TTFT ~142 ms | **~5×** lower TTFT | llama.cpp is the fastest / lowest-TTFT server measured on gfx1151 |
| **Architecture** (MoE vs dense big model) | `gpt-oss:120b` MoE 36.5 tok/s | dense Q8 70B 3.0 tok/s | **~12×** decode at similar/larger footprint | MoE reads only the active experts per token, so bandwidth-bound decode is far faster than a same-size dense model |
| **Quant** (dense Q4_K_M vs Q8_0) | 5.1 tok/s, ~30 GiB smaller | 3.0 tok/s | **~1.7×** decode, ~30 GiB VRAM saved, quality flat | Fewer bytes/token → faster bandwidth-bound decode; MMLU-Pro flat within the 42Q noise |
| **Per-watt** (resident MoE vs dense 70B-Q4) | 0.366 tok/s/W (36.5 @ 99.9 W) | 0.0381 tok/s/W (5.07 @ 133 W) | **~10×** more energy-efficient per token *and* less power | Socket pins to ~100–133 W; efficiency tracks throughput, so the resident MoE wins ~10× per token |

**Net:** forcing VRAM residency + `NUM_PARALLEL=8` + cache + llama.cpp + MoE turns a naive default
(auto CPU-offload, serialized, ~20 s p95 TTFT) into **36.6 tok/s resident, ~121 tok/s concurrent,
sub-second p95, ~10× the energy efficiency** — same box, correctly configured.

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
| `gpt-oss:120b` | 120B MoE MXFP4 | 60.5 GiB | **~36.5** | — |

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
