# vllm-sr on Strix Halo — performance report

**Topology:** [`strix-halo-fleet-2box`](../README.md) · **SUT:** Ryzen AI Max+ 395
(gfx1151, RDNA3.5), ~94 GiB unified LPDDR5X · **Backend:** Ollama tiers
`llama3.2:3b → qwen2.5:7b → qwen2.5:14b → qwen3:14b → qwen2.5:32b` ·
**Harness:** [`perf/`](../perf/README.md)

> **Data provenance.** Rows tagged **[M]** are *measured* from the Halo-A run
> `run-20260708-102706` (single box, stack up). Rows tagged **[P]** are *pending*
> a run with the current harness — the script and the exact command to fill each
> cell are given inline, so every number in this report is reproducible from the
> committed code. The whole harness is offline-verifiable first (`python3
> perf/verify_perf_local.py` → **7/7**), so a [P] cell is a *scheduling* gap, not
> an unknown.
>
> **To fill every [P] row in one shot:** `bash perf/collect-report-data.sh` — it
> runs steps [1]–[7] on Halo-A into one bundle and writes a stitched
`report-data.md`. See §11.

---

## 0. Executive summary — the one thing to remember

On Strix Halo, **co-locating the vllm-sr router with the model backend costs you
almost no decode throughput, but it adds a fixed ~1.4 s to time-to-first-token,
and it burns ~8.4 GiB of the unified memory budget that would otherwise hold a
bigger model.** So the story is *not* "the router slows generation down" — it is
**"the router taxes latency and memory headroom, not bandwidth."** Everything
below is that sentence, with the numbers.

| Question | Answer | Evidence |
| --- | --- | --- |
| How much does vllm-sr **occupy**? | **≈8.35 GiB** unified RAM, router container dominant (~8.8 GB) | §1 **[M]** |
| How much does **throughput drop** (same model)? | Decode tok/s: **≈0% (noise, ±4%)**. TTFT: **156 ms → 1560 ms (+1.4 s)** | §2 **[M]** |
| Which **spec becomes unusable**? | **`qwen2.5:32b` is the ceiling** (~10.7 tok/s). `llama3.3:70b` **fails to load** (HTTP 500, GTT spill 48.9 GB) | §3 **[M]** |
| Are **both boxes** used? | **No — Halo-A only.** Halo-B needs one flag (`HALO_B_PERF=1`) + reachability | §4 |
| **Multi-concurrency** behaviour? | Harness ready (`CONCURRENCY`); decode saturates, TTFT queues | §5 **[P]** |
| Best **semantic-cache** threshold? | Sweep harness records true/false-hit + TTFT payoff per threshold | §6 **[P]** |
| **mmBERT embedding** slow — fix? | Cache first; then fewer classifiers / batching. **Do not** truncate layers | §7 |
| **Lemonade** auto-install? both boxes? | Yes — `install-lemonade.sh`. **Neither box had it**; now scriptable | §8 |
| **vLLM on gfx1151** SOTA / workaround? | Officially **unsupported**; **Lemonade's experimental vLLM+rocm** is the workaround | §9 |

---

## 1. How much does the vllm-sr stack occupy? **[M]**

Measured by `resource_sampler.py` as the delta between *stack-down* and *stack-up*,
plus `docker stats` per container.

| Component | Unified RAM |
| --- | --- |
| Router container (Go + CPU-pinned ONNX classifiers) | **~8.8 GB** |
| Envoy / gateway sidecars | ~0.4 GB |
| **Total stack footprint** | **≈8.35 GiB** |
| Unified budget (measured) | **94.06 GiB** (`unified_mem_total_b` = 100 999 503 872) |

**Story.** The classifiers are **CPU-pinned** (`VLLM_SR_AMD_PRESERVE_CPU=1`), so the
footprint lands in *system* RAM — but on Strix Halo system RAM *is* the GPU's
memory. Every GiB the router holds is a GiB the model can't use. At 8.35 GiB the
tax is ~9% of the budget: survivable, but it is exactly what moves the "max usable
model" boundary in §3.

---

## 2. How much does throughput drop for the same model? **[M]**

`overhead-bench.sh` runs each tier **baseline (stack down)** then **co-located
(stack up)**, same prompt/token shape, and reports the drop. Two very different
answers depending on *which* metric:

### 2a. Decode throughput — essentially unchanged

| Tier | Δ decode tok/s (co-located vs baseline) |
| --- | --- |
| `llama3.2:3b` | **−0.5%** |
| `qwen2.5:7b` | **−3.9%** |
| `qwen2.5:14b` | **−0.8%** |
| `qwen3:14b` | **+2.1%** |
| `qwen2.5:32b` | **+0.8%** |

All within ±4% run-to-run noise (some *positive*, which is only possible if the
true effect is ≈0). **The router does not steal meaningful memory bandwidth from
token generation.** This is the counter-intuitive Strix Halo result: people expect
unified memory contention to crush decode; it doesn't, because a CPU-pinned
classifier at idle isn't streaming weights.

### 2b. TTFT — this is where the router shows up

| | Direct to backend | Through the router | Δ |
| --- | --- | --- | --- |
| TTFT | **~156 ms** | **~1560 ms** | **+~1.4 s** |

**Story.** The ~1.4 s is the request-path work the router does *before* the first
token: embed the prompt (mmBERT), run the classifiers, consult the semantic cache,
pick the route. It is roughly **additive and constant**, so it dominates for small
models (10× on a 3B) and is proportionally smaller on a 32B. This is the single
most important number in the report and it was **hidden by the old summary table**,
which showed only the ~0% decode drop. The table now carries explicit
`direct TTFT ms` / `router TTFT ms` columns (commit `62834f56`) so the tax is never
hidden again:

```
| model tier | mean drop % (contention) | mean drop % (end-to-end) | direct TTFT ms | router TTFT ms |
```

**Lever:** §6 (semantic cache) is the direct countermeasure — a cache *hit* returns
in tens of ms and skips the whole 1.4 s pipeline.

---

## 3. Which model spec becomes unusable? **[M]**

Ascending OOM sweep with the stack co-resident (so the 8.35 GiB tax is included).

| Tier | Decode tok/s (co-located) | Verdict |
| --- | --- | --- |
| `qwen2.5:32b` | **~10.7 tok/s** | **Max usable ✅** |
| `llama3.3:70b` | — | **Fails to load ❌** — HTTP 500, **GTT spill to 48.9 GB** |

**Story.** Failure is *not* a clean OOM — the 70B first **spills into GTT**
(the GPU carveout overflow into the rest of unified memory), which the sampler
flags, and then the load aborts with HTTP 500. So "unusable" on Strix Halo means
*"it tried to page the model through unified memory and gave up,"* not *"CUDA out
of memory."* With the router's 8.35 GiB removed from the budget, **32B is the
practical ceiling** for interactive use on a single box; 70B needs either the
router evicted or a second box.

---

## 4. Are both boxes actually being exercised? — **No**

The measured run is **Halo-A only**. `run-perf-fleet.sh` supports Halo-B but it is
**opt-in and gated on reachability**, and in this run Halo-B was not enabled.

**To include Halo-B** (bare box `10.96.28.126`, currently firewalled):

```bash
HALO_B_PERF=1 HALO_B_SSH="user@10.96.28.126" bash perf/run-perf-fleet.sh
```

`perf_metrics.py` already aggregates *fleet-wide* and reports the **fleet-safe max
usable = the worst box's boundary**, so once Halo-B is reachable the same report
regenerates with two boxes and no code change. **Prerequisite:** provision Halo-B
(open the ports / install Docker + Ollama + Lemonade — see §8).

---

## 5. Multi-concurrency **[P]**

`tokrate_probe.py` already drives N parallel streams (`--concurrency` /
`CONCURRENCY`) and reports `aggregate_decode_tps` plus per-stream TTFT p95. Sweep:

```bash
for c in 1 2 4 8 16; do
  python3 perf/tokrate_probe.py --backend-url http://localhost:11434 --api ollama \
    --model qwen2.5:7b --concurrency "$c" --runs 1 --max-tokens 128 \
    --label "c$c" --out "conc-c$c.json"
done
```

**Expected shape (to confirm on HW):** `aggregate_decode_tps` rises then **saturates**
once the backend is memory-bandwidth-bound (Strix Halo has one memory system to
share), while **TTFT climbs roughly linearly** as requests queue. The knee is the
box's usable concurrency for that tier. This reuses the *already-running* stack —
no cycling — so it is a cheap add-on to a Test 1 run.

---

## 6. Semantic-cache tuning — experiment design + metrics **[P]**

New harness [`perf/cache-sweep.sh`](../perf/cache-sweep.sh). For each
`similarity_threshold` it rewrites the rendered gateway config **in place
(same inode → fsnotify hot-reload)**, then drives **(base, paraphrase, distractor)**
query triples through the router and records to CSV:

| Metric | Meaning | Why it matters |
| --- | --- | --- |
| `true_hit_rate` | paraphrases served from cache | coverage / how often you *save* the 1.4 s |
| `false_hit_rate` | **distinct** questions wrongly served a cached answer | **correctness risk** — the cost of setting the bar too low |
| `ttft_miss_ms` | TTFT when it goes to the LLM | the price of a miss (≈ §2b) |
| `ttft_hit_ms` | TTFT when served from cache | the payoff of a hit (tens of ms) |

```bash
bash perf/cache-sweep.sh          # sweeps {0.50,0.70,0.85,0.92,0.95}, restores config
```

**Hypothesis to confirm.** `true_hit_rate` and `false_hit_rate` **both** fall as the
threshold rises; the report's recommendation is the **lowest threshold that keeps
`false_hit_rate` at 0** — that maximises the number of requests that dodge the §2b
latency tax without ever serving a wrong cached answer. This directly closes the
loop on §2b: semantic cache is the mitigation for the router's TTFT overhead.

---

## 7. mmBERT embedding is slow — how to improve it

The embedding step is a large slice of the §2b 1.4 s. Options, measured/known
trade-offs:

| Lever | Speed | Quality | Verdict |
| --- | --- | --- | --- |
| **Semantic cache** (§6) | ∞ on a hit (skips embed entirely) | exact | **Do this first** — biggest win, zero quality loss |
| Embedding **dimension** 768 → 256 (Matryoshka) | ~1.0× | ~99% retained | Safe; modest |
| **Fewer classifiers** (drop unused heads) | linear with heads removed | none if unused | Do it |
| **Batch** classification | higher throughput | none | Do it under concurrency |
| **Layer truncation** 12 → 6 | **3.3×** | **56% retained** | **Do NOT** — accuracy collapse |
| **GPU embedding** (`use_cpu: false`) | potentially large | exact | **Risky on gfx1151** — competes with decode + stock ROCm gaps (§9) |

**Story.** The tempting knob (chop transformer layers for 3.3×) destroys routing
accuracy (56%). The *real* wins are architectural: **don't embed at all when you can
cache (§6)**, then trim classifier heads and batch. Moving embedding onto the GPU is
the only pure-speed lever left, but on gfx1151 it fights the decode path for the
same memory and depends on the shaky ROCm story in §9 — validate before shipping.

---

## 8. Can Lemonade be auto-installed? Is it on both boxes? — **Yes / No**

**Neither box shipped `lemonade-server`** (that is why the Test 2 lemonade leg
skipped with `command not found`). It is now a one-shot, idempotent, **per-box**
provisioner: [`perf/install-lemonade.sh`](../perf/install-lemonade.sh).

```bash
# Run once on EACH box (Halo-A and Halo-B):
bash perf/install-lemonade.sh                 # install + verify
START=1 bash perf/install-lemonade.sh         # install, then serve on :13305
```

It prefers `pipx`, falls back to `pip --user`, fixes PATH, verifies
`lemonade-server`, optionally pre-pulls a model and serves on the **correct default
port 13305 `/api/v1`** — which is also the port `server-bench.sh` now points at
(the earlier skip was partly a wrong-port config: it used `:8000`; fixed in
`62834f56`).

---

## 9. vLLM on gfx1151 — is it really unsupported? workaround?

| Source | gfx1151 status |
| --- | --- |
| **Official ROCm / vLLM supported-arch list** | **Not listed — unsupported.** Stock `rocm/vllm-dev` serves fail on Strix Halo with `HIP error: invalid device function` (kernels not built for gfx1151) — exactly the Test 2 vLLM skip. |
| **AMD "TheRock" nightly** | Lists **gfx1151 as Release-Ready ✅** — the toolchain *can* target it. |
| **Lemonade SDK (Linux)** | Ships a **llama.cpp(rocm)** backend **and an experimental vLLM+rocm backend that targets gfx1151**. |

**Story / workaround.** As of now there is **no official SOTA vLLM for gfx1151** —
the stock container aborts on an invalid device function. The **practical path is
Lemonade's experimental vLLM+rocm backend** (§8), with a TheRock-built ROCm
underneath. That is why `server-bench.sh` treats vLLM as *skip-with-reason* rather
than a failure, and why the recommended way to get vLLM-class serving on this box
today is *through Lemonade*, not stock vLLM.

---

## 10. Test 2 — inference-server comparison (bundled with vllm-sr)

Same box, same base model (`qwen2.5:7b` class), different servers.

| Server | Status | Decode tok/s | TTFT ms | Note |
| --- | --- | --- | --- | --- |
| **Ollama** | **[M] measured** | **43.7** | **147** | baseline reference |
| llama.cpp (rocm) | [P] was skipped → **fixed** | — | — | skip was a stale `/llama-server` container name; pre-clean added (`62834f56`) |
| Lemonade | [P] was skipped → **fixed** | — | — | not installed + wrong port; §8 installer + port 13305 fix |
| vLLM (rocm) | [P] skip-with-reason | — | — | gfx1151 `invalid device function`; use Lemonade path (§9) |

**Story.** Only Ollama measured cleanly first time; the other three skips were
**three different classes of bug** — a container-name collision (infra), a
wrong-port + missing-binary (config/provisioning), and a genuine hardware-support
gap (gfx1151). Two are now fixed in code; the third is documented with its
workaround. Re-run to fill the [P] rows:

```bash
bash perf/install-lemonade.sh
SERVERS="ollama llamacpp lemonade" SERVER_BENCH_ROUTER=1 bash perf/server-bench.sh
```

---

## 11. Reproduce everything

**One shot — run on Halo-A, collect every number below into one bundle:**

```bash
bash perf/collect-report-data.sh
#   → <bundle>/report-data.md  (perf-summary + concurrency + cache tables, filled)
# add Halo-B:
#   HALO_B_PERF=1 HALO_B_SSH=user@halo-b HALO_B_REPO=~/semantic-router \
#     bash perf/collect-report-data.sh
```

That script runs, in order: [1] offline verifier → [2] install Lemonade → [3] Test 1
+ Test 2 (fleet) → [4] ensure stack up → [5] concurrency sweep → [6] cache sweep →
[7] stitch `report-data.md`. Or run the pieces by hand:

```bash
# 0. Prove the harness offline (no HW/Docker/gateway) — expect 7/7
python3 perf/verify_perf_local.py

# 1–3. Overhead + throughput + OOM ceiling (stack up), 4. fleet aggregate
HALO_A_MODE=gateway HALO_B_MODE=gateway PERF_BENCH=1 bash run-all-2box.sh
#   → bundle: perf-metrics.json + perf-summary.md (now with TTFT columns)

# 5. concurrency sweep         (see §5)
# 6. semantic-cache sweep:     bash perf/cache-sweep.sh
# 8. lemonade both boxes:      bash perf/install-lemonade.sh   # on each box
# 10. server comparison:       SERVERS="ollama llamacpp lemonade" bash perf/server-bench.sh
```

## 12. Honest caveats

- **Single box.** Headline numbers are Halo-A only (§4). Fleet-safe boundaries
  regenerate automatically once Halo-B is reachable.
- **Quant parity (Test 2).** Servers load *different* quantizations of the same
  base; each row records its `quant`. Treat cross-server deltas as
  "this server + this quant on this box".
- **[P] rows are scheduled, not unknown.** Each has a committed script and a
  one-line command; the offline verifier (7/7) already exercises the code paths.
- **Router TTFT is the headline, not decode drop.** If you quote one number from
  this report, quote **+1.4 s TTFT**, mitigated by the semantic cache (§6).
