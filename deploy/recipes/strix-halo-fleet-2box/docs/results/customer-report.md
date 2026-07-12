# vllm-sr on Strix Halo — feasibility & cost (customer brief)

_Box: halo-a · unified memory: 94.1 GiB · vllm-sr stack footprint: 8.6 GiB_

## 1. Bottom line

- **It runs today:** router + backend share one box; the router costs ~8.6 GiB of memory and near-zero decode throughput.
- **The real cost is first-token latency:** direct ~158 ms -> through the router ~1436 ms (**+1.3 s**), which a semantic-cache hit removes entirely.
- **Feasibility is memory-bound:** the biggest model a box serves = unified memory ÷ quantization (below).

## 2. Feasibility boundary — largest model each box can serve

Fleet-safe max usable model: **qwen3:14b**.

| model | est. footprint (GiB) | projected mem use | ran on | decode tok/s | verdict |
|---|---|---|---|---|---|
| qwen2.5:32b | 19.2 | 32.7% | halo-b | 10.6 | usable (measured) |
| llama3.1:70b | 42.0 | 56.9% | halo-b | 3.8 | usable (measured) |

_Models whose projected memory use exceeds 85% of one box are offloaded to Halo-B; when Halo-B is unprovisioned/unreachable the oversized model is skipped-with-reason (never attributed to a box it did not run on). A model that fits neither box is the hard boundary._

### Quantization decides the ceiling (76.1 GiB usable after stack + 10% reserve)

| quantization | GiB per 1B params | max params on this box |
|---|---|---|
| Q4 | 0.6 | ~126B |
| Q8 | 1.1 | ~69B |
| fp16 | 2.2 | ~34B |

_Q4 roughly **doubles** the largest model vs fp16 on the same hardware — the practical lever for fitting a bigger model._

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

**(c) vs a discrete GPU.** Strix Halo's 94.1 GiB unified memory holds a 32B that would need a >40 GB discrete card; one integrated box replaces a GPU-server tier (lower capex + power).

_Marginal per-token cost is ~$0 locally; the levers that make it genuinely cheaper: (1) route down to small models, (2) cache hits remove repeat work, (3) fit via Q4 instead of paying for a bigger card._

