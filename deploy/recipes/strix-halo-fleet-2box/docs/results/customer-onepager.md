# vllm-sr on Strix Halo — executive one-pager

_2× Ryzen AI Max+ 395 fleet (halo-a + halo-b) · core figures measured 2026-07-12; Halo-B 96 GiB carveout re-test 2026-07-13; best-config matrix (disk-fixed) re-test 2026-07-14 · companion to the [full technical report](customer-report.md)_

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

**Best config vs naive default (single end-to-end matrix run, Halo-B, `gpt-oss:120b`, 2026-07-14 disk-fixed re-test):** the winning combination — **VRAM-resident (`-ngl 999`) + `--parallel 8` on llama.cpp (ROCm)** — serves the 120B at **95.2 tok/s aggregate across 8 concurrent streams** (52.9 tok/s single-stream), **VRAM-resident (~59 GiB)**, at **0.641 tok/s/W** — simultaneously the **fastest and most power-efficient** of the eight configurations tested. The **naive default** (plain `gpt-oss:120b`, ollama auto layer-estimate, `NUM_PARALLEL=1`, no override) **failed to decode at all** on this box — auto sizing CPU-offloaded ~⅔ of the model into the ~30 GiB system RAM — so the honest gap is **usable vs unusable**: the difference is configuration, not hardware. (Within the resident config, `--parallel 8` over `1` adds **~1.88×** aggregate throughput at the cost of first-token latency; a semantic-cache hit removes the routing first-token tax on repeats, **~1.04 s → ~0.69 s**.)

## Cost / ROI — three levers

- **vs cloud API:** ~$0 marginal cost per token after the one-off **~$2,500/box**. At $0.60 / 1M output tokens the box pays for itself after **~4.2 billion output tokens**.
- **vs no routing:** easy queries route to a small tier that runs **~4.1× faster** than the 32B — cheaper per request on the same hardware.
- **vs a discrete GPU:** one integrated box's **unified memory replaces a >40 GB GPU card**; the 120B MoE is even **more power-efficient per token than a dense 32B — 0.38 (ollama) to 0.64 (llama.cpp) vs 0.093 tok/s/W** (7B is 0.41) — a far larger model with no bigger power bill.

## Recommended settings

**Primary path — llama.cpp (ROCm), model loaded fully VRAM-resident (`-ngl 999`), semantic cache at 0.92.** This is the fastest and most power-efficient stack we measured — and it now serves the flagship 120B too (2026-07-14 disk-fixed re-test; the earlier "won't load" was a disk artifact, see caveats). Set `--parallel` to match your workload:

| Scenario | Setting | Serves `gpt-oss:120b` at | First token (TTFT) | Best for |
|---|---|---|---|---|
| **Low-latency (single user)** | `--parallel 1` | **52.7 tok/s** single-stream | **~85 ms** | chat, IDE completion, one user at a time |
| **High-throughput (many users)** — recommended default | `--parallel 8` | **95.2 tok/s** across 8 streams (still 52.9 single-stream) | ~85 ms for one user; ~3.0 s (p95) under 8 concurrent | multi-user, batch, a shared backend service |
| _sweet-spot_ | `--parallel 2` / `4` | _measurement pending_ | _measurement pending_ | balanced concurrency (being measured now) |

- **Semantic-cache threshold 0.92** — maximum cache coverage with zero false hits.
- **`--parallel 8` is the safe default:** it costs a lone user almost nothing — the same ~52.9 tok/s single-stream and the same ~85 ms first token — it only splits the KV cache eight ways, slightly shortening the maximum context per request, and it is also the **most power-efficient** setting (0.641 tok/s/W). Choose `--parallel 1` only for a strict single-user path that needs the largest possible context — it slows sharply the moment a second request arrives (first-token p95 ~17.8 s at 8 concurrent streams).
- **Large models:** run the box **headless** and force full residency (`-ngl 999`); on the 96 GiB carveout, headless alone is not enough.
- **Alternative / fallback — ollama.** If you standardize on ollama instead, set **`OLLAMA_NUM_PARALLEL=8`** and load large models with the **`num_gpu=999` + `use_mmap=false`** override (the `-vram` variants). On the flagship 120B this measured **65.7 tok/s aggregate / 36.6 tok/s single-stream / 0.414 tok/s/W** — usable, but slower and less efficient than llama.cpp (95.2 / 52.9 / 0.641). _(The old "~128 tok/s" figure was a 7B-on-ollama concurrency number, not the flagship — the 120B figures above are the ones to quote.)_

## Honest caveats

- **Asymmetric BIOS carveout** (Halo-A 32 GiB vs Halo-B 96 GiB) gives the two boxes **different model ceilings** — but **router overhead is identical** on both.
- **vLLM is skip-with-reason on gfx1151** (a genuine kernel gap, `invalid device function`); the practical path is **llama.cpp (ROCm)**, which serves everything measured **including the MXFP4 `gpt-oss:120b`**. The disk-fixed 2026-07-14 re-test confirmed llama.cpp loads and is the *fastest* server for the flagship — an earlier "won't load on gfx1151" result was a **disk/download artifact (since corrected), not a hardware limitation**.
- **The winning `--parallel 8` config maximizes aggregate throughput, not latency:** at 8 concurrent streams the 120B's first-token time rises (llama.cpp TTFT p95 ~3.0 s @ c8, vs ~85 ms single-stream) as prompts queue for prefill. For a single-user, latency-sensitive path prefer `--parallel 1`; for many concurrent users prefer `8` (which on llama.cpp is also the most power-efficient).
- On Halo-B's **96 GiB carveout**, servers size GPU layers to the (now ~30 GiB) system RAM by default, so big models need explicit full-resident placement — llama.cpp **`-ngl 999`** or ollama **`num_gpu=999` + `use_mmap=false`** (the `-vram` variants) — to stay VRAM-resident; headless alone is not enough.

_Full measured detail, methods, and reproduction commands: [customer-report.md](customer-report.md)._
