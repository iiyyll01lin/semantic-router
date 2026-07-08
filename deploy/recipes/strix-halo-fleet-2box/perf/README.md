# Strix Halo perf benchmarks — co-location overhead & inference-server comparison

Two measurement harnesses for the [strix-halo-fleet-2box](../README.md) topology,
answering questions the existing scripts do not (`run-bench.sh` measures routing
*latency* overhead; `topology-bench.sh` measures rps; `fleet_metrics.py` measures
config convergence — none measure resource footprint, token throughput, or
non-Ollama backends):

- **Test 1 — vllm-sr co-location overhead** ([`overhead-bench.sh`](overhead-bench.sh)):
  how much the vllm-sr stack **occupies**, how much **throughput drops** for the
  same model when the router is co-resident, and which model **spec becomes
  unusable** on this box.
- **Test 2 — inference-server comparison** ([`server-bench.sh`](server-bench.sh)):
  the performance difference of **different inference servers** (Ollama, llama.cpp,
  Lemonade, vLLM) bundled with vllm-sr on the same box + base model.

Both roll up **fleet-wide** via [`perf_metrics.py`](perf_metrics.py) into
`perf-metrics.json` + `perf-summary.md`.

> **Written up with measured data:** see the
> [performance report](../docs/perf-report.md) — co-location footprint, the
> decode-vs-TTFT story, the OOM ceiling, concurrency, semantic-cache tuning,
> mmBERT levers, Lemonade install, and the gfx1151/vLLM workaround.

## Why Strix Halo makes this interesting

Strix Halo (Ryzen AI Max+ 395, gfx1151) has **unified LPDDR5X memory** shared by
CPU and GPU, and the router's ONNX classifiers are **CPU-pinned**
(`VLLM_SR_AMD_PRESERVE_CPU=1`). So the router competes with the GPU for the *same
memory bandwidth* used for token generation: the throughput drop is
**bandwidth/contention-driven, not VRAM-driven**, and "max usable model" = what
fits in *unified memory minus the stack footprint*. The sampler auto-detects the
unified-memory budget and watches **GTT** (the tell-tale of spilling past the GPU
carveout).

## Components

| File | Role |
| --- | --- |
| [`tokrate_probe.py`](tokrate_probe.py) | Direct-backend tokens/sec + TTFT. Ollama (`/api/generate`, server timings) and OpenAI-compatible (`/chat/completions`, streamed) dialects. Stdlib only. |
| [`resource_sampler.py`](resource_sampler.py) | Samples `rocm-smi`/`amd-smi` (VRAM/GTT/busy), `/proc/meminfo` (unified budget), and `docker stats` (per-container). `snapshot`/`start`/`stop`/`summarize`. Degrades gracefully when a tool is absent. |
| [`overhead-bench.sh`](overhead-bench.sh) | Test 1 driver: baseline (stack down) vs co-located (stack up) per tier + ascending OOM sweep. |
| [`server-bench.sh`](server-bench.sh) | Test 2 driver: brings up each server, measures direct (and optional through-router) throughput; skips a server that won't run with a recorded reason. |
| [`repoint_backend.py`](repoint_backend.py) | In-place (same-inode) rewrite of one model card's backend, for the optional through-router path. |
| [`perf_metrics.py`](perf_metrics.py) | Aggregates per-box `overhead-*`/`server-*` JSON into a fleet record + markdown. |
| [`run-perf-fleet.sh`](run-perf-fleet.sh) | Turnkey fleet-wide runner (Halo-A local + Halo-B over SSH) → one bundle. |
| [`collect-report-data.sh`](collect-report-data.sh) | **One-shot report data collection** — runs steps [1]–[7] (verify → lemonade → Test 1+2 → concurrency → cache) into one bundle and stitches a filled `report-data.md`. |
| [`cache-sweep.sh`](cache-sweep.sh) | Semantic-cache tuning sweep: for each `similarity_threshold` records true/false hit-rate + TTFT (hit vs miss) to CSV, via in-place config rewrite + hot-reload. |
| [`install-lemonade.sh`](install-lemonade.sh) | Idempotent per-box provisioner for the Lemonade server (`lemonade-sdk`, port 13305 `/api/v1`) — the practical vLLM+rocm workaround for gfx1151. |
| [`verify_perf_local.py`](verify_perf_local.py) | **Offline CI-grade verifier (7/7)** — mock backends exercise the real probe / in-place rewrite / aggregate code paths, no ROCm/Docker/gateway. |

## Quick start

```bash
# Verify the WHOLE harness offline first -- no hardware, no Docker (7/7):
python3 verify_perf_local.py          # or: SELFTEST=1 bash run-perf-fleet.sh
```

Then, on a gateway box with the stack up:

```bash
# EASIEST -- one shot: verify -> lemonade -> Test 1+2 -> concurrency -> cache -> report-data.md
bash collect-report-data.sh

# Test 1 only
bash overhead-bench.sh

# Test 2 only (add SERVER_BENCH_ROUTER=1 for the through-router path)
SERVERS="ollama llamacpp" bash server-bench.sh

# Both, fleet-wide, aggregated into one bundle
bash run-perf-fleet.sh

# Or fold into the one-shot deploy run:
HALO_A_MODE=gateway HALO_B_MODE=gateway PERF_BENCH=1 bash ../run-all-2box.sh
```

Key knobs (see each script's header for the full list): `TIERS`,
`OVERSIZED_TAGS`, `OOM_MIN_TPS` (Test 1); `SERVERS`, `<SRV>_MODEL`, `<SRV>_UP_CMD`,
`<SRV>_QUANT`, `SERVER_BENCH_ROUTER` (Test 2); `RUNS`, `CONCURRENCY`,
`MAX_TOKENS`, `PROMPT_TOKENS` (both).

## Honest caveats

- **Quant parity (Test 2).** Servers load *different* quantizations of the same
  base model; each row records its `quant`. Treat cross-server deltas as "this
  server + this quant on this box", or pin identical artifacts via `<SRV>_MODEL` /
  `<SRV>_UP_CMD` for strict parity.
- **vLLM on gfx1151 is experimental.** If it won't build/serve it is *skipped*
  with a reason, not failed.
- **OOM on unified memory** may surface as GTT spill (very slow) rather than a
  hard failure; a decode-rate collapse below `OOM_MIN_TPS` is reported as
  `unusable(slow-spill)` alongside hard `unusable(load-fail)`.
- The default llama.cpp / Lemonade / vLLM bring-up commands are best-effort
  defaults — override `<SRV>_UP_CMD` / `<SRV>_IMAGE` / `<SRV>_HF` for your box.
