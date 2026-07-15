# Customer demo runbook — vllm-sr on Strix Halo (2-box fleet)

One page, follow-along. The **Dashboard (`:8700`) is the single entry point** — Grafana, Jaeger,
Prometheus and the fleet-sim are all embedded inside it, so you rarely leave it. Every number
below is measured; sources are linked in §5. Target audience knobs are in §4.

## 0. Pre-flight (2 min before you open)

1. **Stack up on Halo-B.** From the box (or over SSH) confirm the hybrid stack is healthy:

```bash
vllm-sr status   # expect the 9 vllm-sr-* containers (router, envoy, dashboard,
                 # grafana, prometheus, jaeger, sim, redis, postgres) + ollama, all up
```

2. **Open the tunnel** from your laptop (Dashboard only — everything else is embedded):

```bash
ssh -N -L 8700:localhost:8700 test001@10.96.31.132
# optional, only if you want the services standalone (all embedded in the Dashboard):
#   -L 3000:localhost:3000     Grafana      -L 16686:localhost:16686  Jaeger
#   -L 9090:localhost:9090     Prometheus   -L 8810:localhost:8810    fleet-sim
```

3. **Log in.** Dashboard `http://localhost:8700` → **`yingylin@amd.com` / `aupaup123`**.
   Grafana / Jaeger / Prometheus / fleet-sim need no separate login (Grafana is anonymous;
   the rest open from inside the Dashboard's own pages).

## 1. The demo arc (~25 min) — visual first, Dashboard as the one door

| # · time | Open (route) | Say (one-liner) | Expect on screen | Fallback |
| --- | --- | --- | --- | --- |
| 1 · 30s | `/status` | "The whole hybrid stack is live — not a screenshot." | Router / Envoy / dashboard green; 5 tier models ready; auto-refresh 10s. | `vllm-sr status` in a terminal; re-run REHEARSAL Gate E. |
| 2 · ~5m **(core)** | `/playground/fullscreen` | "The router picks the model per request: cheap stays local, hard escalates, unsafe is blocked." | Easy → `qwen/qwen3.5-rocm` (~$0); hard reasoning → `openai/gpt5.4` + reasoning on; PII & jailbreak → `security_guard`, `x-vsr-fast-response: true`. HeaderReveal shows MODEL / DECISION / SIGNALS. | `python deploy/recipes/strix-halo-poc/smoke_test.py` runs the same 4 cases from the CLI. |
| 3 · ~3m **(safest)** | `/topology` | "Here's *why* it routes — semantics drive the decision, no model call." | Dry-run a query; signal → decision → model path highlights; `security_guard` (priority 300) wins. | This step never hits a model — it *is* the safe fallback; otherwise narrate from `/config`. |
| 4 · ~3m | `/monitoring` | "Cost drops even when everything runs local — here's the local-served ratio and latency." | Embedded Grafana: cost-reduction %, model distribution, TTFT/TPOT P95, cache hits. | Send a few Playground requests to populate; else quote the one-pager numbers (§2). |
| 5 · ~2m | `/tracing` | "Routing overhead is ~0% of the request." | Embedded Jaeger `service=vllm-sr`: classify → decide → upstream spans per request. | Re-send one Playground request (always-on sampling); else quote ~0% decode / +1.4s TTFT. |
| 6 · ~3m **(business)** | `/fleet-sim/runs` | "We prove the fleet's TCO *before* we buy it." | optimize / simulate run → **36.1% fewer GPUs (23 vs 36; $445K vs $697K/yr)**. | Show the last completed run in `/fleet-sim`; else quote 36.1% (§2). |
| 7 · ~5–8m **(CLI)** | `demo-fleet.sh` | "One central edit → both boxes converge to a byte-exact signed hash, with audit + rollback." | Edit one rule → halo-a & halo-b hot-reload and converge → central audit log → one-edit rollback; HW-verified hash `a78aebc5fd5f`. | Offline: `python deploy/recipes/strix-halo-fleet-2box/verify_local.py` (8/8) shows the same converge / drift / rollback / audit logic. |
| 8 · ~3m | one-pager + canvas | "The best local default is Gemma 4 26B MoE; the same box still proves a 120B capacity story." | Gemma 26B Q4 @ 58.4 tok/s / 69.0% or Q8 @ 44.6 tok/s / 71.4%; candidate sweep confirmed no Qwen/DeepSeek/Mistral/Phi replacement; 120B reference @ ~36.5 tok/s; capacity 94.59 / 96 GiB. | Read from `results/customer-onepager.md` + canvas `strix-halo-hardware-limits`. |

> Fleet step (7) needs a live 2-box fleet — run `deploy/recipes/strix-halo-fleet-2box/deploy-fleet-2box.sh`
> first. No fleet? Use the offline `verify_local.py` fallback and quote the hash.

## 2. Key-numbers cheat sheet

- **Routing accuracy:** 61/62 probes = **98.4%** (decision coverage 13/14).
- **Router tax:** **~0%** decode-throughput impact + a fixed **~1.4 s** TTFT; a cache hit removes it (exact-repeat **~1–2 ms**).
- **Default/demo model (Halo-B):** Gemma 4 26B MoE — balanced `gemma4:26b-a4b-it-q8_0` at **44.6 tok/s / 71.4%**, or throughput `gemma4:26b` Q4 at **58.4 tok/s / 69.0%**. The 2026-07-15 candidate sweep confirms this: `qwen3-coder:30b` is faster but 54.8%, `qwen3-next:80b` is 61.9%, and `qwen3.6:27b` is 69.0% but only 13.5 tok/s.
- **Capacity/reference model (Halo-B, 96 GiB, headless):** `gpt-oss:120b` (120B MoE) @ **~36.5 tok/s**, **60.5 GiB**, **64.3%**; largest resident footprint remains **94.59 / 96 GiB** via `mixtral:8x22b-q5_K_M`.
- **Fleet TCO (simulated from real trace):** **36.1%** fewer GPUs — **23 vs 36**, **$445K vs $697K/yr**.
- **Fleet governance:** both boxes converge to one signed config hash **`a78aebc5fd5f`**.

## 3. Offline / no-GPU fallbacks (if the box is unreachable)

```bash
# End-to-end routing + security on CPU (llm-katan echo backend, no model download):
bash deploy/recipes/strix-halo-poc/cpu-smoke.sh

# Fleet config control-plane (converge / edit-once / drift / rollback / tamper / audit): 8/8
python deploy/recipes/strix-halo-fleet-2box/verify_local.py

# Perf harness pipeline (probe / rewrite / aggregate): 7/7
python deploy/recipes/strix-halo-fleet-2box/perf/verify_perf_local.py
```

## 4. Adjust for the audience

- **Technical:** add the three hardware walls (capacity / bandwidth / power) and the quantization
  frontier from `hardware-limits.md`; dwell on `/topology`, `/tracing`, and the signed-hash fleet
  governance (`demo-fleet.sh`).
- **Business:** press `/monitoring` (cost down) and `/fleet-sim` (TCO: 36.1%, $445K vs $697K/yr),
  then close on the one-pager — a ~$2,500 box, ~$0 marginal cost/token.

## 5. Links

- [04-dashboard-tour.md](../../../../docs/poc/04-dashboard-tour.md) — 11-step, click-by-click Dashboard walkthrough.
- [strix-halo-poc/REHEARSAL.md](../../strix-halo-poc/REHEARSAL.md) — Go/No-Go rehearsal (gates A–G).
- [results/customer-onepager.md](results/customer-onepager.md) — executive one-pager (numbers above).
- [hardware-limits.md](hardware-limits.md) — the three walls + quantization frontier.
- Canvases (live in the Cursor canvases dir, outside the repo): **`strix-halo-hardware-limits`** (quant frontier, capacity, MoE vs dense, per-watt) and **`strix-halo-customer-report`** (customer-facing report). Only the former is also referenced from a repo file (`hardware-limits.md`).

---

_Footnote — SSH target: `test001@10.96.31.132` is confirmed working this session; the repo's older committed value was `test001@10.96.28.126` (swap it if the box IP changes)._
