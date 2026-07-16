# vllm-sr on Strix Halo — feasibility & cost (customer brief)

*Fleet: 2× Ryzen AI Max+ 395 · halo-a (94.1 GiB visible / 32 GiB VRAM carveout) + halo-b (~30 GiB visible / 96 GiB VRAM carveout, raised from 64) · vllm-sr stack footprint: ~8.5 GiB per box*

## Executive summary

**For the decision-maker.** An intelligent LLM router (vllm-sr) runs on the 2-box Strix Halo fleet **today**: it costs **~8.5 GiB** of unified memory, **~0%** decode throughput, and a fixed **~1.4 s** first-token latency that a semantic cache (threshold **0.92**) or an exact-repeat cache (**~1–2 ms**) removes. The only hard limit is memory.

**What the fleet serves (measured, full stack co-resident):**

- **Fleet-safe default:** `qwen3:14b` across both boxes.
- **Halo-A peak:** `qwen2.5:32b` (~10.7 tok/s); a 70B fails to load (HTTP 500).
- **Halo-B local/default:** Gemma 4 26B MoE is now the recommended default family:
  balanced `gemma4:26b-a4b-it-q8_0` (**44.6 tok/s**, **25.3 GiB**, **71.4%**), throughput/demo
  `gemma4:26b` Q4_K_M (**58.4 tok/s**, **21.6 GiB**, **69.0%**), and compact/fast
  `gemma4:26b-a4b-it-qat` (**65.0 tok/s**, **13.8 GiB**, **64.3%**).
- **Halo-B capacity/reference (headless, 96 GiB carveout):** `gpt-oss:120b` **(120B MoE) VRAM-resident
  at ~36.5 tok/s**, **60.5 GiB**, **64.3%**, **0.382 tok/s/W**; at 96 GiB even a **70B-Q8 (~70 GiB) is
  VRAM-resident**, and the largest model measured resident is a **141B MoE (`mixtral:8x22b`-Q5) at
  ~94.6 GiB** / 7.80 tok/s — **essentially the full 96 GiB carveout** (~1.4 GiB shy) (needs the
  `num_gpu`/`use_mmap` override — see caveats/§2).

**Recommended settings:** semantic-cache threshold **0.92** · default/demo model **Gemma 4 26B MoE**
(`gemma4:26b-a4b-it-q8_0` balanced, `gemma4:26b` Q4_K_M for throughput/demo) · inference server
**llama.cpp (ROCm)** where available · keep large capacity/reference models **headless** and loaded via
full-residency controls (`-ngl 999`, or ollama `num_gpu`/`use_mmap=false` / `-vram` variants) — headless
alone is not enough.

**Default conclusion:** `gpt-oss:120b` is no longer the best/default config. It remains the 120B
capacity/reference and big-MoE baseline, but Gemma 4 26B MoE is faster, smaller, higher-quality for the
balanced default, and more efficient for everyday local serving.

**Operational carveout conclusion:** use **64 GiB VRAM** for Gemma 4 26B default serving (the Gemma
rungs peak at only 13.8–25.3 GiB and benefit from ~62 GiB OS-visible system RAM). Keep **96 GiB
VRAM** as a capacity/frontier-test mode for `gpt-oss:120b`, 70B-Q8, and mixtral-q5, where full
residency past the 64 GiB ceiling is the point.

**Candidate sweep confirmation (2026-07-15):** the broad Halo-B P0 + capped P1/P2 sweep did not find a replacement. `qwen3-coder:30b` is the speed standout (**71.0 tok/s**) but only **54.8%** on the 42Q quality slice; `qwen3-next:80b` is **49.6 tok/s / 61.9%**; `qwen3.6:27b` matches Gemma Q4 quality (**69.0%**) but is much slower (**13.5 tok/s**) and inefficient (**0.082 tok/s/W**). See `perf/quant-frontier/candidate-summary-halo-b.md` for measured rows and skips.

**Why it's cheaper:** ~~$0 marginal cost per token after **~~$2,500/box** (payback ~~4.2B output tokens vs cloud) · routing to small tiers is **~~4.1×** faster than the 32B · unified memory replaces a **>40 GB GPU card**, Gemma 4 26B MoE reaches **0.40–0.48 tok/s/W**, and the 120B MoE capacity reference is still more power-efficient per token than a dense 32B (**0.382 vs 0.093 tok/s/W**; 7B is 0.41).

**Caveats:** the two boxes have **asymmetric BIOS carveouts** (32 vs 96 GiB) → different model ceilings but **identical router overhead**; **vLLM is skip-with-reason on gfx1151**; on the 96 GiB carveout Ollama sizes GPU layers to the (now smaller ~30 GiB) system RAM, so big models need the `num_gpu=999` **+** `use_mmap=false` override (the `-vram` variants) to stay VRAM-resident — headless alone is not enough.

> **Companion artifact (same numbers, same recommendations):** the sendable executive [one-pager](customer-onepager.md). Sections **§1–§6 below are the technical body** — full measurements, methods, and reproduction commands.

---



## 1. Bottom line

- **It runs today:** router + backend share one box; the router costs ~8.5 GiB of memory and near-zero decode throughput.
- **The real cost is first-token latency:** direct ~~156 ms -> through the router ~1560 ms (**+1.4 s**). An **exact-repeat cache** (live) returns identical prompts in **~~1–2 ms** — the entire tax gone. A second, **optional** lever — **trimming the two heaviest safety heads** — cuts the classify stage by a measured **~56%**, but it drops PII/jailbreak detection, so the live box **keeps those heads ON** (accuracy unchanged either way).
- **Feasibility is memory-bound:** the biggest model a box serves = unified memory ÷ quantization (below).
- **The default model and max model are now separate:** Halo-B's best local/default family is Gemma 4
  26B MoE; the max/capacity story is still `gpt-oss:120b` **(120B MoE) VRAM-resident at ~36.5 tok/s**,
  plus a **70B-Q8 (~70 GiB)** resident rung and a **141B MoE resident at ~94.6 GiB** (largest measured,
  7.80 tok/s — essentially the full carveout), using the `num_gpu`/`use_mmap` override (§2).



## 2. Feasibility boundary — largest model each box can serve

**Fleet-safe standard-ladder max (both boxes, safe default):** `qwen3:14b` — the conservative ceiling across the shared tier ladder that every box can serve. This is *distinct* from each box's single-box peak ceiling below, which depends on that box's VRAM carveout and can go much higher.

Measured single-box peak ceilings (full vllm-sr stack co-resident):


| box (VRAM carveout)       | largest model               | footprint    | decode tok/s | verdict                                 |
| ------------------------- | --------------------------- | ------------ | ------------ | --------------------------------------- |
| halo-a (32 GiB, GUI up)   | qwen2.5:32b                 | 26.7 GiB     | 10.7         | **max usable**                          |
| halo-a (32 GiB, GUI up)   | llama3.1:70b                | ~48.9 GiB    | —            | **fails to load** (HTTP 500, GTT spill) |
| halo-b (64 GiB, headless) | qwen2.5:32b                 | 26.7 GiB     | 10.9         | usable                                  |
| halo-b (64 GiB, headless) | llama3.1:70b                | 48.2 GiB     | 3.6          | usable (VRAM-fit)                       |
| halo-b (64 GiB, headless) | **gpt-oss:120b (120B MoE)** | **56.6 GiB** | **30.4**     | **max usable**                          |
| halo-b (64 GiB, headless) | llama3.1:70b-instruct-q8_0  | ~69 GiB      | 2.1          | unusable (soft CPU-offload)             |


*The ceiling is governed by the **BIOS VRAM carveout**, not the OS-visible budget. When a model's weights exceed the carveout the two boxes fail differently: on **Halo-A** the overflow spills to GTT and the load **aborts (HTTP 500)** — a hard fail; on **Halo-B** the overflow is a **soft CPU-offload** (Ollama runs the extra layers on the CPU) that still "runs" but collapses decode below the usable floor. Going **headless** first moved Halo-B's ceiling 32B → 120B; raising the carveout further (below) then makes even a 70B-Q8 VRAM-resident.*

***96 GiB re-test (current Halo-B config).** The BIOS carveout was later raised **64 → 96 GiB**. With the* `num_gpu`*/*`use_mmap` *override,* `gpt-oss:120b` *is VRAM-resident at **~36.5 tok/s** (up from 30.4), and the **70B-Q8 (~70 GiB) that was unusable at 64 GiB is now fully VRAM-resident** (~~3 tok/s, LPDDR5X-bandwidth-bound); the largest real model measured resident is a **141B MoE (mixtral-8x22b-Q5) at 94.59 GiB / 7.80 tok/s** — essentially the full 96 GiB carveout (~1.4 GiB shy), so residency is now measured to the carveout edge rather than extrapolated. This is the capacity/reference story; the local/default story is Gemma 4 26B MoE. Trade-off: system RAM drops to ~30 GiB and Ollama's default auto-estimate CPU-offloads big models unless overridden. Detail:* `perf-report.md` *[§11.1](../perf-report.md) and* `[halo-b-maxmodel.md](../halo-b-maxmodel.md)`*.*

***Operational note.** For Gemma 4 26B default serving, **64 GiB** is the recommended Halo-B
carveout: Gemma fits comfortably, Ollama behaves more naturally, and the OS/container side gets
~62 GiB visible system RAM. Switch to **96 GiB** only when demonstrating or measuring the
capacity/reference rungs above.*

### Quantization decides the ceiling — bound by the VRAM carveout, not OS-visible RAM

The largest model is set by the **BIOS VRAM carveout** a model's weights must fit (overflow spills/offloads and becomes unusable), **not** the OS-visible system RAM. For **Halo-B's 96 GiB carveout** (~90 GiB usable for weights after runtime buffers):


| quantization | GiB per 1B params | max params in a 96 GiB carveout |
| ------------ | ----------------- | ------------------------------- |
| Q4           | ~0.6              | ~150B                           |
| Q8           | ~1.1              | ~82B                            |
| fp16         | ~2.2              | ~41B                            |


*Q4 roughly **doubles** the largest model vs fp16 on the same carveout — the practical lever for fitting a bigger model. These are weights-fit ceilings: a dense model near the top is VRAM-resident but LPDDR5X-bandwidth-bound, while an MoE (few active params) stays fast at the same size.*

*Caveat — asymmetric BIOS.* The two boxes have different VRAM carveouts (**Halo-A 32 GiB, Halo-B 96 GiB**), so their **model ceilings differ** even though **router overhead is identical** on both. Halo-A's 32 GiB carveout caps it at `qwen2.5:32b` in practice (a 70B overflows and aborts); big models on Halo-B's 96 GiB carveout need **headless +** `num_gpu`**/**`use_mmap=false` (the `-vram` variants) to load VRAM-resident._

### Current local/default model guidance

| Use case | Model | Decode | Footprint | MMLU-Pro (42Q) | Why |
| -------- | ----- | ------ | --------- | -------------- | --- |
| **Balanced default** | `gemma4:26b-a4b-it-q8_0` | **44.6 tok/s** | 25.3 GiB | **71.4% (30/42)** | best quality/speed/VRAM balance |
| **Throughput/demo default** | `gemma4:26b` Q4_K_M | **58.4 tok/s** | 21.6 GiB | **69.0% (29/42)** | best interactive feel |
| **Compact/fast edge** | `gemma4:26b-a4b-it-qat` | **65.0 tok/s** | 13.8 GiB | **64.3% (27/42)** | fastest and smallest, lower quality |
| **Quality-only local** | `gemma4:31b-it-qat` | 12.3 tok/s | 18.5 GiB | **78.6% (33/42)** | best local quality, too slow for default |
| **120B capacity/reference** | `gpt-oss:120b` | ~36.5 tok/s | 60.5 GiB | 64.3% (27/42) | big-MoE baseline, not the default |

## 3. Latency tax and how the cache removes it

The router adds ~1.4 s to first-token latency (classification + embedding + routing). A **semantic-cache hit** skips that pipeline and the model call entirely:


| threshold | true_hit_rate | false_hit_rate | ttft_miss_ms | ttft_hit_ms |
| --------- | ------------- | -------------- | ------------ | ----------- |
| 0.50      | 1.00          | 1.00           | 1467         | 692         |
| 0.70      | 1.00          | 0.67           | 1016         | 683         |
| 0.85      | 0.83          | 0.50           | 1073         | 777         |
| 0.92      | 0.83          | 0.00           | 1059         | 729         |
| 0.95      | 0.67          | 0.00           | 1065         | 703         |


*Recommended threshold: **0.92** — the lowest that never serves a wrong cached answer (false-hit = 0), maximising coverage.*

**Two upgrades cut the tax at the source (measured, from-source router build):**


| lever                                       | before                                 | after              | how                                                          |
| ------------------------------------------- | -------------------------------------- | ------------------ | ------------------------------------------------------------ |
| **Exact-repeat cache**                      | identical prompt still paid ~0.7–0.9 s | **~1–2 ms**        | serve byte-identical repeats *before* classification/routing |
| **Head-trim** (drop PII+jailbreak ML heads) | classify stage ~0.72 s                 | **~0.31 s (−56%)** | remove the two heaviest signal heads; keyword guards remain  |


*Both keep routing accuracy at **88.9%** (unchanged). The **exact-repeat cache is live**; the semantic cache still helps paraphrases and removes the model-call leg. Head-trim is a **safety-vs-latency trade** — it drops ML PII/jailbreak detection — so it is **NOT enabled on the live box** (both safety heads stay on); it is documented here as an optional lever for latency-critical, low-risk deployments only.*

## 4. Concurrency boundary

Measured on Halo-A (`qwen2.5:7b`), sweeping concurrent streams under two backend configs — Ollama's default (single decode slot) and the same container with `OLLAMA_NUM_PARALLEL=4`:


| concurrent streams | serialized agg tok/s | serialized TTFT p95 ms | `OLLAMA_NUM_PARALLEL=4` agg tok/s | p4 TTFT p95 ms |
| ------------------ | -------------------- | ---------------------- | --------------------------------- | -------------- |
| 1                  | 42                   | 152                    | 42                                | 156            |
| 2                  | 43                   | 3098                   | 66                                | 397            |
| 4                  | 43                   | 8980                   | 100                               | 436            |
| 8                  | 43                   | 20753                  | 107                               | 4919           |
| 16                 | 43                   | 41305                  | 107                               | 14452          |


*Default Ollama serves one stream at a time: aggregate throughput stays flat (~~43 tok/s) while first-token latency grows with the queue. With*~~ `OLLAMA_NUM_PARALLEL=4` ~~*throughput scales to **~~107 tok/s (~2.5×)**, with the knee at **c=4** (already ~2.3× serialized while TTFT p95 stays low); beyond c=4 you buy little throughput while latency balloons. A **p8 re-test raises the 7B plateau to ~128 tok/s with the knee at c8** (TTFT p95 ~825 ms at c8), so `OLLAMA_NUM_PARALLEL=8` is the recommended 7B-class concurrency operating point; for larger models, size slots to the workload rather than treating p8 as a universal default.*

## 5. Inference-server options (same base model — note the quantization)


| server   | quantization                      | decode tok/s | TTFT ms | status                     |
| -------- | --------------------------------- | ------------ | ------- | -------------------------- |
| ollama   | **Q4_0 (ollama default)**         | 43.0         | 142     | measured                   |
| llamacpp | **Q4_K_M**                        | 43.2         | 28      | measured                   |
| lemonade | **Q4_1 (lemonade Qwen3-8B-GGUF)** | 39.8         | 90      | measured                   |
| vllm     | **fp16 (or awq)**                 | -            | -       | skip-with-reason (gfx1151) |


*Quantization differs per server, so decode-rate deltas are **not** apples-to-apples — compare within the same quantization. Quantization also sets the max model (§2).*

## 6. Cost — can it really be cheaper?

**(a) vs cloud API.** After the one-off box cost (~$2500), local tokens are ~$0 marginal. At $0.60 / 1M output tokens, the box pays for itself after ~4.2 billion output tokens.

**(b) vs no routing.** vllm-sr sends easy queries to a small model instead of always the big one; the small tier runs ~4.1x faster than the 32B, so routed traffic is proportionally cheaper per request.

**(c) vs a discrete GPU.** Strix Halo's unified memory holds a 32B that would need a >40 GB discrete card, and Halo-B's 96 GiB carveout can host a 120B MoE capacity reference that would otherwise need a multi-card box; one integrated box replaces a GPU-server tier (lower capex + power). The MoE lever compounds this: Gemma 4 26B MoE is the better local/default point (**0.40–0.48 tok/s/W**), while `gpt-oss:120b` (~~5.1B active params/token) still beats dense 32B on energy/token (**0.382 vs 0.093 tok/s/W**) as the 120B reference.

*Marginal per-token cost is ~$0 locally; the levers that make it genuinely cheaper: (1) route down to small models, (2) cache hits remove repeat work, (3) fit via Q4 instead of paying for a bigger card.*