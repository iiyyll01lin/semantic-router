# vllm-sr on Strix Halo — executive one-pager

_2× Ryzen AI Max+ 395 fleet (halo-a + halo-b) · core figures measured 2026-07-12; Halo-B 96 GiB carveout re-test 2026-07-13 · companion to the [full technical report](customer-report.md)_

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
| Halo-B peak (96 GiB VRAM, headless) | **`gpt-oss:120b` (120B MoE)** | **~37 tok/s** | VRAM-resident; 70B-Q8 (~70 GiB) also resident |

A **120-billion-parameter** model at ~37 tok/s on a single ~$2,500 mini-PC — the MoE activates only ~5B params/token, and Halo-B is run headless with a **96 GiB VRAM carveout** (big models loaded via the `num_gpu`/`use_mmap` override).

## Cost / ROI — three levers

- **vs cloud API:** ~$0 marginal cost per token after the one-off **~$2,500/box**. At $0.60 / 1M output tokens the box pays for itself after **~4.2 billion output tokens**.
- **vs no routing:** easy queries route to a small tier that runs **~4.1× faster** than the 32B — cheaper per request on the same hardware.
- **vs a discrete GPU:** one integrated box's **unified memory replaces a >40 GB GPU card**; the 120B MoE is even **more power-efficient per token than a dense 32B — 0.30 vs 0.093 tok/s/W** (7B is 0.41) — a far larger model with no bigger power bill.

## Recommended settings

- Semantic-cache threshold **0.92** — max coverage with zero false hits.
- **`OLLAMA_NUM_PARALLEL=4`** → aggregate **~107 tok/s** (~2.5× the serialized default; throughput knee at 4 concurrent streams).
- Inference server: **llama.cpp (ROCm)** — the fastest measured (lowest TTFT ~28 ms).
- Large models: run the box **headless**, and on a large VRAM carveout load them via the **`num_gpu`/`use_mmap=false`** override (the `-vram` variants) — headless alone is not enough.

## Honest caveats

- **Asymmetric BIOS carveout** (Halo-A 32 GiB vs Halo-B 96 GiB) gives the two boxes **different model ceilings** — but **router overhead is identical** on both.
- **vLLM is skip-with-reason on gfx1151** (kernel gap, `invalid device function`); the practical path is llama.cpp (ROCm).
- On Halo-B's **96 GiB carveout**, Ollama sizes GPU layers to the (now ~30 GiB) system RAM, so big models need the **`num_gpu=999` + `use_mmap=false`** override (the `-vram` variants) to stay VRAM-resident — headless alone is not enough.

_Full measured detail, methods, and reproduction commands: [customer-report.md](customer-report.md)._
