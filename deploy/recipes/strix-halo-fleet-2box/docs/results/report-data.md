# Strix Halo perf — collected report data (aup-HP-Z2-Mini-G1a-Workstation-Desktop-PC)

# Fleet perf summary (report-run-20260712-123240)

## Test 1 — vllm-sr co-location overhead

Fleet-safe max usable model: **qwen3:14b**  ·  mean stack RAM footprint: **8.56 GiB**

| box | unified mem (GiB) | stack RAM (GiB) | max usable | first unusable |
|---|---|---|---|---|
| halo-a | 94.06 | 8.56 | qwen3:14b | None |

| model tier | mean drop % (contention) | mean drop % (end-to-end) | direct TTFT ms | router TTFT ms |
|---|---|---|---|---|
| llama3.2:3b | 3.5 | 3.8 | 158 | 1436 |
| qwen2.5:14b | 0.6 | 0.6 | 163 | 1535 |
| qwen2.5:7b | 2.7 | 2.7 | 142 | 1434 |
| qwen3:14b | -2.9 | -3.9 | 162 | 1595 |

## Test 2 — inference-server comparison (bundled with vllm-sr)

Fleet consensus fastest server: **llamacpp**

### halo-a (base qwen2.5-7b, fastest: llamacpp)

| server | status | decode tok/s | TTFT ms | vs ollama | router overhead % | quant |
|---|---|---|---|---|---|---|
| ollama | measured | 43.0 | 142 | +0.0% | - | Q4_0 (ollama default) |
| llamacpp | measured | 43.2 | 28 | +0.4% | - | Q4_K_M |
| lemonade | measured | 39.8 | 90 | -7.3% | - | Q4_1 (lemonade Qwen3-8B-GGUF) |
| vllm | skipped | - | - | - | - | fp16 (or awq) |

## Concurrency sweep (qwen2.5:7b)

| concurrency | aggregate decode tok/s | scaling vs c1 | TTFT mean ms | TTFT p95 ms | success |
|---|---|---|---|---|---|
| 1 | 42 | 1.00x | 152 | 152 | 100% |
| 2 | 43 | 1.02x | 1631 | 3098 | 100% |
| 4 | 43 | 1.03x | 4576 | 8980 | 100% |
| 8 | 43 | 1.03x | 10471 | 20753 | 100% |
| 16 | 43 | 1.04x | 22235 | 41305 | 100% |

_Ollama default serializes (single slot): aggregate throughput stays flat while TTFT grows with the queue -- concurrency queues, it does not scale. Raise OLLAMA_NUM_PARALLEL, or use llama.cpp/vLLM, for true parallel throughput._

## Semantic-cache threshold sweep

| threshold | true_hit_rate | false_hit_rate | ttft_miss_ms | ttft_hit_ms |
|---|---|---|---|---|
| 0.50 | 1.00 | 1.00 | 1467 | 692 |
| 0.70 | 1.00 | 0.67 | 1016 | 683 |
| 0.85 | 0.83 | 0.50 | 1073 | 777 |
| 0.92 | 0.83 | 0.00 | 1059 | 729 |
| 0.95 | 0.67 | 0.00 | 1065 | 703 |

_Recommendation: the lowest threshold that keeps `false_hit_rate` at 0._

---
Narrative + interpretation: `docs/perf-report.md` — replace its **[P]** rows with the tables above.
