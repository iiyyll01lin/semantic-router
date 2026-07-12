# vllm-sr on Strix Halo — feasibility & cost (customer brief)

_Fleet: 2× Ryzen AI Max+ 395 · halo-a (94.1 GiB visible / 32 GiB VRAM carveout) + halo-b (62.4 GiB visible / 64 GiB VRAM carveout) · vllm-sr stack footprint: ~8.6 GiB per box_

## 1. Bottom line

- **It runs today:** router + backend share one box; the router costs ~8.6 GiB of memory and near-zero decode throughput.
- **The real cost is first-token latency:** direct ~158 ms -> through the router ~1436 ms (**+1.3 s**). Two levers now cut it at the source: an **exact-repeat cache** returns identical prompts in **~1–2 ms** (the entire tax gone), and **trimming the two heaviest safety heads** cuts the classify stage by **~56%** (both measured, accuracy unchanged).
- **Feasibility is memory-bound:** the biggest model a box serves = unified memory ÷ quantization (below).
- **The max model moves with the box topology:** Halo-A tops out at `qwen2.5:32b` (70B fails to load); Halo-B, run headless to free its whole 64 GiB VRAM carveout, reaches **`gpt-oss:120b` (120B MoE) @ ~30 tok/s, fully VRAM-resident**.

## 2. Feasibility boundary — largest model each box can serve

**Fleet-safe standard-ladder max (both boxes, safe default): `qwen3:14b`** — the conservative ceiling across the shared tier ladder that every box can serve. This is *distinct* from each box's single-box peak ceiling below, which depends on that box's VRAM carveout and can go much higher.

Measured single-box peak ceilings (full vllm-sr stack co-resident):

| box (VRAM carveout) | largest model | footprint | decode tok/s | verdict |
|---|---|---|---|---|
| halo-a (32 GiB, GUI up) | qwen2.5:32b | 26.7 GiB | 10.7 | **max usable** |
| halo-a (32 GiB, GUI up) | llama3.1:70b | ~48.9 GiB | — | **fails to load** (HTTP 500, GTT spill) |
| halo-b (64 GiB, headless) | qwen2.5:32b | 26.7 GiB | 10.9 | usable |
| halo-b (64 GiB, headless) | llama3.1:70b | 48.2 GiB | 3.6 | usable (VRAM-fit) |
| halo-b (64 GiB, headless) | **gpt-oss:120b (120B MoE)** | **56.6 GiB** | **30.4** | **max usable** |
| halo-b (64 GiB, headless) | llama3.1:70b-instruct-q8_0 | ~69 GiB | 2.1 | unusable (soft CPU-offload) |

_The ceiling is governed by the **BIOS VRAM carveout**, not the OS-visible budget. When a model's weights exceed the carveout the two boxes fail differently: on **Halo-A** the overflow spills to GTT and the load **aborts (HTTP 500)** — a hard fail; on **Halo-B** the overflow is a **soft CPU-offload** (Ollama runs the extra layers on the CPU) that still "runs" but collapses decode below the usable floor. The lever that moved Halo-B's ceiling from 32B to 120B is **going headless** to free the whole 64 GiB carveout — OS-only tuning, BIOS unchanged, fully reproducible._

### Quantization decides the ceiling (76.1 GiB usable after stack + 10% reserve)

| quantization | GiB per 1B params | max params on this box |
|---|---|---|
| Q4 | 0.6 | ~126B |
| Q8 | 1.1 | ~69B |
| fp16 | 2.2 | ~34B |

_Q4 roughly **doubles** the largest model vs fp16 on the same hardware — the practical lever for fitting a bigger model._

_Caveat — asymmetric BIOS._ The two boxes have different VRAM carveouts (Halo-A 32 GiB, Halo-B 64 GiB), so their **model ceilings differ** even though **router overhead is identical** on both. The `gpt-oss:120b` peak on Halo-B additionally requires **headless + `use_mmap=false`** to load VRAM-resident inside client timeouts.

## 3. Latency tax and how the cache removes it

The router adds ~1.3 s to first-token latency (classification + embedding + routing). A **semantic-cache hit** skips that pipeline and the model call entirely:

| threshold | true_hit_rate | false_hit_rate | ttft_miss_ms | ttft_hit_ms |
|---|---|---|---|---|
| 0.50 | 1.00 | 1.00 | 1467 | 692 |
| 0.70 | 1.00 | 0.67 | 1016 | 683 |
| 0.85 | 0.83 | 0.50 | 1073 | 777 |
| 0.92 | 0.83 | 0.00 | 1059 | 729 |
| 0.95 | 0.67 | 0.00 | 1065 | 703 |

_Recommended threshold: **0.92** — the lowest that never serves a wrong cached answer (false-hit = 0), maximising coverage._

**Two upgrades cut the tax at the source (measured, from-source router build):**

| lever | before | after | how |
|---|---|---|---|
| **Exact-repeat cache** | identical prompt still paid ~0.7–0.9 s | **~1–2 ms** | serve byte-identical repeats *before* classification/routing |
| **Head-trim** (drop PII+jailbreak ML heads) | classify stage ~0.72 s | **~0.31 s (−56%)** | remove the two heaviest signal heads; keyword guards remain |

_Both keep routing accuracy at **88.9%** (unchanged). The semantic cache above still helps paraphrases and removes the model-call leg; the exact-repeat cache additionally removes the classification tax for repeats. Head-trim is a **safety-vs-latency trade** — it drops ML PII/jailbreak detection, so re-enable those heads for adversarial or PII-bearing traffic._

## 4. Concurrency boundary

| concurrent streams | aggregate tok/s | scaling vs 1 | TTFT p95 ms |
|---|---|---|---|
| 1 | 42 | 1.00x | 152 |
| 2 | 43 | 1.02x | 3098 |
| 4 | 43 | 1.03x | 8980 |
| 8 | 43 | 1.03x | 20753 |
| 16 | 43 | 1.04x | 41305 |

_Default Ollama serves one stream at a time: total throughput stays flat while first-token latency grows with the queue. Effective capacity ~1 concurrent request per box at full speed; scale with OLLAMA_NUM_PARALLEL or llama.cpp/vLLM._

## 5. Inference-server options (same base model — note the quantization)

| server | quantization | decode tok/s | TTFT ms | status |
|---|---|---|---|---|
| ollama | **Q4_0 (ollama default)** | 43.0 | 142 | measured |
| llamacpp | **Q4_K_M** | 43.2 | 28 | measured |
| lemonade | **Q4_1 (lemonade Qwen3-8B-GGUF)** | 39.8 | 90 | measured |
| vllm | **fp16 (or awq)** | - | - | skipped |

_Quantization differs per server, so decode-rate deltas are **not** apples-to-apples — compare within the same quantization. Quantization also sets the max model (§2)._

## 6. Cost — can it really be cheaper?

**(a) vs cloud API.** After the one-off box cost (~$2500), local tokens are ~$0 marginal. At $0.60 / 1M output tokens, the box pays for itself after ~4.2 billion output tokens.

**(b) vs no routing.** vllm-sr sends easy queries to a small model instead of always the big one; the small tier runs ~4.1x faster than the 32B, so routed traffic is proportionally cheaper per request.

**(c) vs a discrete GPU.** Strix Halo's unified memory holds a 32B that would need a >40 GB discrete card, and Halo-B's 64 GiB carveout serves a 120B MoE that would need a multi-card box; one integrated box replaces a GPU-server tier (lower capex + power). The MoE lever compounds this: `gpt-oss:120b` (~5.1B active params/token) is **both bigger *and* more power-efficient per token than a dense 32B — 0.30 vs 0.093 tok/s/W (~3.2×)** — so the box serves a far larger model without a bigger power bill.

_Marginal per-token cost is ~$0 locally; the levers that make it genuinely cheaper: (1) route down to small models, (2) cache hits remove repeat work, (3) fit via Q4 instead of paying for a bigger card._

