# vllm-sr on Strix Halo — executive one-pager

_2× Ryzen AI Max+ 395 fleet (halo-a + halo-b) · core figures measured 2026-07-12; Halo-B 96 GiB carveout re-test 2026-07-13; best-config matrix (disk-fixed) re-test 2026-07-14; interactive TTFT sweet-spot sweep 2026-07-14 · companion to the [full technical report](customer-report.md)_

**Bottom line: an intelligent LLM router runs on commodity Strix Halo mini-PCs today — it costs almost no throughput and a fixed, cache-removable first-token delay, and the only hard limit is memory.**

## The three things to know

1. **It runs.** The vllm-sr router and the model backend share one box. The router uses **~8.5 GiB** of unified memory and has **~0% impact on decode throughput** (measured across five model tiers, within run-to-run noise).
2. **The real cost is first-token latency — and it's removable.** Routing adds a fixed **~1.4 s** to time-to-first-token (embedding + classification). A semantic-cache hit (recommended threshold **0.92**, zero wrong answers) skips it, and an exact-repeat cache returns identical prompts in **~1–2 ms** — the tax gone.
3. **Feasibility is memory-bound.** The largest model a box can serve is set by its memory carveout, not by the router (below).

## What each box can serve (measured, full stack co-resident)

| Scope | Model | Decode | Note |
|---|---|---|---|
| Fleet-safe standard ladder (safe default) | `qwen3:14b` | — | conservative ceiling every box meets |
| Halo-A peak (32 GiB VRAM, GUI up) | `qwen2.5:32b` | ~10.7 tok/s | 70B **fails to load** (HTTP 500) |
| Halo-B peak (96 GiB VRAM, headless) | **`gpt-oss:120b` (120B MoE)** | **~53 tok/s** (llama.cpp) | VRAM-resident; 70B-Q8 (~70 GiB) also resident; largest measured 141B MoE at **~94.6 GiB** (essentially the full carveout) |

A **120-billion-parameter** model at **~53 tok/s** single-stream (llama.cpp, ROCm) on a single ~$2,500 mini-PC — the MoE activates only ~5B params/token, and Halo-B is run headless with a **96 GiB VRAM carveout** (loaded fully resident: llama.cpp `-ngl 999`, or ollama's `num_gpu`/`use_mmap` override).

**Best config vs naive default (single end-to-end matrix run, Halo-B, `gpt-oss:120b`, 2026-07-14 disk-fixed re-test):** the winning combination — **VRAM-resident (`-ngl 999`) + `--parallel 8` on llama.cpp (ROCm)** — serves the 120B at **95.2 tok/s aggregate across 8 concurrent streams**† (52.9 tok/s single-stream), **VRAM-resident (~59 GiB)**, at **0.641 tok/s/W** — simultaneously the **fastest and most power-efficient** of the eight configurations tested. The **naive default** (plain `gpt-oss:120b`, ollama auto layer-estimate, `NUM_PARALLEL=1`, no override) **failed to decode at all** on this box — auto sizing CPU-offloaded ~⅔ of the model into the ~30 GiB system RAM — so the honest gap is **usable vs unusable**: the difference is configuration, not hardware. (Within the resident config, raising `--parallel` mainly buys **concurrency capacity, not raw throughput**: the MXFP4 120B is memory-bandwidth-bound, so aggregate is nearly flat from `--parallel 1 → 2` (~50 → ~51 tok/s) and only climbs at `--parallel 8` (~79–95 tok/s across 8 streams, run-dependent) — at the cost of multi-second first-token latency. A semantic-cache hit removes the routing first-token tax on repeats, **~1.04 s → ~0.69 s**.)

## Cost / ROI — three levers

- **vs cloud API:** ~$0 marginal cost per token after the one-off **~$2,500/box**. At $0.60 / 1M output tokens the box pays for itself after **~4.2 billion output tokens**.
- **vs no routing:** easy queries route to a small tier that runs **~4.1× faster** than the 32B — cheaper per request on the same hardware.
- **vs a discrete GPU:** one integrated box's **unified memory replaces a >40 GB GPU card**; the 120B MoE is even **more power-efficient per token than a dense 32B — 0.38 (ollama) to 0.64 (llama.cpp) vs 0.093 tok/s/W** (7B is 0.41) — a far larger model with no bigger power bill.

## Recommended settings

**Primary path — llama.cpp (ROCm), model loaded fully VRAM-resident (`-ngl 999`), semantic cache at 0.92.** This is the fastest and most power-efficient stack we measured — and it now serves the flagship 120B too (2026-07-14 disk-fixed re-test; the earlier "won't load" was a disk artifact, see caveats). **Size `--parallel` to your expected concurrency — bigger is not a free default** (the 120B is memory-bandwidth-bound, so extra slots add concurrency capacity, not throughput per user):

| Scenario | Setting | Serves `gpt-oss:120b` at | First token (TTFT) | Best for |
|---|---|---|---|---|
| **Low-latency (1 user)** | `--parallel 1` | **52 tok/s** single-stream | **~85 ms** | one latency-critical user: chat, IDE completion |
| **Interactive sweet spot** ★ | `--parallel 2` | ~24 tok/s per stream · **51 tok/s** aggregate | **~0.2 s (p50) · ~1.0 s (p95)** | a few concurrent interactive users (holds a ~2 s first-token budget) |
| Moderate concurrency | `--parallel 4` | ~16 tok/s per stream · 58 tok/s aggregate | ~0.34 s (p50) · **~3.2 s (p95)** | more streams, but first token now exceeds ~2 s |
| **High-throughput / batch** | `--parallel 8` | **~79–95 tok/s** across 8 streams† | ~85 ms for 1 user; **~3.7 s (p95)** at 8 concurrent | batch or many users where first-token latency doesn't matter |

† The 8-stream aggregate is run-to-run / co-residency variable: the dedicated best-config matrix measured **95.2 tok/s**, a later co-resident sweep **79.1 tok/s** at the same point — read it as **~80–95 tok/s**, not one hard number. That sweep also found single-stream decode *drops* as slots rise (~52 tok/s at `--parallel 1` → ~30–33 at `--parallel 4`/`8`, even for a lone user), so over-provisioning slots slows the single-user path.

- **Semantic-cache threshold 0.92** — maximum cache coverage with zero false hits.
- **Match `--parallel` to your concurrency — do not over-provision.** The co-resident sweep shows the 120B is memory-bandwidth-bound, so extra slots buy **capacity to serve more users, not more speed per user**, and over-provisioning actually **slows the single-user path**: lone-request decode falls from **~52 tok/s (`--parallel 1`) to ~30–33 tok/s (`--parallel 4`/`8`)**. Use **`--parallel 1`** for one latency-critical user, **`--parallel 2`** for a few concurrent interactive users (first token still ~1 s at p95, at ~the same ~51 tok/s aggregate), and **`--parallel 8`** only for batch/throughput where multi-second first tokens are acceptable (also the most power-efficient, 0.641 tok/s/W). Note `--parallel 1` serializes under load — first-token p95 balloons to **~17.8 s** if eight users hit its single slot.
- **Large models:** run the box **headless** and force full residency (`-ngl 999`); on the 96 GiB carveout, headless alone is not enough.
- **Alternative / fallback — ollama.** If you standardize on ollama instead, set **`OLLAMA_NUM_PARALLEL=8`** and load large models with the **`num_gpu=999` + `use_mmap=false`** override (the `-vram` variants). On the flagship 120B this measured **65.7 tok/s aggregate / 36.6 tok/s single-stream / 0.414 tok/s/W** — usable, but slower and less efficient than llama.cpp (95.2† / 52.9 / 0.641). _(The old "~128 tok/s" figure was a 7B-on-ollama concurrency number, not the flagship — the 120B figures above are the ones to quote.)_

## Honest caveats

- **Asymmetric BIOS carveout** (Halo-A 32 GiB vs Halo-B 96 GiB) gives the two boxes **different model ceilings** — but **router overhead is identical** on both.
- **vLLM is skip-with-reason on gfx1151** (a genuine kernel gap, `invalid device function`); the practical path is **llama.cpp (ROCm)**, which serves everything measured **including the MXFP4 `gpt-oss:120b`**. The disk-fixed 2026-07-14 re-test confirmed llama.cpp loads and is the *fastest* server for the flagship — an earlier "won't load on gfx1151" result was a **disk/download artifact (since corrected), not a hardware limitation**.
- **`--parallel 8` maximizes aggregate throughput, not latency:** at 8 concurrent streams the 120B's first-token time rises to **~3.7 s (p95)** (vs ~85 ms for a lone user) as prompts queue for prefill, and per-stream decode drops to ~12 tok/s. Prefer **`--parallel 1`** for a single latency-critical user, **`--parallel 2`** for a few concurrent interactive users (the sweet spot — first token still ~1 s at p95), and **`--parallel 8`** only for batch / many-user throughput (where it is also the most power-efficient).
- On Halo-B's **96 GiB carveout**, servers size GPU layers to the (now ~30 GiB) system RAM by default, so big models need explicit full-resident placement — llama.cpp **`-ngl 999`** or ollama **`num_gpu=999` + `use_mmap=false`** (the `-vram` variants) — to stay VRAM-resident; headless alone is not enough.

_Full measured detail, methods, and reproduction commands: [customer-report.md](customer-report.md)._
