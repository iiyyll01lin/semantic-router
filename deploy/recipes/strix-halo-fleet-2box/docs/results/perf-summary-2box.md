# Fleet perf summary (report-run-2box-20260712-153904)

> **RETIRED — SUPERSEDED (read first).** This file is **retired** for Halo-B's
> capacity / max-model numbers. It is the **2026-07-12** two-box fleet run, captured when
> **Halo-B was still at its 64 GiB VRAM carveout** (hence `halo-b 62.44 GiB` visible below);
> Halo-B was **later raised to 96 GiB** and re-tested. Do **not** cite the `halo-b` capacity
> row here. For Halo-B's **current** max-model ceiling + quantization frontier, use
> [`../perf-report.md`](../perf-report.md) §11.1–§11.2 and
> [`../halo-b-maxmodel.md`](../halo-b-maxmodel.md) instead. Only the Test 1 / Test 2 findings
> below (co-location overhead, server comparison, fleet-safe `qwen3:14b`) are
> **box-carveout-independent** and remain valid.

## Test 1 — vllm-sr co-location overhead

Fleet-safe max usable model: **qwen3:14b**  ·  mean stack RAM footprint: **8.68 GiB**

| box | unified mem (GiB) | stack RAM (GiB) | max usable | first unusable |
|---|---|---|---|---|
| halo-a | 94.06 | 8.56 | qwen3:14b | None |
| halo-b | 62.44 | 8.8 | qwen3:14b | None |

| model tier | mean drop % (contention) | mean drop % (end-to-end) | direct TTFT ms | router TTFT ms |
|---|---|---|---|---|
| llama3.2:3b | 1.5 | 2.0 | 158 | 1467 |
| qwen2.5:14b | 0.0 | 0.1 | 160 | 1468 |
| qwen2.5:7b | 1.1 | 1.2 | 141 | 1450 |
| qwen3:14b | -1.7 | -2.7 | 162 | 1575 |

## Test 2 — inference-server comparison (bundled with vllm-sr)

### halo-a (base qwen2.5-7b, fastest: llamacpp)

| server | status | decode tok/s | TTFT ms | vs ollama | router overhead % | quant |
|---|---|---|---|---|---|---|
| ollama | measured | 43.0 | 142 | +0.0% | - | Q4_0 (ollama default) |
| llamacpp | measured | 43.2 | 28 | +0.4% | - | Q4_K_M |
| lemonade | measured | 39.8 | 90 | -7.3% | - | Q4_1 (lemonade Qwen3-8B-GGUF) |
| vllm | skipped | - | - | - | - | fp16 (or awq) |

### halo-b (base qwen2.5-7b, fastest: ollama)

| server | status | decode tok/s | TTFT ms | vs ollama | router overhead % | quant |
|---|---|---|---|---|---|---|
| ollama | measured | 44.8 | 143 | +0.0% | - | Q4_0 (ollama default) |

## Router-TTFT experiments (halo-a, custom from-source image)

These land on top of the fleet numbers above; both boxes share identical router
overhead, so the halo-a measurement transfers. See `report-data.md` (Custom
from-source router image) and `docs/perf-report.md` §7.5 for detail.

- **Exact-repeat cache (landed):** identical prompt served in **~1–2 ms** (was
  ~0.7–0.9 s), skipping classify+route; accuracy unchanged 88.9%.
- **Head-trim (measured option, reverted on the live box):** `signal.evaluation`
  **716 → 313 ms (−56%)** by dropping the PII+jailbreak heads; accuracy unchanged
  88.9%. Reverted to keep full safety on the live router — kept as an opt-in lever.
- **INT8/OpenVINO (C) and GPU-offload (D):** documented blockers, not landed
  (OpenVINO not integrated; GPU flip SIGSEGVs on gfx1151). Router stays CPU.

