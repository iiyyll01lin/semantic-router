# vllm-sr on Strix Halo — performance report

**Topology:** [`strix-halo-fleet-2box`](../README.md) · **SUT:** 2× Ryzen AI Max+
395 (gfx1151, RDNA3.5), 128 GiB unified LPDDR5X each — **Halo-A** 32 GiB VRAM
carveout (~94 GiB OS-visible), **Halo-B** 64 GiB carveout (~62 GiB visible; later
re-tested at 96 GiB — §11.1) ·
**Backend:** Ollama tiers `llama3.2:3b → qwen2.5:7b → qwen2.5:14b → qwen3:14b →
qwen2.5:32b` (+ `llama3.1:70b` / `gpt-oss:120b` on Halo-B) · **Harness:**
[`perf/`](../perf/README.md)

> **Data provenance.** Every data row is tagged **[M]** — *measured* — from the
> current-harness runs: Halo-A + two-box overhead/server/concurrency/cache
> (`report-run-20260712-123240`, `report-run-2box-20260712-153904`), the Halo-B
> symmetric Test 2 + perf-per-watt bundle, and the Halo-B max-model sweep
> ([`halo-b-maxmodel.md`](halo-b-maxmodel.md)). The lone non-measured row is the
> **vLLM** leg of Test 2, kept as an explicit **skip-with-reason** (gfx1151, §9),
> not a gap. The whole harness is offline-verifiable first (`python3
> perf/verify_perf_local.py` → **7/7**), and every number is reproducible from the
> committed code via the exact command given inline in each section.
>
> **To regenerate the fleet numbers in one shot:** `bash perf/collect-report-data.sh`
> — it runs steps [1]–[7] into one bundle and stitches a `report-data.md`. See §13.

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
| How much does vllm-sr **occupy**? | **≈8.4 GiB** unified RAM, router container dominant (~8.8 GB) | §1 **[M]** |
| How much does **throughput drop** (same model)? | Decode tok/s: **≈0% (noise, ±4%)**. TTFT: **156 ms → 1560 ms (+1.4 s)** | §2 **[M]** |
| Which **spec becomes unusable**? | **`qwen2.5:32b` on Halo-A** (~10.7 tok/s); 70B **aborts** (HTTP 500, GTT spill). Halo-B's bigger carveout reaches **`gpt-oss:120b`** | §3, §11 **[M]** |
| Are **both boxes** used? | **Yes — both measured.** Halo-A 94 GiB visible / 32 GiB VRAM; Halo-B 62 GiB / 64 GiB VRAM (the carveout sets the ceiling) | §4 **[M]** |
| **Multi-concurrency** behaviour? | Serialized (Ollama default): **flat ~43 tok/s**, TTFT queues. `OLLAMA_NUM_PARALLEL=4`: scales to **~107 tok/s (~2.5×), knee at c=4** | §5 **[M]** |
| Best **semantic-cache** threshold? | **0.92** (false-hit **0%**, true-hit **83–100%**) — **now enabled on the live path** (was gated off); a hit skips the upstream leg (~0.7–0.9 s hit vs ~1.2 s miss) | §6, §7.1 **[M]** |
| Can a cache hit skip the **router tax** too? | **Yes, for exact repeats — now landed.** A pre-routing exact-match cache (custom from-source image) short-circuits an identical prompt in **~1–2 ms**, skipping embed+classify+routing entirely (vs ~0.7–0.9 s before) | §7.5 **[M]** |
| **Routing accuracy** (guardrail)? | **88.9%** domain over 261 MMLU cases; **unchanged** by both the cache reorder and the head-trim | §7.2 **[M]** |
| **mmBERT embedding** slow — fix? | Cache first (live now). Head-trim **measured −56% signal-eval (0.72→0.31 s)** by dropping the **pii+jailbreak safety heads** (accuracy unchanged) — but **reverted on the live box to keep full safety; kept as an optional config**. GPU offload **empirically crashes on gfx1151** (SIGSEGV in embedding ROCm-EP init, even with the TD-046 fix). **Do not** truncate layers | §7.3–7.5 **[M]** |
| **Lemonade** auto-install? both boxes? | Yes — `install-lemonade.sh`; now **installed + measured on both boxes** | §8, §10 **[M]** |
| **vLLM on gfx1151** SOTA / workaround? | Officially **unsupported** (kernel gap); installed **Lemonade 9.1.4 ships no vLLM backend** either — practical path is **llama.cpp(rocm)** | §9 |
| **Max model** under the topology? | Halo-B (headless, 64 GiB): **`gpt-oss:120b` @ ~30 tok/s, VRAM-resident**. At **96 GiB** carveout **Q8-70B (70.7 GiB) becomes VRAM-resident** — but only with `num_gpu`/`use_mmap` overrides (Ollama's default budget tracks system RAM, so 96 GiB *regresses* out of the box) | §11, §11.1 **[M]** |
| **Perf-per-watt**? | idle ~12–14 W; 7B **0.41**, 32B **0.093**, 120B MoE **0.30** tok/s/W — the MoE is bigger *and* more efficient/token | §12 **[M]** |

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

> **Halo-B — the ceiling moves with the topology.** The boundary above is Halo-A
> (94 GiB unified, GUI up). On **Halo-B**, tuned **headless** with its **GTT enlarged to
> 48 GiB** (OS-only levers; BIOS still 64 GiB VRAM), the *same* co-resident sweep reaches
> **`gpt-oss:120b` (120B MoE, MXFP4) at ~30 tok/s, VRAM-resident (no GTT spill)** — and
> the dense 70B that *fails to load here* loads **cleanly** there (48.2 GiB, vram-fit).
> The reliable ceiling moves from **32B → ≥120B**. Full memory map + ascending sweep +
> failure mode: [`halo-b-maxmodel.md`](halo-b-maxmodel.md) (harness:
> [`perf/maxmodel-sweep.sh`](../perf/maxmodel-sweep.sh)).

---

## 4. Are both boxes actually being exercised? — **Yes** **[M]**

Both boxes now run the full Test 1 co-location sweep with the stack co-resident.
`perf_metrics.py` aggregates fleet-wide and reports the **fleet-safe max usable =
the worst box's boundary**.

| box | unified budget (OS-visible) | BIOS VRAM carveout | stack RAM | max usable (Test 1 tiers) | first unusable |
| --- | --- | --- | --- | --- | --- |
| halo-a | **94.06 GiB** | **32 GiB** | 8.56 GiB | `qwen3:14b` | None |
| halo-b | **62.44 GiB** | **64 GiB** | 8.8 GiB | `qwen3:14b` | None |

Fleet-safe max usable across the standard tier ladder: **`qwen3:14b`** · mean stack
footprint **≈8.68 GiB**. Co-location overhead is symmetric across the two boxes:

| model tier | mean drop % (contention) | mean drop % (end-to-end) | direct TTFT ms | router TTFT ms |
| --- | --- | --- | --- | --- |
| `llama3.2:3b` | 1.5 | 2.0 | 158 | 1467 |
| `qwen2.5:7b` | 1.1 | 1.2 | 141 | 1450 |
| `qwen2.5:14b` | 0.0 | 0.1 | 160 | 1468 |
| `qwen3:14b` | −1.7 | −2.7 | 162 | 1575 |

**Why the box with *more* visible RAM has the *lower* model ceiling — the VRAM
carveout.** Both boxes hold **128 GiB** of physical LPDDR5X, but the BIOS carves a
**fixed VRAM region** out of it, and the two boxes are set differently:

- **Halo-A: 32 GiB VRAM carveout → ~94 GiB OS-visible** system RAM (the "unified
  budget" the sampler reports).
- **Halo-B: 64 GiB VRAM carveout → ~62 GiB OS-visible** system RAM.

The **OS-visible budget** (94 vs 62 GiB) is what the router stack and GTT overflow
share; the **VRAM carveout** (32 vs 64 GiB) is what a model's weights must fit to
stay GPU-resident. **The carveout — not the visible budget — governs the max
model**, because weights that overflow the carveout must spill, and on these boxes
a spill is either fatal or slow:

- On **Halo-A** (32 GiB carveout) the dense **70B (~48.9 GB) overflows the carveout,
  spills to GTT, and the load *aborts* (HTTP 500)** — so its ceiling is
  `qwen2.5:32b` (26.7 GiB, fits the carveout). See §3.
- On **Halo-B** (64 GiB carveout) that same **70B fits *entirely in VRAM* (48.2 GiB,
  no spill)** and even **`gpt-oss:120b` (56.6 GiB MoE) is VRAM-resident at ~30
  tok/s**; overflow only begins past 64 GiB and is a *soft* CPU-offload, not an
  abort. See §11.

So the larger VRAM carveout — despite leaving *less* OS-visible RAM — is exactly
what moves the reliable ceiling from **32B (Halo-A) → 120B (Halo-B)**.

```bash
# Fleet-wide, both boxes, one bundle:
HALO_A_MODE=gateway HALO_B_MODE=gateway PERF_BENCH=1 bash run-all-2box.sh
```

---

## 5. Multi-concurrency **[M]**

`tokrate_probe.py` drives N parallel streams (`--concurrency` / `CONCURRENCY`) and
reports `aggregate_decode_tps` plus per-stream TTFT p95. Measured on Halo-A
(`qwen2.5:7b`, `max_tokens=128`, `prompt_tokens=256`), sweeping `c = 1,2,4,8,16`
under **two** backend configs: Ollama's default (single decode slot) and the same
container with **`OLLAMA_NUM_PARALLEL=4`**.

| c | serialized agg tok/s | serialized TTFT p95 | parallel-`p4` agg tok/s | parallel-`p4` TTFT p95 | throughput speedup | TTFT p95 better |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 41.8 | 152 ms | 41.7 | 156 ms | 1.00× | 1.0× |
| 2 | 42.6 | 3098 ms | 66.5 | 397 ms | 1.56× | 7.8× |
| 4 | 43.1 | 8980 ms | 100.0 | 436 ms | 2.32× | 20.6× |
| 8 | 43.3 | 20753 ms | 107.3 | 4919 ms | 2.48× | 4.2× |
| 16 | 43.4 | 41305 ms | 107.2 | 14452 ms | 2.47× | 2.9× |

_Per-stream decode under parallelism falls as slots fill (memory-bandwidth
contention on the unified-memory APU): 43.9 (c1) → 36.1 (c2) → 27.1 (c4) → ~27.5
(c8/16) tok/s._

**Story — two completely different curves from the same box.**

- **Serialized (Ollama default):** aggregate throughput is **flat at ~42.9 tok/s**
  for *every* concurrency — there is zero parallel benefit — while **TTFT p95 grows
  linearly with the queue** (0.15 s → 41.3 s at c=16). Concurrency **queues, it does
  not scale**: one decode slot, extra requests just wait.
- **Parallel (`OLLAMA_NUM_PARALLEL=4`):** aggregate throughput **scales 41.7 → 66.5
  → 100.0 tok/s across c=1..4, then saturates at ~107 tok/s** (c=8 peak, c=16 flat)
  — **~2.5× the serialized ceiling.**
- **The saturation knee is c=4** — it equals the parallel-slot count. There
  throughput is already ~2.3× serialized *and* TTFT p95 stays low (0.44 s vs 8.98 s
  serialized, ~20× better). Beyond c=4 you buy only ~7% more throughput (107 vs 100)
  while TTFT p95 balloons (4.9 s @ c=8, 14.5 s @ c=16) as extra requests queue for
  the 4 slots.
- **Recommended operating point:** run concurrency **≈ `OLLAMA_NUM_PARALLEL`** for
  the best throughput/latency trade-off; raise `OLLAMA_NUM_PARALLEL` (or use
  llama.cpp/vLLM slotting) to push the knee higher, bounded by memory bandwidth.

Reproduce (reuses the *already-running* stack — no cycling — so it is a cheap
add-on to a Test 1 run):

```bash
for c in 1 2 4 8 16; do
  python3 perf/tokrate_probe.py --backend-url http://localhost:11434 --api ollama \
    --model qwen2.5:7b --concurrency "$c" --runs 1 --max-tokens 128 \
    --label "c$c" --out "conc-c$c.json"
done
# parallel curve: recreate the ollama container with -e OLLAMA_NUM_PARALLEL=4, re-sweep, restore.
```

---

## 6. Semantic-cache tuning — threshold sweep **[M]**

Harness [`perf/cache-sweep.sh`](../perf/cache-sweep.sh). For each
`similarity_threshold` it rewrites the rendered gateway config **in place
(same inode → fsnotify hot-reload)**, then drives **(base, paraphrase, distractor)**
query triples through the router and records to CSV:

| Metric | Meaning | Why it matters |
| --- | --- | --- |
| `true_hit_rate` | paraphrases served from cache | coverage / how often you *save* the 1.4 s |
| `false_hit_rate` | **distinct** questions wrongly served a cached answer | **correctness risk** — the cost of setting the bar too low |
| `ttft_miss_ms` | TTFT when it goes to the LLM | the price of a miss (≈ §2b) |
| `ttft_hit_ms` | TTFT when served from cache | the payoff of a hit (tens of ms) |

Measured sweep (Halo-A, router in-loop):

| threshold | true_hit_rate | false_hit_rate | ttft_miss_ms | ttft_hit_ms |
| --- | --- | --- | --- | --- |
| 0.50 | 1.00 | **1.00** | 1467 | 692 |
| 0.70 | 1.00 | **0.67** | 1016 | 683 |
| 0.85 | 0.83 | **0.50** | 1073 | 777 |
| **0.92** | **0.83** | **0.00** | 1059 | 729 |
| 0.95 | 0.67 | **0.00** | 1065 | 703 |

**Recommendation: `similarity_threshold = 0.92`.** It is the **lowest threshold
that drives `false_hit_rate` to 0** (never serves a distinct question a wrong
cached answer) while still keeping **`true_hit_rate` at 83%** — so paraphrases keep
dodging the §2b ~1.4 s latency tax and return in ~0.7 s instead of ~1.1 s. Going
higher to 0.95 buys **no correctness** (false-hit already 0) but **loses coverage**
(true-hit 83% → 67%); going lower starts serving wrong answers (false-hit 0.50 at
0.85, up to 1.00 at 0.50). This closes the loop on §2b: the semantic cache is the
direct mitigation for the router's TTFT overhead, and 0.92 maximises the requests
that skip the pipeline with zero correctness risk.

```bash
bash perf/cache-sweep.sh          # sweeps {0.50,0.70,0.85,0.92,0.95}, restores config
```

**Now enabled on the live path (not just swept). [M]** The sweep above enabled
caching only *transiently* — `cache-sweep.sh` restores the config at the end — so
the persistent live path kept running with caching **off** (confirmed in §7.1:
`find_similar` = 0). Root cause: with routing `decisions:` present, the global
`stores.semantic_cache.enabled` toggle is **ignored**; the cache only runs on a
decision that carries a `semantic-cache` plugin (`config/helper.go`
`IsCacheEnabledForDecision`). Fix (committed to `poc-strix.yaml`): a
`- type: semantic-cache` / `configuration.enabled: true` plugin on **all 14
non-`security_guard` decisions**, plus a global
`stores.semantic_cache.similarity_threshold: 0.92`. Live re-measurement on the
persistent config (router `:8899`, header `x-vsr-cache-hit`) reproduces the sweep:

| workload @0.92 (live, persistent) | true_hit | false_hit | ttft_miss | ttft_hit |
| --- | --- | --- | --- | --- |
| §6 cases (n=6) | **0.83** | **0.00** | ~1180 ms | ~880 ms |
| novel triples (n=8) | **1.00** | **0.00** | ~1189 ms | ~790 ms |

`find_similar` now runs (Prometheus `llm_cache_operation_duration_seconds{operation="find_similar"}`:
**93 calls, ~46 ms avg** over the probe), and hits are served from the
`plugin.execution` span (`cache.hit=true`), **skipping the upstream LLM call**. The
true-hit rate rides the tightness of the paraphrase (0.83 on the §6 wording that
includes one 0.887-similarity pair, 1.00 on tighter paraphrases); false-hit stays
**0%** either way, confirming 0.92 as the correctness-safe operating point on the
live stack. See §7.1 for the hit-vs-miss span decomposition (a hit saves the
*upstream* leg, not the embed/classify tax).

---

## 7. mmBERT embedding is slow — how to improve it

The embedding step is a large slice of the §2b 1.4 s. Options, measured/known
trade-offs:

| Lever | Speed | Quality | Verdict |
| --- | --- | --- | --- |
| **Semantic cache** (§6) | ∞ on a hit (skips embed entirely) | exact | **Do this first** — biggest win, zero quality loss |
| Embedding **dimension** 768 → 256 (Matryoshka) | ~1.0× | ~99% retained | Safe; modest |
| **Fewer classifiers** (drop heads) | **non-linear** (contention): −18% dropping jailbreak, **−60% dropping pii+jailbreak** | **every slow head is used**; the two heaviest are the **safety** heads | **§7.3** — measured, **flagged for user** |
| **Batch** classification | higher throughput | none | Do it under concurrency |
| **Layer truncation** 12 → 6 | **3.3×** | **56% retained** | **Do NOT** — accuracy collapse |
| **GPU embedding/heads** (`use_cpu: false`) | untested — **blocked** | exact | **Blocked by TD-046** (§7.4): concurrent ROCm ONNX init SIGSEGVs the router; kept CPU |

**Story.** The tempting knob (chop transformer layers for 3.3×) destroys routing
accuracy (56%). The *real* wins are architectural: **don't embed at all when you can
cache (§6)**, then trim classifier heads and batch. Moving embedding onto the GPU is
the only pure-speed lever left, but on gfx1151 it fights the decode path for the
same memory and depends on the shaky ROCm story in §9 — validate before shipping.

### 7.1 TTFT decomposition — where the ~1.4 s actually goes **[M]**

To locate the §2b tax, **8 distinct (cache-missing) prompts** were sent through the
router (`:8899`) and each request's **Jaeger trace** (service `vllm-sr`, via
`/api/traces`) was read span-by-span, corroborated by the **Prometheus stage
histograms** (`:9190`, before/after deltas). The pipeline splits cleanly — one
stage is essentially the whole tax:

| Stage | Instrument | Per request | Share of router tax |
| --- | --- | --- | --- |
| **Signal extraction** — mmBERT embed → fan-out of CPU-pinned ONNX classifiers (concurrent) | Jaeger `signal.evaluation` span | **~830 ms median / ~1020 ms mean** (686–2393) | **≈ 100%** |
| Routing / decision evaluation | Jaeger `decision.evaluation`; Prom `…decision_evaluation_latency` | **~0.4 ms** | ~0% |
| Model selection (ML selectors) | Prom `…model_selection_duration` | **0** — rule-based decision-engine path taken | 0% |
| Semantic-cache lookup | Prom `…cache_operation{find_similar}`; Jaeger `plugin.execution` (`cache.hit`) | **0** on the original run (cache gated off); **now live: ~46 ms avg** after enabling the plugin | small — a *hit* skips the **upstream** leg, not embed/classify (box below) |
| **Total router processing** | Prom `…model_routing_latency` | **~1030 ms mean** | 100% |
| _Upstream first token — **not** router tax_ | Jaeger `upstream.request`; Prom `…model_ttft` | _~165 ms warm (local qwen); seconds when the routed model cold-loads or is remote_ | — |

**Inside the dominant stage.** The signal heads run **concurrently** (goroutine
fan-out), so the `signal.evaluation` wall-clock (~0.8–1.0 s) is the **critical path
≈ the slowest head — not the sum.** Per-head latency under that concurrent CPU
contention (Prometheus `llm_signal_extraction_latency_seconds`, mean over the run):

| Signal head | avg ms (concurrent) |
| --- | --- |
| **pii** | **~880** |
| jailbreak | ~735 |
| complexity | ~735 |
| domain | ~600 |
| fact_check | ~600 |
| **mmBERT embedding** (prerequisite for the heads) | **~480** |
| language | ~40 |
| keyword | ~3 |
| structure | ~0.3 |

**Story — the §2b ~1.4 s is essentially one stage: embed + classify.** Routing,
decision and model-selection are **sub-millisecond**; the tax is *entirely* the
signal-extraction stage — the mmBERT embedding feeding a fan-out of CPU-pinned ONNX
classifiers, with **pii / jailbreak / complexity the slowest heads**. Because the
heads overlap, the lever is not "make one head faster" but **remove or shorten the
stage**: a semantic-cache *hit* (§6, **now enabled on the live path** — box below)
skips the **upstream** call but — measured — **still pays embed+classify** (caching
is scoped *per routing decision*, so the router must embed + classify to pick the
decision before it can consult that decision's cache), and dropping unused
classifier heads pulls the critical path down toward the next-slowest head. This is the measured backing for the §7 levers above, and it
confirms the warning in the table: **layer truncation would only shave the embedding
leg (~0.48 s) while collapsing accuracy — not worth it.** _(Measured on the current
live stack, whose routed models — `qwen/qwen3.5-rocm` plus cloud tiers — differ from
the Ollama tiers timed in §2b; the **decomposition/shape** is the result here, and
it matches §2b's embed-dominated ~1.4 s tax. Reproduce: send a few `:8899` requests,
then `GET :16686/api/traces?service=vllm-sr` + diff `:9190/metrics`.)_

**Cache hit vs miss — live span decomposition (0.92, now enabled). [M]** With the
`semantic-cache` plugin live on the 14 decisions, a HIT and a MISS were traced
span-by-span (Jaeger `vllm-sr`):

| request | `signal.evaluation` | `plugin.execution` (cache) | `upstream.request` | wall |
| --- | --- | --- | --- | --- |
| MISS | ~715–870 ms | ~36–66 ms | ~340–360 ms | ~1090–1290 ms |
| HIT | ~700–720 ms | ~0.4 ms | **skipped** | **~720–880 ms** |

**The hit does *not* skip the ~0.7 s embed/classify stage.** Because caching is
per-decision (`IsCacheEnabledForDecision`), the router must run the full signal
extraction to *pick* the decision before it can look up that decision's cache — so a
hit only removes the **upstream** leg (~0.34 s here on a warm local `qwen`; **seconds**
when the routed tier is cold-loading or a remote cloud model). Net: the *semantic*
cache is the right lever when the *upstream* is the expensive part, but the ~0.7 s
router tax itself is addressable two ways: **(a)** an **exact-match pre-routing cache**
that skips embed+classify+routing for identical repeats (**landed — §7.5**, exact
repeat ~1–2 ms), and **(b)** shortening signal extraction via **head-trimming
(§7.3)**. Layer truncation is still the wrong lever.

### 7.2 Routing-accuracy baseline — the guardrail for head-trimming **[M]**

Before trimming any classifier head (§7 "fewer classifiers"), we need a routing
baseline to prove no regression. Harness
[`perf/route-accuracy.py`](../perf/route-accuracy.py) — a stdlib replay of the
e2e corpus `e2e/testcases/testdata/domain_classify_cases.json` (**261 labeled
MMLU-style cases, 14 domains**), the standalone counterpart of the k8s e2e tests
`domain_classify.go` / `model_selection.go`. It scores against the router's own
classification API `:8080/api/v1/classify/intent` (the **same decision engine**
the `:8899` data-path uses — cross-checked equal on locally-routed queries — but
upstream-independent, so cloud-tier routes don't 503 away their headers).

**Overall domain-classification accuracy: 88.9% (232/261).** Per category, with
the dominant decision → model each domain routes to:

| domain | acc | n | top decision | top model |
| --- | --- | --- | --- | --- |
| biology | 88% | 16 | simple_general | qwen/qwen3.5-rocm |
| business | 89% | 18 | medium_explainer | qwen/qwen3.5-rocm |
| chemistry | 95% | 19 | complex_specialist | qwen/qwen3.5-rocm |
| computer science | 95% | 19 | complex_specialist | qwen/qwen3.5-rocm |
| economics | 95% | 21 | medium_explainer | qwen/qwen3.5-rocm |
| engineering | 95% | 20 | complex_specialist | google/gemini-3.1-pro |
| health | 82% | 17 | casual_chat | qwen/qwen3.5-rocm |
| history | 77% | 22 | medium_explainer | qwen/qwen3.5-rocm |
| law | 89% | 19 | premium_legal | anthropic/claude-opus-4.6 |
| math | 84% | 19 | reasoning_deep | google/gemini-3.1-pro |
| other | 100% | 15 | simple_general | qwen/qwen3.5-rocm |
| philosophy | 88% | 16 | casual_chat | qwen/qwen3.5-rocm |
| physics | 79% | 19 | simple_general | qwen/qwen3.5-rocm |
| psychology | 90% | 21 | medium_explainer | qwen/qwen3.5-rocm |

Decision mix (all 261): `simple_general` 62, `medium_explainer` 40, `casual_chat`
36, `complex_specialist` 30, `premium_legal` 23, `reasoning_deep` 19,
`verified_explainer` 19, `fast_qa` 13, `medium_code_general` 9, `medium_creative`
6, `security_guard` **3**, `verified_health` 1. Model mix: `qwen/qwen3.5-rocm`
169, `google/gemini-3.1-pro` 50, `anthropic/claude-opus-4.6` 23,
`google/gemini-2.5-flash-lite` 19. Full per-case records:
[`perf/route-accuracy-halo-a.json`](../perf/route-accuracy-halo-a.json).

_Note: **3 legit science questions** (a plant-genetics, a heat-of-combustion, and
a nozzle-shock problem) route to `security_guard` — i.e. the jailbreak/PII guard
false-positives on them. That is a real (small) accuracy cost of keeping those
safety heads, and a data point for the §7-head-trim trade-off below._

```bash
BOX=halo-a python3 perf/route-accuracy.py baseline        # writes route-accuracy-halo-a.json
```

### 7.3 Head-trim audit — measured win vs. capability (flagged for decision) **[M]**

**Audit (which signals each decision actually uses).** Static pass over
`routing.decisions[].rules` + the runtime `/api/v1/eval` `used_signals`: **every
slow head is referenced by routing** — there is no genuinely-unused head to drop
for free. `domain` (28 rule refs, core tier routing), `complexity` (8, reasoning
tiers), `fact_check` (7, verified tiers), `pii` (1, `security_guard`),
`jailbreak` (1, `security_guard`). (`user_feedback`/feedback_detector is
configured but does **not** run on the request TTFT path; hallucination /
modality / mcp / prompt_compression are off or response-only.)

**Per-head latency under full concurrent contention (Prom `llm_signal_extraction_latency_seconds`):**
pii **707**, jailbreak **671**, complexity **581**, fact_check **396**, domain
**393**, embedding **363**, language 27, keyword 1.6, structure 0.1 ms. The two
heaviest are the two **safety** heads.

**Measured `signal.evaluation` wall-clock (10 `:8899` requests each, Jaeger):**

| config | signal.eval median | Δ vs all-on | capability removed |
| --- | --- | --- | --- |
| all heads on (baseline) | **716 ms** | — | — |
| jailbreak head off | **584 ms** | **−132 ms (−18%)** | jailbreak guard |
| pii + jailbreak off | **284 ms** | **−432 ms (−60%)** | PII detection **and** jailbreak guard |

The drop is **non-linear** and far bigger than the "wall = slowest static head"
model predicts (that predicted ~581 ms): the pii/jailbreak ONNX heads are the two
heaviest, so removing them **relieves CPU contention** and the *remaining* heads
speed up too (complexity's static 581 ms collapses into a 284 ms whole-stage
wall). So the real TTFT lever here is **exactly the two safety heads**.

**Routing impact of the pii+jailbreak-off experiment** (re-ran §7.2 over all 261
cases): **domain accuracy unchanged at 88.9%**, model distribution identical, and
the *only* decision change is the **3 science questions that were false-positived
into `security_guard` now route to their real decisions** (`security_guard` 3→0).
So on this benign MMLU corpus dropping the guard causes **no routing regression**
(a tiny correctness *gain*) — but that is precisely because the corpus contains no
real attacks/PII; in production the change **removes real PII-leak and
prompt-injection protection**.

**Decision — measured under approval, then reverted on the live box; kept as an
optional config.** No head is unused, and `domain`/`complexity`/`fact_check` drive
real routing tiers in the §7.2 baseline (dropping them regresses routing). The
only material TTFT win (**−60%, 716→284 ms**) requires dropping **pii + jailbreak
— both safety capabilities**. The trade was approved and applied *as an
experiment*, re-measured live (below), then — by explicit decision — **reverted so
the live router keeps full PII + jailbreak safety**. `poc-strix.yaml` ships with
`prompt_guard.enabled: true` and the pii/jailbreak conditions present in the
`security_guard` rules; the measured win is retained here as a documented, opt-in
lever, not a shipped default.

**Measured result [M] (live re-measure, from-source image, CPU-pinned):**
with pii + jailbreak disabled, `signal.evaluation` median **716 → 313 ms (−56%)**,
matching this audit's −60% prediction within run-to-run noise; per-head Prometheus
counters confirmed `pii`/`jailbreak` stopped executing. Routing accuracy
**unchanged at 88.9% (232/261)**; the sole decision change was `security_guard`
**3 → 0** (the three false-positived science questions routed to their real
decisions). **This trimmed config is NOT live** — after measuring it, we reverted
to the full-safety config (per-head metrics again show `pii`+`jailbreak` active;
`security_guard` back to 3). To re-enable the lever, set
`prompt_guard.enabled: false` and drop the pii/jailbreak conditions from
`security_guard`. Keep the heads on for any deployment facing real adversarial or
PII-bearing traffic — which is exactly why the live box keeps them.

> **Trade the ML PII-detection + jailbreak guard for a ~56–60% (~0.4 s) router-TTFT
> cut?** On this offline PoC those heads only ever *false-positived* (3/261) and
> the keyword markers in `security_guard` still catch obvious cases; but any
> deployment facing real adversarial or PII-bearing traffic should keep them.
> INT8-quantising the kept heads (separate `openvino-binding` lift) is the only
> path to cutting the tax **without** dropping a capability (see §7.5).

```bash
# reproduce the applied head-trim (poc-strix.yaml already ships it): redeploy and
# re-measure -- BOX=halo-a python3 perf/route-accuracy.py post-head-trim ;
# send :8899 requests + GET :16686/api/traces?service=vllm-sr for signal.evaluation.
```

### 7.4 GPU-offload experiment — documented blocker, kept CPU **[M]**

Goal: flip the embedding + classifier heads to the iGPU (`use_cpu: false`) and
measure the TTFT delta vs. decode impact. **Outcome: attempted live on a
from-source image with the TD-046 fix implemented — it still crashes on gfx1151.
Reverted; classifiers stay on CPU.**

**Empirical result [M] (from-source image `VLLM_SR_AMD_FORCE_GPU=1`).** We
implemented TD-046's exit criteria — a session-creation mutex in
`onnx-binding/src/ffi/classification.rs` (holds a `parking_lot::Mutex` across each
`MmBert*Classifier::load`) plus `MaxParallelism = 1` in
`classifier_lifecycle.go` — rebuilt the ROCm router image, and deployed with GPU
classifiers. The router **still `SIGSEGV`s on startup**, but the crash is
**earlier than TD-046's concurrent-classifier-init race**: after the GPU is
detected (`[gpu_memory] Probed VRAM: total=47.0 GB … sessions=3`) the very first
GPU session — the shared **mmBERT embedding** — crashes during ROCm
execution-provider creation:

```
embedding_models_init_started … use_cpu:false
INFO: Attempting ROCm execution provider...
SIGSEGV: segmentation violation … signal arrived during cgo execution
… candle-binding._Cfunc_init_mmbert_embedding_model … InitMmBertEmbeddingModel(…)
```

So on this box the blocker is **the ROCm-EP session build for the embedding model
on gfx1151**, which fails before any classifier head is even created. The TD-046
serialization fix is correct and necessary for the *later* concurrent-classifier
race, but it cannot help here — the router never reaches that stage — and
`MaxParallelism=1` would only slow the CPU reload path, so both changes were
reverted and the deploy rolled back to the CPU image. This **empirically confirms**
(with a live crash trace) the inspection-based conclusion below.

**Why the GPU path crashes (concrete, current code):**
1. The GPU flip is **all-or-nothing**. `--platform amd` with
   `VLLM_SR_AMD_FORCE_GPU` (or simply unsetting `VLLM_SR_AMD_PRESERVE_CPU`) runs
   `apply_platform_gpu_defaults` → `_set_use_cpu_false_for_amd`
   (`cli/commands/runtime_config_mutation.py`), which recursively rewrites **every**
   classifier's `use_cpu: true → false` (embedding, prompt_guard, domain, pii,
   fact_check, detector, explainer, feedback, modality). There is no
   per-head/serialized GPU option.
2. The Go classifier runtime then creates those ROCm ONNX sessions **concurrently**
   — `classifier_lifecycle.go` still uses
   `MaxParallelism: modelruntime.DefaultParallelism(len(tasks))` (= `NumCPU`), and
   `onnx-binding` session creation is unsynchronised → **`SIGSEGV` inside
   `init_sequence_classifier`/`init_token_classifier`**, killing the whole router
   (incl. the `:8080` apiserver), on startup **and on every `:8080` config reload**
   (TD-046).
3. **No runtime serialize-init toggle exists** — the fix is code
   (`MaxParallelism = 1` + a creation mutex in the FFI) **+ a from-source router
   image rebuild**. We implemented and built exactly that (see the empirical result
   above); it correctly serializes classifier-session creation but is moot here
   because the crash is upstream of it, in the embedding ROCm-EP build on gfx1151.

The current stack confirms the setup: the router container **is** ROCm-capable
(`/opt/rocm/lib` + `/opt/onnxruntime/capi` on `LD_LIBRARY_PATH`, `/dev/kfd` +
`/dev/dri` passed through) and is CPU-pinned **on purpose** —
`VLLM_SR_AMD_PRESERVE_CPU=1`, all 8 `use_cpu` flags `true` — i.e. this is a
deliberate crash-avoidance, not a missing-hardware gap.

**Even a manual single-session hack isn't worth it.** Editing just
`embeddings.semantic.use_cpu: false` (one GPU session, dodging the concurrent-init
crash) targets the **wrong stage**: the embedding is ~0.36–0.48 s but is **not**
the critical path — §7.3 shows the wall is head-bound (~0.71 s), so a GPU embedding
wouldn't move it. And gfx1151 ROCm is shaky: the fp16 flash-attention ONNX ops
already fail to register on this box (`com.ck:CKFlashAttention(-1) is not a
registered function/op`, seen every reload — it silently falls back), and GPU
classifiers would then fight the Ollama LLM **decode** for the shared unified
memory (§9). Low reward, real crash/contention risk.

**Verdict: stays CPU (gfx1151 ROCm-EP, then TD-046).** We *did* land TD-046's exit
criteria (serialize ROCm session creation) on a rebuilt image and flip
`use_cpu:false` — and the router still crashed, now in the **embedding** ROCm-EP
build (above), i.e. gfx1151's shaky ROCm is the first wall and TD-046 the second.
Both point the same way: **keep classifiers on CPU on this box.** Revisit only if
a future ROCm/gfx1151 stack builds the embedding ONNX session without segfaulting.

[TD-046]: ../../../../docs/agent/tech-debt/td-046-onnx-binding-concurrent-rocm-session-init-segfault.md

### 7.5 Custom from-source router image — cache reorder (landed) + INT8 (blocked) **[M]**

A/C/D above require a **from-source** `vllm-sr-rocm` router image (the live stack
runs the prebuilt `ghcr.io/.../vllm-sr-rocm:latest`). We built that image from the
current source (`make docker-build-vllm-sr-router VLLM_SR_PLATFORM=amd` →
`src/vllm-sr/Dockerfile.rocm`), deployed it via `VLLM_SR_ROUTER_IMAGE`, and
**verified parity as a hard gate**: `/config/hash` up, routing accuracy **88.9%
(232/261)** identical, `signal.evaluation` median **701 ms** — matching the §7.3
all-heads-on baseline. `:latest` stays the instant rollback.

**Cache reorder — LANDED.** §7.1 showed the semantic-cache hit still pays the
~0.7 s embed+classify because caching is per-decision and only consulted *after*
routing. We added an **exact-match pre-routing cache** (Go-only:
`InMemoryCache.FindExact` + an optional `ExactMatcher` interface, called in
`runRequestPreRoutingStages` *before* `signal.evaluation`). An identical repeat
prompt is now served **before** embedding/classification/routing:

| request | signal.evaluation | plugin (cache) | upstream | **client wall TTFT** |
| --- | --- | --- | --- | --- |
| miss (full pipeline) | ~0.70 s | ~45 ms | ~0.36 s (warm) | ~1.17 s |
| **exact repeat (new)** | **skipped** | ~1 ms exact lookup | **skipped** | **~1–2 ms** |
| semantic/paraphrase hit (unchanged) | ~0.70 s | ~45 ms | skipped | ~0.72–0.85 s |

Exact repeats collapse from ~0.72–0.88 s (old per-decision hit) or ~1.17 s (miss)
to **~1–2 ms** — the ~0.7 s router tax is gone for them. The semantic/paraphrase
path is deliberately left on the per-decision post-routing lookup (a pre-routing
semantic match could cross decision boundaries), so **routing accuracy is
unchanged at 88.9%** and there is no false-hit regression.

**INT8 heads (OpenVINO) — DOCUMENTED BLOCKER (not landed).** INT8 is first-class
only via the separate `openvino-binding` (`--weight-format int8`), and the router
*has* an OpenVINO classifier backend — but it is **compiled out** of the shipped
image: the ROCm build is `-tags=onnx`, so `openvino_backend_cgo.go` (which needs
`//go:build openvino`) is replaced by the stub. Wiring INT8 in would require, on a
disk-tight AMD box, **all** of: (1) rebuild the router with `-tags=openvino`;
(2) a CMake C++ build of `openvino-binding` + the **OpenVINO SDK/runtime** in the
image (the deployed ORT exposes only `MIGraphX/ROCM/CPU` execution providers — **no
OpenVINO EP** — and no `openvino`/`optimum-intel` toolchain is present anywhere);
(3) `optimum-cli export openvino --weight-format int8` for each kept head; (4)
re-wiring the classifier runtime to route each head through OpenVINO. Moreover the
OpenVINO backend as implemented accelerates the mmBERT **embedding** + a single
ModernBert classifier, **not** the CPU-pinned head suite (pii/jailbreak/complexity)
that dominates TTFT (§7.1) — so INT8-embedding would not move the head-bound
critical path (same conclusion as §7.4's GPU-embedding argument). Given the scope
and the box's disk limits, INT8 was **not integrated**; the router was left
untouched. This is the plan's documented-negative fallback for the INT8 lever;
head-trimming (§7.3) reaches the same TTFT cut but was **reverted on the live box
to preserve safety** and remains an opt-in config rather than a shipped default.

---

## 8. Can Lemonade be auto-installed? Is it on both boxes? — **Yes / Yes (now)**

**Neither box *shipped* `lemonade-server`** (that is why the first Test 2 lemonade
leg skipped with `command not found`). It is now a one-shot, idempotent, **per-box**
provisioner — [`perf/install-lemonade.sh`](../perf/install-lemonade.sh) — and has
been **installed and measured on both boxes** (`lemonade-sdk` 9.1.4; the Test 2
lemonade rows in §10 are now live on Halo-A *and* Halo-B).

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
| **Lemonade SDK 9.1.4 (installed, Linux)** | **No vLLM backend.** Verified on the box: `serve` exposes only `--llamacpp {vulkan,rocm,metal,cpu}`; the recipe registry (`server_models.json`) has `llamacpp` / `oga-cpu·igpu·npu·hybrid` / `flm` / `whispercpp` — **no `vllm` recipe** — and no `vllm`/`torch`/`rocm` in the venv. vLLM appears only as a **roadmap "Under Consideration"** item in the package METADATA, not as a shipped backend. |

**Story / workaround.** There is **no official SOTA vLLM for gfx1151** — the stock
container aborts on an invalid device function — **and, contrary to the earlier
assumption in this section, the installed Lemonade 9.1.4 provides no vLLM+rocm path
either.** A time-boxed check of the installed SDK (no from-source build attempted)
found its serving backends are **llama.cpp(rocm) + OnnxRuntime-GenAI (OGA) +
FastFlowLM + whisper.cpp**, with **vLLM only listed "Under Consideration" on the
project roadmap** — there is no `vllm` recipe and no `vllm`/`torch` in the venv. So
vLLM on this box is a **skip-with-reason on two independent grounds**: stock vLLM's
gfx1151 kernel gap, *and* the absence of any shipped vLLM backend in the installed
Lemonade. The practical vLLM-*class* serving path on gfx1151 today is therefore
**llama.cpp(rocm)** (the fastest server in §10) — *not* stock vLLM and *not*
Lemonade-vLLM. Revisit if AMD's TheRock ROCm plus a future Lemonade vLLM recipe
land; until then it is a substantiated skip, not a data gap.

---

## 10. Test 2 — inference-server comparison (bundled with vllm-sr) **[M]**

Same box, same base model (`qwen2.5-7b` class), different servers
(`max_tokens=128`, `prompt_tokens=256`, `runs=3`, direct path). Measured on
**both** boxes — the three server skips from the first pass are now fixed in code,
so ollama / llama.cpp / Lemonade all measure cleanly; vLLM stays skip-with-reason.

### Halo-A (fastest: llama.cpp)

| Server | Status | Decode tok/s | TTFT ms | vs ollama | Quant |
| --- | --- | --- | --- | --- | --- |
| **Ollama** | **measured** | **43.0** | 142 | +0.0% | Q4_0 (ollama default) |
| **llama.cpp** (rocm) | **measured** | **43.2** | **28** | +0.4% | Q4_K_M |
| **Lemonade** | **measured** | **39.8** | 90 | −7.3% | Q4_1 (Qwen3-8B-GGUF) |
| vLLM (rocm) | **skip-with-reason** | — | — | — | fp16/awq — gfx1151 `invalid device function` (§9) |

### Halo-B (fastest: llama.cpp)

| Server | Status | Decode tok/s | TTFT ms | vs ollama | Quant |
| --- | --- | --- | --- | --- | --- |
| **Ollama** | **measured** | **44.7** | 139 | +0.0% | Q4_0 (ollama default) |
| **llama.cpp** (rocm) | **measured** | **46.0** | **28** | +3.1% | Q4_K_M |
| **Lemonade** | **measured** | **39.7** | 96 | −11.1% | Q4_1 (Qwen3-8B-GGUF) |
| vLLM (rocm) | **skip-with-reason** | — | — | — | fp16/awq — gfx1151 `invalid device function` (§9) |

**Story.** On the first pass only Ollama measured cleanly; the other three skips
were **three different classes of bug** — a stale `/llama-server` container-name
collision (infra), a wrong-port + missing-binary Lemonade (config/provisioning),
and a genuine hardware-support gap (gfx1151 vLLM). The first two are now fixed in
code (`62834f56`) and confirmed on **both** boxes: llama.cpp is the fastest server
on each (lowest TTFT ~28 ms, and it edges ollama on decode), while Lemonade is a
touch slower **because it serves a different artifact** — `Qwen3-8B-GGUF` (an 8B
reasoning model), not `qwen2.5-7b` — so its −7 to −11% is a quant/model-parity
gap, not a server deficiency (see the caveat in §14). vLLM remains the third class:
a documented skip-with-reason, not a failure — and the practical vLLM-class serving
path on gfx1151 is **llama.cpp(rocm)**, since the installed Lemonade 9.1.4 ships no
vLLM backend either (§9, verified on-box).

```bash
bash perf/install-lemonade.sh                        # once per box
SERVERS="ollama llamacpp lemonade" bash perf/server-bench.sh
```

---

## 11. Max model under the topology (Halo-B, headless) **[M]**

§3 is the **Halo-A** ceiling (32 GiB carveout, GUI up): `qwen2.5:32b`, and the 70B
aborts. This is the **Halo-B** counterpart — tuned **headless** with GTT enlarged to
48 GiB (OS-only levers; BIOS still 64 GiB VRAM), the *same* co-resident ascending
sweep ([`perf/maxmodel-sweep.sh`](../perf/maxmodel-sweep.sh)):

| Rung | Type | Verdict | Mem mode | Decode tok/s | Peak VRAM | Peak GTT |
| --- | --- | --- | --- | --- | --- | --- |
| `qwen2.5:32b` | 32B dense Q4 | **usable** | vram-fit | **10.9** | 26.7 GiB | ~0 |
| `llama3.1:70b` | 70B dense Q4 | **usable** | vram-fit | **3.6** | 48.2 GiB | ~0 |
| `llama3.1:70b` @ `num_ctx=131072` | 70B + max KV | **usable** | vram-fit | **3.9** | 55.9 GiB | ~0 |
| **`gpt-oss:120b`** | **120B MoE MXFP4** | **usable** | **vram-fit** | **30.4** | **56.6 GiB** | ~0 |
| `llama3.1:70b-instruct-q8_0` | 70B dense Q8 (~69 GiB) | **unusable(slow-spill)** | vram-exceeded | **2.1** | 56.4 GiB | ~0 |

**Story.**

- **Max usable = `gpt-oss:120b` at ~30 tok/s, VRAM-resident** (56.6 GiB inside the
  64 GiB carveout, no GTT spill), full vllm-sr stack co-resident. Being a
  **Mixture-of-Experts** (~5.1B active params/token) it *decodes ~8× faster than the
  dense 70B* while being far larger — 30.4 vs 3.6 tok/s.
- **The boundary is the 64 GiB VRAM carveout** (≈60 GiB usable after runtime
  buffers). Everything at/below 56.6 GiB loaded cleanly and fast; the first rung
  *above* it is Q8-70B (~69 GiB weights).
- **The failure mode is a *soft* CPU layer-offload — not a GTT-spill abort.** The
  oversized Q8-70B does not spill to GTT and does not hard-fail: Ollama caps GPU
  layers to the carveout (VRAM pinned 56.4 GiB, **GTT ~0**) and runs the rest on the
  **CPU** (50/50 split, system RAM +20 GiB), collapsing decode to 2.1 tok/s. Contrast
  **Halo-A**, where the 70B spilled to **GTT and the load aborted (HTTP 500)** — a
  *harder* failure.
- **`use_mmap=false` is required on Halo-B** for the 120B: with mmap the ~68 GB load
  + CPU tensor overrides never finished inside client timeouts ("aborting load");
  no-mmap loads in ~31 s and decodes at ~31 tok/s. Pinning `num_ctx` also stalled the
  load — the model's **default ctx** works.
- **The enlarged GTT (48 GiB) is *not* what raised the ceiling** — GTT stayed ~0 on
  every rung (Ollama/llama.cpp on ROCm 7.2 CPU-offloads instead of using GTT for
  weight overflow). The lever that moved the ceiling **32B → 120B** is **going
  headless to free the whole 64 GiB VRAM carveout**.

Full memory map, tuning steps, and failure-mode detail:
[`halo-b-maxmodel.md`](halo-b-maxmodel.md).

### 11.1 Re-test at 96 GiB VRAM carveout (BIOS 64 → 96 GiB) **[M]**

We later raised Halo-B's BIOS UMA carveout **64 → 96 GiB** (OS-visible system RAM
correspondingly **62 → 30 GiB**) and re-ran the co-resident probe at `num_ctx=4096`. The
result is **counter-intuitive and operationally important**:

- **By default the bigger carveout *regresses* Ollama.** `ollama ps` caps GPU use at **~27
  GiB and CPU-offloads the rest for every big model** — despite `amd-smi` reporting **~69 GiB
  VRAM free** — because Ollama sizes GPU layers to **OS-visible system RAM** (now 30 GiB), not
  the VRAM carveout. So `gpt-oss:120b` drops **30.4 → 5.7 tok/s** (59% CPU-offloaded) vs 64 GiB.
- **Overriding the estimate exploits the carveout.** With **`num_gpu=999` + `use_mmap=false`**
  both models load **100% on GPU**:

| Model | Mode | ollama split | VRAM used | Decode tok/s |
| --- | --- | --- | --- | --- |
| `gpt-oss:120b` | forced | **100% GPU** | **60.5 GiB** | **36.8** (> 64 GiB's 30.4) |
| `llama3.1:70b-instruct-q8_0` (~69 GiB) | forced | **100% GPU** | **70.7 GiB** | **3.0** |

- **Headline: Q8-70B — the first-*unusable* rung at 64 GiB (2.1 tok/s, CPU-offloaded) — is now
  fully VRAM-resident (70.7 GiB, 100% GPU) at 96 GiB.** Decode is still ~3 tok/s (dense-Q8 is
  LPDDR5X **bandwidth-bound** even all-GPU), but it clears the usable floor and no longer
  thrashes the CPU. **~25 GiB headroom** remains → ~90 GiB-weight models should be VRAM-resident
  with the override.
- **Guidance:** for hands-off Ollama keep **64 GiB** (auto-offload works, 120B @ ~30 tok/s, 62
  GiB system RAM free); use **96 GiB only with `num_gpu` + `use_mmap=false`** when you
  specifically need **>60 GiB models VRAM-resident**. Full memory map + before/after:
  [`halo-b-maxmodel.md` → 96 GiB re-test](halo-b-maxmodel.md#96-gib-vram-carveout-re-test).

---

## 12. Perf-per-watt (socket power) **[M]**

Strix Halo is a unified-memory APU with no discrete-GPU rail, so the meaningful
energy figure is **socket graphics-package power** from `rocm-smi --showpower`,
sampled ~1 Hz around a sustained decode. Sampler:
[`perf/power_sampler.py`](../perf/power_sampler.py) (formalized from the throwaway
probe used to gather these numbers). **Idle socket power: ~12–14 W** on both boxes.

| box | model | decode tok/s | mean load W | **tok/s per W** (load) | tok/s per W (net of idle) |
| --- | --- | --- | --- | --- | --- |
| Halo-A | `qwen2.5:7b` | 44.0 | 108 | **0.41** | 0.46 |
| Halo-A | `qwen2.5:32b` | 10.9 | 117 | **0.093** | 0.103 |
| Halo-B | `gpt-oss:120b` (120B MoE) | 30.3 | 102 | **0.30** | 0.34 |

**Story.**

- **Power is roughly constant (~100–120 W) under sustained decode regardless of
  model size** — the socket pins to the same envelope — so **efficiency tracks
  throughput**: the 7B decodes ~4× faster than the dense 32B for the same ~110 W, and
  is therefore **~4.4× more energy-efficient per token** (0.41 vs 0.093 tok/s/W).
- **The MoE 120B is the counter-intuitive win: it is both *bigger* and *more
  efficient per token* than the dense 32B** — 0.30 vs 0.093 tok/s/W (~3.2×) — because
  only ~5.1B of its 120B params are active per token, so it draws similar power
  (~102 W) yet decodes ~3× faster. On this hardware, a well-chosen MoE beats a
  smaller dense model on *both* speed and energy/token.
- Dynamic (load − idle) draw is ~96 W (7B) / ~105 W (32B) / ~90 W (120B); idle sits
  at ~12–14 W, so the box is cheap to leave resident between requests.

```bash
python3 perf/power_sampler.py --model qwen2.5:7b  --max-tokens 128 --out pw-7b.json
python3 perf/power_sampler.py --model gpt-oss:120b --no-mmap --runs 1 \
    --max-tokens 1400 --keep-alive 30m --out pw-120b.json   # 120B MoE on Halo-B
```

---

## 13. Reproduce everything

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

## 14. Honest caveats

- **Two boxes, but asymmetric BIOS.** Both boxes are measured (§4), but the VRAM
  carveout differs (Halo-A 32 GiB, Halo-B 64 GiB), so the model **ceilings** differ
  (§3 vs §11). The co-location *overhead* (§2) is box-independent; the *max model*
  is not.
- **Quant parity (Test 2).** Servers load *different* quantizations of the same
  base; each row records its `quant`. Treat cross-server deltas as
  "this server + this quant on this box" — Lemonade's −7…−11% is mostly that it
  serves `Qwen3-8B-GGUF`, not `qwen2.5-7b`.
- **vLLM is a documented skip, not a data gap.** It is intentionally
  skip-with-reason on gfx1151 (`invalid device function`, §9); every other data row
  in this report is measured **[M]**.
- **The 120B on Halo-B needs headless + `use_mmap=false`.** ~30 tok/s is only
  reachable VRAM-resident on the freed 64 GiB carveout; with mmap the load never
  finished inside client timeouts (§11).
- **Router TTFT is the headline, not decode drop.** If you quote one number from
  this report, quote **+1.4 s TTFT**, mitigated by the semantic cache (§6, rec.
  threshold 0.92).
