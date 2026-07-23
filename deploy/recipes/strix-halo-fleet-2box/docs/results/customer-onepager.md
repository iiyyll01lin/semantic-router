# vllm-sr on Strix Halo — executive one-pager

_2× Ryzen AI Max+ 395 fleet (halo-a + halo-b) · core figures measured 2026-07-12; Halo-B 96 GiB carveout re-test 2026-07-13; best-config matrix (disk-fixed) re-test 2026-07-14; interactive TTFT sweet-spot sweep 2026-07-14; separate `demo-002` agentic-context addendum 2026-07-22/23 · companion to the [full technical report](customer-report.md)_

**Bottom line: an intelligent LLM router runs on commodity Strix Halo mini-PCs today — Gemma 4 26B MoE is now the best local/default family, while `gpt-oss:120b` remains the 120B capacity/reference story.**

**2026-07-22/23 agentic-context addendum (`demo-002`, separate scope):** a
pinned official vLLM v0.25.1 BF16 stack measured working on gfx1151. Direct
Ollama Gemma Q8 was configured/loaded at 65,536 tokens and tested inputs through
65,152: **17 cells / 174 requests, 174/174 HTTP and exact prompt usage, 150/174
markers, 7/17 fully green**. Capacity is **PARTIAL PASS**, quality **NOT
ACHIEVED**, and reliability **NOT RUN**. See the
[focused agentic brief](agentic-context-customer-onepager-20260722.md); these
figures do not replace the dated Halo-A/B baseline below.

## The three things to know

1. **It runs.** The vllm-sr router and the model backend share one box. The router uses **~8.5 GiB** of unified memory and has **~0% impact on decode throughput** (measured across five model tiers, within run-to-run noise).
2. **The real cost is first-token latency — and it's removable.** Routing adds a fixed **~1.4 s** to time-to-first-token (embedding + classification). A semantic-cache hit (recommended threshold **0.92**, zero wrong answers) skips it, and an exact-repeat cache returns identical prompts in **~1–2 ms** — the tax gone.
3. **Feasibility is memory-bound.** The largest model a box can serve is set by its memory carveout, not by the router (below).

## What each box can serve (measured, full stack co-resident)

| Scope | Model | Decode | Note |
|---|---|---|---|
| Fleet-safe standard ladder (safe default) | `qwen3:14b` | — | conservative ceiling every box meets |
| Halo-A peak (32 GiB VRAM, GUI up) | `qwen2.5:32b` | ~10.7 tok/s | 70B **fails to load** (HTTP 500) |
| Halo-B balanced local/default | **`gemma4:26b-a4b-it-q8_0`** | **44.6 tok/s** | **71.4% (30/42)** MMLU-Pro, **25.3 GiB**, **0.418 tok/s/W** |
| Halo-B throughput/demo default | **`gemma4:26b` Q4_K_M** | **58.4 tok/s** | **69.0% (29/42)**, **21.6 GiB**, **0.481 tok/s/W**; best-feeling demo path |
| Halo-B compact/fast edge | `gemma4:26b-a4b-it-qat` | **65.0 tok/s** | **13.8 GiB**, **64.3% (27/42)**; fastest and compact, but lower quality |
| Halo-B capacity/reference | **`gpt-oss:120b` (120B MoE)** | **~36.5 tok/s** | **60.5 GiB**, **64.3% (27/42)**, **0.382 tok/s/W**; important big-MoE baseline, not the default |

A single ~$2,500 mini-PC now has two complementary stories: the **Gemma 4 26B MoE default** is faster, higher quality, smaller, and more efficient than the 120B reference for everyday local serving; the **120B capacity story** proves the same box can still hold a very large MoE fully resident when run headless with a **96 GiB VRAM carveout** and explicit full-resident placement.

**Default recommendation:** use `gemma4:26b-a4b-it-q8_0` when the demo needs the best balance of quality and responsiveness; switch to `gemma4:26b` Q4_K_M when the audience should feel maximum throughput; use `gemma4:26b-a4b-it-qat` only when compactness or raw speed matters more than quality. `gemma4:31b-it-qat` is the best local quality rung (**78.6%**, 12.3 tok/s), but it is too slow to be the default unless the demo is quality-only.

**Candidate sweep confirmation (2026-07-15):** P0 Qwen candidates did not change this recommendation. `qwen3-coder:30b` is faster (**71.0 tok/s**) but low quality (**54.8%**), `qwen3-next:80b` is **49.6 tok/s / 61.9%**, and `qwen3.6:27b` reaches **69.0%** but only **13.5 tok/s** / **0.082 tok/s/W**. Lower-priority DeepSeek/Mistral/Phi/Falcon checks also did not produce a better default.

## Operating profiles — the model depends on the workload

There is no single "best" model — pick by workload. All figures measured on Halo-B (profile-specific
follow-ups in the [full report](customer-report.md) and `perf/quant-frontier/profiles-summary-halo-b.md`):

| Workload | Run this | Measured | Carveout |
|---|---|---|---:|
| **Single-turn** chat/QA | `gemma4:26b-a4b-it-q8_0` (or `gemma4:26b` Q4 for fastest feel) | 44.6 tok/s, 71.4% · Q4 58.4 tok/s, 69.0% | 64 GiB |
| **Agentic / tool-calling** | `gemma4:26b-a4b-it-q8_0` (Qwen3-Coder only if the tool-call bench earns it) | Qwen3-Coder 71.0 tok/s but 54.8% generic | 64 GiB |
| **Multiagent / concurrent** | `gemma4:26b` Q4 (throughput) or Q8 (quality), `OLLAMA_NUM_PARALLEL=8` | 7B plateau ~120–128 tok/s, knee c8 | 64 GiB |
| **Quality-only** | `gemma4:31b-it-qat` | 78.6% best local quality, 12.3 tok/s | 64 GiB |
| **Capacity demo** | `gpt-oss:120b` (explicit by-name, never auto-routed) | ~36.5 tok/s, 64.3%, 60.5 GiB | **96 GiB** |

**Everyday serving uses the 64 GiB carveout** (Gemma rungs are 13.8–25.3 GiB); reserve **96 GiB** only for the capacity demo.

## Cost / ROI — three levers

- **vs cloud API:** ~$0 marginal cost per token after the one-off **~$2,500/box**. At $0.60 / 1M output tokens the box pays for itself after **~4.2 billion output tokens**.
- **vs no routing:** easy queries route to a small tier that runs **~4.1× faster** than the 32B — cheaper per request on the same hardware.
- **vs a discrete GPU:** one integrated box's **unified memory replaces a >40 GB GPU card**; Gemma 4 26B MoE reaches **0.40–0.48 tok/s/W**, and the 120B MoE capacity reference is still **0.382 tok/s/W** — far above dense 32B (**0.093 tok/s/W**) with no bigger power bill.

## Recommended settings

**Primary local/demo path — Gemma 4 26B MoE + semantic cache at 0.92.** Pick the tag by demo goal:

| Scenario | Setting | Decode / quality | Best for |
|---|---|---|---|
| **Balanced default** ★ | `gemma4:26b-a4b-it-q8_0` | **44.6 tok/s**, **71.4%**, 25.3 GiB | default customer demo and local serving |
| **Throughput/demo default** | `gemma4:26b` Q4_K_M | **58.4 tok/s**, **69.0%**, 21.6 GiB | best-feeling interactive throughput |
| **Compact/fast edge** | `gemma4:26b-a4b-it-qat` | **65.0 tok/s**, **64.3%**, 13.8 GiB | footprint-constrained edge use |
| **Quality-only local** | `gemma4:31b-it-qat` | **12.3 tok/s**, **78.6%**, 18.5 GiB | quality-first demos where speed is secondary |

**Operational VRAM carveout:** for this Gemma-default path, prefer **64 GiB VRAM / ~62 GiB
system RAM**. The Gemma 26B rungs peak at only **13.8–25.3 GiB**, so 96 GiB is unnecessary for
normal serving and leaves too little OS-visible RAM. Use **96 GiB** only for the capacity/reference
story below (`gpt-oss:120b`, 70B-Q8, mixtral-q5), where models exceed the 64 GiB resident ceiling.

**120B capacity/reference path — llama.cpp (ROCm), model loaded fully VRAM-resident (`-ngl 999`), semantic cache at 0.92.** Use this when the point is "the box can host a 120B MoE", not as the default demo model. Size `--parallel` to expected concurrency — bigger is not a free default:

| Scenario | Setting | Serves `gpt-oss:120b` at | First token (TTFT) | Best for |
|---|---|---|---|---|
| **Low-latency (1 user)** | `--parallel 1` | **52 tok/s** single-stream | **~85 ms** | one latency-critical user: chat, IDE completion |
| **Interactive sweet spot** ★ | `--parallel 2` | ~24 tok/s per stream · **51 tok/s** aggregate | **~0.2 s (p50) · ~1.0 s (p95)** | a few concurrent interactive users (holds a ~2 s first-token budget) |
| Moderate concurrency | `--parallel 4` | ~16 tok/s per stream · 58 tok/s aggregate | ~0.34 s (p50) · **~3.2 s (p95)** | more streams, but first token now exceeds ~2 s |
| **High-throughput / batch** | `--parallel 8` | **~79–95 tok/s** across 8 streams† | ~85 ms for 1 user; **~3.7 s (p95)** at 8 concurrent | batch or many users where first-token latency doesn't matter |

† The 8-stream aggregate is run-to-run / co-residency variable: the dedicated best-config matrix measured **95.2 tok/s**, a later co-resident sweep **79.1 tok/s** at the same point — read it as **~80–95 tok/s**, not one hard number. That sweep also found single-stream decode _drops_ as slots rise (~52 tok/s at `--parallel 1` → ~30–33 at `--parallel 4`/`8`, even for a lone user), so over-provisioning slots slows the single-user path.

- **Semantic-cache threshold 0.92** — maximum cache coverage with zero false hits.
- **Do not call `gpt-oss:120b` the best/default config anymore.** It is the capacity/reference and big-MoE baseline; Gemma 4 26B MoE is the local/default family.
- **Match `--parallel` to your concurrency — do not over-provision.** The co-resident sweep shows the 120B is memory-bandwidth-bound, so extra slots buy **capacity to serve more users, not more speed per user**, and over-provisioning actually **slows the single-user path**: lone-request decode falls from **~52 tok/s (`--parallel 1`) to ~30–33 tok/s (`--parallel 4`/`8`)**. Use **`--parallel 1`** for one latency-critical user, **`--parallel 2`** for a few concurrent interactive users (first token still ~1 s at p95, at ~the same ~51 tok/s aggregate), and **`--parallel 8`** only for batch/throughput where multi-second first tokens are acceptable (also the most power-efficient, 0.641 tok/s/W). Note `--parallel 1` serializes under load — first-token p95 balloons to **~17.8 s** if eight users hit its single slot.
- **Large models:** run the box **headless** and force full residency (`-ngl 999`); on the 96 GiB carveout, headless alone is not enough.
- **Alternative / fallback — ollama for the 120B reference.** If you standardize on ollama instead, set **`OLLAMA_NUM_PARALLEL=8`** and load large models with the **`num_gpu=999` + `use_mmap=false`** override (the `-vram` variants). On the 120B reference this measured **65.7 tok/s aggregate / 36.6 tok/s single-stream / 0.414 tok/s/W** — usable, but slower and less efficient than llama.cpp (95.2† / 52.9 / 0.641). _(The old "~128 tok/s" figure was a 7B-on-ollama concurrency number, not the 120B reference — the 120B figures above are the ones to quote.)_

## Honest caveats

- **Asymmetric BIOS carveout** (Halo-A 32 GiB vs Halo-B 96 GiB) gives the two boxes **different model ceilings** — but **router overhead is identical** on both.
- The historical `rocm/vllm-dev` parity image failed on gfx1151 with `invalid device function`, but a separately pinned official vLLM v0.25.1 BF16 stack later measured working. This is exact-stack evidence, not blanket support or parity with the GGUF rows. **llama.cpp (ROCm)** remains the measured practical path for the MXFP4 `gpt-oss:120b`; the disk-fixed 2026-07-14 re-test confirmed it is the fastest server for that reference model.
- **`--parallel 8` maximizes aggregate throughput, not latency:** at 8 concurrent streams the 120B's first-token time rises to **~3.7 s (p95)** (vs ~85 ms for a lone user) as prompts queue for prefill, and per-stream decode drops to ~12 tok/s. Prefer **`--parallel 1`** for a single latency-critical user, **`--parallel 2`** for a few concurrent interactive users (the sweet spot — first token still ~1 s at p95), and **`--parallel 8`** only for batch / many-user throughput (where it is also the most power-efficient).
- On Halo-B's **96 GiB carveout**, servers size GPU layers to the (now ~30 GiB) system RAM by default, so big models need explicit full-resident placement — llama.cpp **`-ngl 999`** or ollama **`num_gpu=999` + `use_mmap=false`** (the `-vram` variants) — to stay VRAM-resident; headless alone is not enough.

_Full measured detail, methods, and reproduction commands: [customer-report.md](customer-report.md)._
