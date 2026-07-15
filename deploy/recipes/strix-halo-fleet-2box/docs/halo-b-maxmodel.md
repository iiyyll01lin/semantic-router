# Halo-B — max model under the full vllm-sr topology (headless + enlarged GTT)

Companion to the [performance report](perf-report.md) §3 ("which model spec becomes
unusable"). That section is **Halo-A** (94 GiB unified, GUI up): the ceiling is
`qwen2.5:32b` and a 70B **fails to load** (GTT spill → HTTP 500). This note is the
**Halo-B** counterpart after tuning the box with **OS-only levers** (BIOS unchanged),
measured with the [`maxmodel-sweep.sh`](../perf/maxmodel-sweep.sh) harness.

## TL;DR

On **Halo-B** (Ryzen AI Max+ 395, gfx1151, 128 GiB LPDDR5X, BIOS VRAM carveout **64
GiB**), going **headless** and **enlarging GTT to 48 GiB** (boot-safe kernel module
params) and then running the **full vllm-sr stack co-resident**, the box serves
`gpt-oss:120b` **(120B MoE, MXFP4) at ~30 tok/s** — loaded **entirely in the VRAM
carveout, no GTT spill**. The ceiling under this topology is governed by the **64 GiB
VRAM carveout**: a model whose runtime footprint fits the carveout loads cleanly and is
fast; a footprint **beyond** it is **CPU-offloaded** by Ollama (it caps GPU layers to the
free VRAM and runs the rest on the CPU), so it still "runs" but decode collapses below the
usable floor. Going **headless** — which frees the *whole* 64 GiB carveout — moves the
*reliable* ceiling from **32B (Halo-A) to 120B (Halo-B)**.

This is a **max-model / capacity** note, not the current local default recommendation.
The latest Gemma 4 + `gpt-oss` MMLU-Pro frontier makes **Gemma 4 26B MoE** the default
family: balanced `gemma4:26b-a4b-it-q8_0` (44.6 tok/s, 25.3 GiB, 71.4%), throughput/demo
`gemma4:26b` Q4_K_M (58.4 tok/s, 21.6 GiB, 69.0%), and compact/fast
`gemma4:26b-a4b-it-qat` (65.0 tok/s, 13.8 GiB, 64.3%). `gpt-oss:120b` remains the
120B capacity/reference and big-MoE baseline.

> **Update — BIOS carveout later raised 64 → 96 GiB.** Everything from *Tuning applied*
> down is the **64 GiB** baseline. We then raised the BIOS UMA carveout to **96 GiB** and
> re-ran the probe; the result is nuanced enough to headline up front — see
> [96 GiB VRAM carveout re-test](#96-gib-vram-carveout-re-test) immediately below.



## 96 GiB VRAM carveout re-test

**Setup.** BIOS UMA raised **64 → 96 GiB** (`amd-smi` + sysfs confirm `VRAM total = 96.0
GiB`, GTT 48 GiB), which drops **OS-visible system RAM 62 → 30 GiB**. Headless, full
vllm-sr stack co-resident (router `:8080` up), models probed at `num_ctx=4096`.

**Headline:** the **~69 GiB Q8-70B — *unusable* (CPU-offloaded, 2.1 tok/s) at 64 GiB — now
loads *fully VRAM-resident* at 96 GiB (70.7 GiB, 100% GPU)**. But it only does so when
Ollama's layer estimate is **overridden**: *by default* the bigger carveout is
**counter-productive**, because it shrinks the system RAM that Ollama uses as its budget.

### The Ollama VRAM-budget trap (why the default *regresses*)

At 96 GiB, `ollama ps` **caps GPU use at ~27 GiB and CPU-offloads the rest — for *every*
big model — even though** `amd-smi` **reports ~69 GiB of VRAM *free*.** The cap tracks the **30
GiB OS-visible system RAM**, not the 96 GiB VRAM carveout: on this unified-memory APU
Ollama sizes GPU layers to system memory. So raising the carveout (which *shrinks* system
RAM 62 → 30 GiB) *lowers* Ollama's default budget and pushes models that were VRAM-resident
at 64 GiB into CPU-offload:


| Model                                  | 64 GiB (auto)                         | **96 GiB (auto / default)**                                  | 96 GiB (forced `num_gpu=999`, `use_mmap=false`)              |
| -------------------------------------- | ------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `gpt-oss:120b` (120B MoE)              | vram-fit, 56.6 GiB, **30.4 tok/s**    | **59% CPU-offload, 26.9 GiB VRAM, 5.7 tok/s** ⟵ *regression* | **100% GPU, 60.5 GiB VRAM, 36.8 tok/s**                      |
| `llama3.1:70b-instruct-q8_0` (~69 GiB) | CPU-offload, **2.1 tok/s** (unusable) | ~63% CPU-offload, 27.6 GiB VRAM, CPU-bound (<3 tok/s)        | **100% GPU, 70.7 GiB VRAM, 3.0 tok/s** ⟵ *now VRAM-resident* |




### Forcing full VRAM residency (exploits the 96 GiB carveout)

Overriding Ollama's estimate with `num_gpu=999` **+** `use_mmap=false` (options on
`/api/generate`) makes both models load **100% on GPU**, using the carveout as intended:


| Model                        | ollama split | VRAM used    | GTT | decode tok/s | prefill tok/s |
| ---------------------------- | ------------ | ------------ | --- | ------------ | ------------- |
| `gpt-oss:120b`               | **100% GPU** | **60.5 GiB** | ~0  | **36.8**     | 274           |
| `llama3.1:70b-instruct-q8_0` | **100% GPU** | **70.7 GiB** | ~0  | **3.0**      | 45            |


- `gpt-oss:120b` **is now *faster* than at 64 GiB** — 36.8 vs 30.4 tok/s — fully
  VRAM-resident at 60.5 GiB. (Its ~5.1B active params/token keep the MoE fast.)
- `llama3.1:70b-instruct-q8_0` **fits VRAM-resident at 70.7 GiB (100% GPU)** — the exact rung
  that was *first-unusable* at 64 GiB. Its decode is only **~3 tok/s** even VRAM-resident: a
  dense 70B at Q8 (~69 GiB) is **memory-bandwidth-bound** on LPDDR5X, so residency removes the
  *hard* CPU-offload penalty but not the intrinsic bandwidth ceiling. It clears the 3 tok/s
  usable floor — just barely.



### New ceiling at 96 GiB (forced-residency sweep)

To find where 100% residency *actually* tops out, a dedicated forced sweep
(`NUM_GPU=999 USE_MMAP=false NUM_CTX=4096`, full vllm-sr stack co-resident) climbed one
**bigger** dense rung — `qwen2.5:72b-instruct-q8_0` (~72 GiB) — beyond the Q8-70B
([`maxmodel-sweep-halo-b-96g-forced.json`](../perf/maxmodel-sweep-halo-b-96g-forced.json)):


| Rung                         | Type                   | Sweep verdict              | Decode tok/s | Peak VRAM | Peak GTT | Peak sys RAM |
| ---------------------------- | ---------------------- | -------------------------- | ------------ | --------- | -------- | ------------ |
| `gpt-oss:120b`               | 120B MoE MXFP4         | **usable** / vram-fit      | **36.7**     | 60.5 GiB  | ~0       | 12.1 GiB     |
| `llama3.1:70b-instruct-q8_0` | 70B dense Q8           | **usable** / vram-fit      | **3.04**     | 70.7 GiB  | ~0       | 12.1 GiB     |
| `qwen2.5:72b-instruct-q8_0`  | 72B dense Q8 (~72 GiB) | **unusable(slow-spill)** * | **2.94**     | 72.6 GiB  | ~0       | 12.2 GiB     |


- **At 96 GiB, residency is *not* the ceiling — dense-Q8 decode bandwidth is.** All three
  rungs stayed **VRAM-resident**: GTT ~0 **and** system RAM flat at ~12 GiB (identical to the
  100%-GPU MoE rung), i.e. **nothing CPU-offloaded**. `qwen2.5:72b-instruct-q8_0` sat fully in
  **72.6 GiB of VRAM with ~23 GiB carveout headroom to spare**, yet decoded **2.94 tok/s** — a
  hair under the 3 tok/s "usable" floor.
- **The `unusable(slow-spill)` / `vram-exceeded` label is a *speed-floor* artifact, not an
  overflow.** The sweep classifies any <3 tok/s rung that way, but the resource evidence (GTT
  ~0, system RAM unchanged from the all-GPU rungs) shows the 72B **did fit VRAM**; it is simply
  LPDDR5X **bandwidth-bound**. So it gets *slow* before it stops *fitting*.
- **Net ceilings at 96 GiB:** the largest **VRAM-resident** footprint has since been measured at
  **94.59 GiB of weights** — `mixtral:8x22b-instruct-v0.1-q5_K_M` (141B MoE) loads 100%
  VRAM-resident at 94.59 GiB (GTT 0.05, only ~1.4 GiB below the 96 GiB carveout; see the
  [quant frontier](#quantization-frontier-96-gib-forced-resident) below), replacing this sweep's
  72.6 GiB (`qwen2.5:72b-instruct-q8_0`) as the largest *measured* resident footprint — and
  **superseding the old ~90 GiB extrapolation with a real measurement** (the residency break now
  sits at/above the carveout itself); the ***usable* (≥3 tok/s) dense-Q8** ceiling is **~70 GiB**
  (`llama3.1:70b-instruct-q8_0`, 3.04 tok/s). A **MoE** (`gpt-oss:120b`, ~5.1B active) stays
  fast (36.7 tok/s) at any of these footprints.
- `mixtral:8x22b` **was not pulled *for this dense-Q8 forced sweep*:** `qwen2.5:72b-instruct-q8_0`
already pinned down the residency headroom and the dense-Q8 usable edge here, and skipping the
extra download kept the smartcity-down maintenance window short. (It was pulled and measured
separately in the [quant frontier](#quantization-frontier-96-gib-forced-resident) below, whose
Q5 rung set the 94.59 GiB resident record cited above.)
- **Memory map (96 GiB):** VRAM **96.0 GiB** total (~69 GiB free at idle), GTT **48 GiB**,
system RAM **31 GiB**. GTT stayed ~0 throughout (ROCm/llama.cpp does not use GTT for weight
overflow — consistent with the 64 GiB findings).



### Decision: keep the 96 GiB carveout

**We keep Halo-B at the 96 GiB VRAM carveout.** Rationale and trade-off:

- **Why keep it:** only 96 GiB can hold the **>60 GiB models fully VRAM-resident** we now want
available for capacity/reference work — `gpt-oss:120b` (60.5 GiB, and *faster* than at 64 GiB:
36.7 vs 30.4 tok/s), `llama3.1:70b-instruct-q8_0` (70.7 GiB), `qwen2.5:72b-instruct-q8_0`
(72.6 GiB), `mixtral:8x22b-instruct-v0.1-q5_K_M` (94.59 GiB, the largest measured — ~1.4 GiB
below the carveout) and right up to the 96 GiB carveout edge. **64 GiB physically cannot** — its
usable ceiling was ~56 GiB and Q8-70B was CPU-offloaded to 2.1 tok/s there. The local/default
Gemma 4 26B MoE rungs fit well below this ceiling; the 96 GiB carveout is retained for the
capacity story.
- **Cost:** OS-visible system RAM drops to **30 GiB**. Verified acceptable — the 14-container
**smartcity** stack runs co-resident and healthy in that budget, and our CPU-pinned vllm-sr
stack adds only ~8.5 GiB of system RAM (weights live in the VRAM carveout, not system RAM).
- **Caveat:** at 96 GiB you **must** override Ollama's auto layer estimate (it sizes to the 30
GiB system RAM, not the carveout). If *hands-off* Ollama matters more than >60 GiB residency,
revert to 64 GiB (below).



### Full-residency usage — capacity/reference `-vram` variants

Make full residency the **default for a capacity/reference tag** with the helper
[`perf/make-vram-resident-models.sh`](../perf/make-vram-resident-models.sh). It derives a
**non-destructive** `<tag>-vram` variant that bakes `PARAMETER num_gpu 999` + `PARAMETER use_mmap false` — **verified persisted on Ollama 0.30.10** (`ollama show --modelfile` keeps
both; `ollama ps` reports **100% GPU**):

```bash
# create gpt-oss:120b-vram + llama3.1:70b-instruct-q8_0-vram and verify 100% GPU:
bash perf/make-vram-resident-models.sh
ollama run gpt-oss:120b-vram "hello"   # loads 100% GPU, no per-request options needed
```

- **Big models: always use the** `-vram` **variant** (or pass `num_gpu`/`use_mmap=false` per
request). The **original tags are left untouched**, so the auto behavior stays available for
A/Bs. (The helper derives via `FROM <tag>` — deriving from the raw blob path makes 0.30.10
re-validate the GGUF and fail for MXFP4/Q8, and `-f -`/stdin is not accepted.)
- Ad-hoc sweeps can force it fleet-wide via the harness knobs: `NUM_GPU=999 USE_MMAP=false NUM_CTX=4096 bash perf/maxmodel-sweep.sh` (default behavior is unchanged when they are unset).



### Revert to the 64 GiB carveout (documented, not executed)

To restore *hands-off* Ollama (auto layer estimate works again, ~62 GiB system RAM), lower the
BIOS UMA carveout back to 64 GiB. This is a **firmware** change — the carveout is **not** an OS
lever, so there is no `sysfs`/kernel path:

1. Reboot into **UEFI/BIOS setup**.
2. Set the **UMA Frame Buffer Size / VRAM carveout** back to **64 GiB** (inverse of the earlier
  64 → 96 GiB change; the menu name varies, e.g. *Advanced → GFX Configuration / UMA*).
3. Save and reboot. Confirm with `amd-smi metric --mem-usage` (VRAM total → 64 GiB) and
  `cat /sys/class/drm/card1/device/mem_info_vram_total` (→ `68719476736`).

After reverting, drop the variants (`ollama rm <tag>-vram`): at 64 GiB the auto layer estimate
again keeps `gpt-oss:120b` VRAM-resident at ~30 tok/s **without** the override.

Reproduce (on Halo-B, stack up):

```bash
# Default at 96 GiB (shows the auto CPU-offload):
SWEEP_TAGS="gpt-oss:120b llama3.1:70b-instruct-q8_0" NUM_CTX=4096 bash perf/maxmodel-sweep.sh
# Forced full-GPU residency sweep (the harness now takes NUM_GPU/USE_MMAP -> this is the
# forced-residency ceiling run whose JSON is maxmodel-sweep-halo-b-96g-forced.json):
NUM_GPU=999 USE_MMAP=false NUM_CTX=4096 \
  SWEEP_TAGS="gpt-oss:120b llama3.1:70b-instruct-q8_0 qwen2.5:72b-instruct-q8_0" \
  BOX=halo-b OUT=perf/maxmodel-sweep-halo-b-96g-forced.json bash perf/maxmodel-sweep.sh
# Or force a single model directly on the backend:
curl -s http://localhost:11434/api/generate -d \
  '{"model":"llama3.1:70b-instruct-q8_0","prompt":"hello",
    "options":{"num_gpu":999,"use_mmap":false,"num_ctx":4096}}'
```



## Tuning applied (OS-only, needs sudo + one reboot; BIOS stays at 64 GiB VRAM)

```bash
# 1. Headless: stop GNOME/gdm so the GUI holds no VRAM and no system RAM.
sudo cp -a /etc/default/grub /etc/default/grub.bak.$(date +%Y%m%d-%H%M%S)
sudo systemctl set-default multi-user.target

# 2. Enlarge the GTT overflow pool to ~48 GiB via boot-safe module params.
#    GRUB_CMDLINE_LINUX_DEFAULT += amdgpu.gttsize=49152 ttm.pages_limit=12582912
sudo sed -i 's|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT="quiet splash amdgpu.gttsize=49152 ttm.pages_limit=12582912"|' /etc/default/grub
sudo update-grub
sudo reboot
```

`amdgpu.gttsize` / `ttm.pages_limit` are **module params** (not a kernel feature flag),
so they cannot prevent boot. `ttm.pages_limit=12582912` pages × 4 KiB = **48 GiB**.

## Memory map — before vs after (idle)


| Phase                                    | GUI              | VRAM total / used | GTT total / used     | System RAM used    |
| ---------------------------------------- | ---------------- | ----------------- | -------------------- | ------------------ |
| **Before** (graphical.target, pre-tune)  | on (gnome-shell) | 64.00 / 0.16 GiB  | **31.22** / 0.03 GiB | ~13 GiB (w/ stack) |
| **After** (headless, stack **down**)     | off              | 64.00 / 0.14 GiB  | **48.00** / 0.02 GiB | **4.7 GiB**        |
| **After** (headless, stack **up**, idle) | off              | 64.00 / 0.14 GiB  | **48.00** / 0.02 GiB | ~12 GiB            |


- The tuning **enlarges GTT 31.2 → 48.0 GiB** and confirms the **64 GiB VRAM carveout is
~fully free** headless (0.14 GiB used). On this box the GUI at the gdm login screen was
already near-idle on VRAM, so the dominant win is (a) the **enlarged GTT ceiling** and
(b) freeing **system RAM** (headless idle drops to 4.7 GiB), which is where the GTT
overflow pool and the CPU-pinned router stack both live.
- **Unified-memory model:** VRAM (64 GiB, dedicated carveout) is separate from the
**62.4 GiB OS-visible system RAM**; **GTT is carved out of system RAM** on demand, so
the GTT ceiling (48 GiB) competes with the OS + the router stack for those 62.4 GiB.



## vllm-sr stack footprint (co-resident, CPU-pinned classifiers)

Full stack UP = 9 containers (router, Envoy, dashboard, sim, Grafana, Prometheus,
Jaeger, Postgres, Redis) + Ollama. Idle container RAM (`docker stats`):


| Component                                                                  | Unified RAM             |
| -------------------------------------------------------------------------- | ----------------------- |
| Router (Go + CPU-pinned ONNX classifiers)                                  | **7.9 GiB**             |
| Envoy / dashboard / sim / Grafana / Prometheus / Jaeger / Postgres / Redis | ~0.6 GiB                |
| **Total stack footprint**                                                  | **≈8.5 GiB** system RAM |


Because `VLLM_SR_AMD_PRESERVE_CPU=1` pins the classifiers to CPU, the stack lands in
**system RAM**, not VRAM — so it does **not** shrink the 64 GiB VRAM carveout the models
load into; it taxes the system-RAM budget that also backs GTT.

## Ascending max-model sweep (stack co-resident)

`bash maxmodel-sweep.sh` — each rung: pull → sample VRAM/GTT/system while a real decode
runs → classify. `usable` = decode ≥ `OOM_MIN_TPS` (3); `gtt-spill` = peak GTT above 2
GiB (weights spilled past the carveout). All rungs run with the router **UP**.


| Rung                              | Type                       | Verdict                  | Mem mode          | Decode tok/s | Peak VRAM    | Peak GTT | TTFT   |
| --------------------------------- | -------------------------- | ------------------------ | ----------------- | ------------ | ------------ | -------- | ------ |
| `qwen2.5:32b`                     | 32B dense Q4               | **usable**               | vram-fit          | **10.9**     | 26.7 GiB     | ~0       | 231 ms |
| `llama3.1:70b`                    | 70B dense Q4               | **usable**               | vram-fit          | **3.6**      | 48.2 GiB     | ~0       | 461 ms |
| `llama3.1:70b` @ `num_ctx=131072` | 70B + max KV               | **usable**               | vram-fit          | **3.9**      | 55.9 GiB     | ~0       | —      |
| `gpt-oss:120b`                    | **120B MoE MXFP4**         | **usable**               | vram-fit          | **30.4**     | **56.6 GiB** | ~0       | 4.3 s  |
| `llama3.1:70b-instruct-q8_0`      | **70B dense Q8 (~69 GiB)** | **unusable(slow-spill)** | **vram-exceeded** | **2.1**      | 56.4 GiB     | ~0       | —      |


Notes:

- `gpt-oss:120b` **is the 64 GiB capacity/reference max usable model** and, being a
**Mixture-of-Experts** (~5.1B active params/token), it *decodes faster than the dense 70B* while
being far larger — 30 tok/s vs 3.6 tok/s. Its 56.6 GiB footprint sits inside the 64 GiB carveout.
- `num_ctx` is **not** a reliable lever to force a spill here: Ollama **caps the KV-cache
allocation**, so even `num_ctx=131072` leaves the 70B at ~56 GiB (vram-fit). Forcing a
spill therefore requires a model whose **weights alone** exceed the carveout.



## Ceiling + failure mode

- **Max usable capacity/reference model under this topology:** `gpt-oss:120b` (120B MoE, MXFP4)
at **~30 tok/s**, VRAM-resident (no GTT spill), full vllm-sr stack co-resident.
- **The boundary is the 64 GiB VRAM carveout** (≈60 GiB usable for weights after runtime
buffers). Everything at/below `gpt-oss:120b`'s 56.6 GiB loaded cleanly and fast
(vram-fit); the first rung **above** the carveout is `llama3.1:70b-instruct-q8_0`
(~69 GiB weights).
- **The failure mode is CPU layer-offload — not a GTT-spill abort.** The oversized Q8-70B
does **not** spill into GTT and does **not** hard-fail: `ollama ps` shows it loaded
**50%/50% CPU/GPU**. The runtime caps GPU layers to what fits the carveout (VRAM pinned
at 56.4 GiB, **GTT ~0**) and offloads the remaining layers to **CPU / system RAM** (idle
→ **+20 GiB**), collapsing decode to **2.1 tok/s** (< the 3 tok/s floor). So the boundary
here is **soft**: a bigger-than-carveout model still runs, but at CPU-bound speed, which
the sweep flags `unusable(slow-spill)`. Contrast Halo-A, where the spill went to **GTT**
and the load **aborted** (HTTP 500) — a *harder* failure.
- **The enlarged GTT (48 GiB) is not what raised the ceiling.** Across every rung GTT
stayed ~0: Ollama/llama.cpp on ROCm 7.2 does **not** use GTT for weight overflow, it
  CPU-offloads instead. The lever that moved the ceiling **32B → 120B** is **headless
  freeing the full 64 GiB VRAM carveout**. The GTT enlargement is boot-safe headroom (and
  may matter for other runtimes, e.g. Lemonade/vLLM), but Ollama did not exploit it here.
- **Empirical 64 GiB capacity ceiling:** usable up to `gpt-oss:120b` (56.6 GiB, all-GPU, ~30 tok/s);
  first unusable at `llama3.1:70b-instruct-q8_0` (~69 GiB, 50% CPU-offloaded, 2.1 tok/s). The
  max-model reference under the full co-resident topology is `gpt-oss:120b`; the current
  local/default model family is Gemma 4 26B MoE.



## Contrast with Halo-A (perf-report §3)


|                 | Halo-A (94 GiB unified, GUI up)                  | Halo-B (headless, 64 GiB VRAM + 48 GiB GTT)               |
| --------------- | ------------------------------------------------ | --------------------------------------------------------- |
| Max usable      | `qwen2.5:32b` (~10.7 tok/s)                      | `gpt-oss:120b` (~30 tok/s)                                |
| 70B             | **fails to load** (GTT spill 48.9 GB → HTTP 500) | **usable, vram-fit** (48.2 GiB, 3.6 tok/s)                |
| Governing limit | unified budget − stack; GTT spill aborts (hard)  | 64 GiB VRAM carveout; overflow → CPU offload (soft, slow) |




## Reproduce

```bash
# On Halo-B, with the vllm-sr stack up (gateway-bring-up.sh):
SWEEP_TAGS="qwen2.5:32b llama3.1:70b gpt-oss:120b" bash perf/maxmodel-sweep.sh
# Force a footprint past the VRAM carveout to characterize the overflow (CPU offload):
SWEEP_TAGS="llama3.1:70b-instruct-q8_0" bash perf/maxmodel-sweep.sh   # ~69 GiB weights
```



## Quantization frontier (96 GiB, forced-resident)

With the sweep fixed to report residency truthfully (`/api/ps` `size_vram/size`), a
controlled quant sweep on ONE dense family (`llama3.1:70b-instruct`, forced
`num_gpu=999`/`use_mmap=false`, `num_ctx=4096`) plus three big-MoE rungs (mixtral 8x22b Q3_K_M +
Q4_K_M + Q5_K_M) and `gpt-oss:120b` — plus the six **Gemma 4 [M]** rungs (26B A4B MoE + 31B
dense, each Q4_K_M / Q8_0 / int4-QAT) added later under the same forced-resident harness — each
scored for both decode speed (`maxmodel-sweep.sh`) and MCQ accuracy (`quant-quality.py`, 42
stratified MMLU-Pro questions). **All rungs were 100% VRAM-resident** (`vram-fit`,
`size_vram/size`=1.0):


| Model (quant)                                   | Peak VRAM     | Decode tok/s | MMLU-Pro (42Q)    | Verdict           |
| ----------------------------------------------- | ------------- | ------------ | ----------------- | ----------------- |
| `llama3.1:70b-instruct-q4_K_M`                  | 41.0 GiB      | **5.1**      | **52.4%**         | usable / vram-fit |
| `llama3.1:70b-instruct-q5_K_M`                  | 47.8 GiB      | 4.4          | 50.0%             | usable / vram-fit |
| `llama3.1:70b-instruct-q6_K`                    | 55.0 GiB      | 3.9          | 50.0%             | usable / vram-fit |
| `llama3.1:70b-instruct-q8_0`                    | 70.7 GiB      | 3.0          | 50.0%             | usable / vram-fit |
| `mixtral:8x22b-instruct-v0.1-q3_K_M` (141B MoE) | 64.6 GiB      | **10.8**     | 42.9%             | usable / vram-fit |
| `mixtral:8x22b-instruct-v0.1-q4_K_M` (141B MoE) | 81.2 GiB      | 9.03         | 42.86% (18/42)    | usable / vram-fit |
| `mixtral:8x22b-instruct-v0.1-q5_K_M` (141B MoE) | **94.59 GiB** | **7.80**     | **45.2% (19/42)** | usable / vram-fit |
| `gpt-oss:120b` (120B MoE MXFP4) [M]             | 60.5 GiB      | **~36.5**    | 64.3% (27/42)     | usable / vram-fit |
| `gemma4:26b` (25B MoE, Q4_K_M) [M]              | 21.6 GiB      | **58.4**     | 69.0% (29/42)     | usable / vram-fit |
| `gemma4:26b-a4b-it-q8_0` (25B MoE) [M]          | 25.3 GiB      | 44.6         | 71.4% (30/42)     | usable / vram-fit |
| `gemma4:26b-a4b-it-qat` (25B MoE) [M]           | 13.8 GiB      | **65.0**     | 64.3% (27/42)     | usable / vram-fit |
| `gemma4:31b` (31B dense, Q4_K_M) [M]            | 19.4 GiB      | 11.3         | 73.8% (31/42)     | usable / vram-fit |
| `gemma4:31b-it-q8_0` (31B dense) [M]            | 32.4 GiB      | 7.1          | 76.2% (32/42)     | usable / vram-fit |
| `gemma4:31b-it-qat` (31B dense) [M]             | 18.5 GiB      | **12.3**     | **78.6% (33/42)** | usable / vram-fit |


Per-rung data: [`perf/quant-frontier/`](../perf/quant-frontier/) (`sweep-*.json` + `quality-*.json`).

- **Decode is LPDDR5X-bandwidth-bound — lower quant is monotonically faster.** Same 70B
  weights, decode climbs as the footprint shrinks: Q8 **3.0** -> Q6 **3.9** -> Q5 **4.4** ->
  Q4 **5.1 tok/s** (Q4 is ~**1.7x** Q8), exactly as `tok/s ~= mem-bandwidth / bytes-per-token`
  predicts.
- **Q4 is the dense sweet spot.** Across Q4->Q8 the MMLU-Pro accuracy is flat within noise
  (52.4 / 50.0 / 50.0 / 50.0%) while Q4 decodes ~1.7x faster **and** uses ~30 GiB less VRAM.
  For a dense 70B on this box, prefer **`Q4_K_M`**, not Q8. (42 questions is a small,
  indicative sample — treat +/-~7pp as noise, not a real quality ranking.)
- **MoE is "big and fast".** `mixtral:8x22b` (141B total, ~39B active) sits above the dense line
  at every rung: Q3_K_M in **64.6 GiB** decodes **10.8 tok/s** (~2x the dense 70B-Q4, ~3.6x the
  dense 70B-Q8), and **Q5_K_M is the largest real footprint measured — 94.59 GiB, 100% VRAM-resident,
  7.80 tok/s, ~1.4 GiB below the carveout** — because only the active experts are read per token. Its
  MCQ score (45.2%, 19/42) reflects the base model/quant, not the architecture; the speed is the point.
- **`gpt-oss:120b` quality now measured [M].** The resident 120B MoE keeps its role as a fast
  120B capacity/reference point (**~36.5 tok/s** at 60.5 GiB) and scores **64.3% (27/42)** on the
  same 42Q MMLU-Pro slice — above the older mixtral rungs and in the modern MoE band, but no longer
  the best local/default choice versus Gemma 4 26B.
- **Gemma 4 [M] — the 25B MoE is the speed win, `31b-it-qat` the quality standout.** `gemma4:26b`
  (25B total, ~3.8B active) decodes **58.4 tok/s** at 21.6 GiB — **the fastest MoE in this frontier**
  (past `gpt-oss:120b` ~36.5 and every mixtral rung) — and `gemma4:26b-a4b-it-qat` is the fastest
  overall at **65.0 tok/s** in just 13.8 GiB, because a small-total MoE reads only ~3.8B params/token.
  The dense `gemma4:31b` is bandwidth-bound like llama (Q8 7.1 < Q4 11.3, monotonic in footprint) but
  scores a little higher on MMLU-Pro (73.8–78.6% vs the MoE's 64.3–71.4%); **`gemma4:31b-it-qat` is the
  standout — 78.6% (33/42), the highest 42Q score here, compact at 18.5 GiB and the fastest dense rung
  at 12.3 tok/s**. 42Q is a small indicative sample (±~7 pp), so treat these as *speed-at-footprint +
  modern MoE-vs-dense*, not a precise MMLU ranking.
- **Default conclusion:** use Gemma 4 26B MoE for local/default serving: balanced
  `gemma4:26b-a4b-it-q8_0`, throughput/demo `gemma4:26b` Q4_K_M, compact/fast
  `gemma4:26b-a4b-it-qat`. Keep `gemma4:31b-it-qat` for quality-only local runs and
  `gpt-oss:120b` for 120B capacity/reference comparisons.

- **Candidate sweep update (Halo-B, 2026-07-15) [M].** A broad P0 + capped P1/P2 sweep did **not** displace Gemma 4. The best speed candidate, `qwen3-coder:30b`, hit **71.0 tok/s** in **18.1 GiB** but only **54.8% (23/42)**. `qwen3-next:80b` was fast enough for default consideration (**49.6 tok/s**, **47.4 GiB**) but scored **61.9% (26/42)**. `qwen3.6:27b` matched the Gemma Q4 quality sample (**69.0%**, 29/42) but was much slower (**13.5 tok/s**) and inefficient (**0.082 tok/s/W**). Lower-priority measured candidates also missed the default bar (`mistral-small:24b` **15.2 tok/s / 54.8%**, `deepseek-r1:32b` **11.0 tok/s / 50.0%**); EXAONE/OpenThinker/Magistral/Phi produced speed/power but no quality JSON under the 30-minute cap, EXAONE is research-only/non-commercial, GLM-4.5-Air and DeepSeek-R1 70B were skipped to keep the sweep bounded, and Falcon-H1 manifests were unavailable. Raw data and skip notes: [`candidate-summary-halo-b.json`](../perf/quant-frontier/candidate-summary-halo-b.json) / [`candidate-summary-halo-b.md`](../perf/quant-frontier/candidate-summary-halo-b.md).

- **Sweep verdict fix validated.** Every rung reported `vram-fit` / `usable` /
  `size_vram/size`=1.0 — including the 3.0 tok/s Q8-70B, which the old logic mislabeled
  `unusable(slow-spill)`.

**Limitation.** The intended 235B MoE rung (`qwen3:235b-a22b-q2_K`) is **not a published Ollama
tag** (404), so the run used `mixtral:8x22b` (141B) instead. The largest *measured* resident
footprint here is now **94.59 GiB** (`mixtral:8x22b-...-q5_K_M`, 100% VRAM-resident, 7.80 tok/s),
which replaces the earlier 81.2 GiB (Q4) and 70.7 GiB (Q8-70B) top rungs; at only ~1.4 GiB below
the 96 GiB carveout it **supersedes the old ~90 GiB extrapolation with a real measurement**, so
the residency break now sits at/above the carveout itself.