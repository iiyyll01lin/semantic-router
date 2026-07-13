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
**`gpt-oss:120b` (120B MoE, MXFP4) at ~30 tok/s** — loaded **entirely in the VRAM
carveout, no GTT spill**. The ceiling under this topology is governed by the **64 GiB
VRAM carveout**: a model whose runtime footprint fits the carveout loads cleanly and is
fast; a footprint **beyond** it is **CPU-offloaded** by Ollama (it caps GPU layers to the
free VRAM and runs the rest on the CPU), so it still "runs" but decode collapses below the
usable floor. Going **headless** — which frees the *whole* 64 GiB carveout — moves the
*reliable* ceiling from **32B (Halo-A) to 120B (Halo-B)**.

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

At 96 GiB, **`ollama ps` caps GPU use at ~27 GiB and CPU-offloads the rest — for *every*
big model — even though `amd-smi` reports ~69 GiB of VRAM *free*.** The cap tracks the **30
GiB OS-visible system RAM**, not the 96 GiB VRAM carveout: on this unified-memory APU
Ollama sizes GPU layers to system memory. So raising the carveout (which *shrinks* system
RAM 62 → 30 GiB) *lowers* Ollama's default budget and pushes models that were VRAM-resident
at 64 GiB into CPU-offload:

| Model | 64 GiB (auto) | **96 GiB (auto / default)** | 96 GiB (forced `num_gpu=999`, `use_mmap=false`) |
| --- | --- | --- | --- |
| `gpt-oss:120b` (120B MoE) | vram-fit, 56.6 GiB, **30.4 tok/s** | **59% CPU-offload, 26.9 GiB VRAM, 5.7 tok/s** ⟵ *regression* | **100% GPU, 60.5 GiB VRAM, 36.8 tok/s** |
| `llama3.1:70b-instruct-q8_0` (~69 GiB) | CPU-offload, **2.1 tok/s** (unusable) | ~63% CPU-offload, 27.6 GiB VRAM, CPU-bound (<3 tok/s) | **100% GPU, 70.7 GiB VRAM, 3.0 tok/s** ⟵ *now VRAM-resident* |

### Forcing full VRAM residency (exploits the 96 GiB carveout)

Overriding Ollama's estimate with **`num_gpu=999` + `use_mmap=false`** (options on
`/api/generate`) makes both models load **100% on GPU**, using the carveout as intended:

| Model | ollama split | VRAM used | GTT | decode tok/s | prefill tok/s |
| --- | --- | --- | --- | --- | --- |
| `gpt-oss:120b` | **100% GPU** | **60.5 GiB** | ~0 | **36.8** | 274 |
| `llama3.1:70b-instruct-q8_0` | **100% GPU** | **70.7 GiB** | ~0 | **3.0** | 45 |

- **`gpt-oss:120b` is now *faster* than at 64 GiB** — 36.8 vs 30.4 tok/s — fully
  VRAM-resident at 60.5 GiB. (Its ~5.1B active params/token keep the MoE fast.)
- **`llama3.1:70b-instruct-q8_0` fits VRAM-resident at 70.7 GiB (100% GPU)** — the exact rung
  that was *first-unusable* at 64 GiB. Its decode is only **~3 tok/s** even VRAM-resident: a
  dense 70B at Q8 (~69 GiB) is **memory-bandwidth-bound** on LPDDR5X, so residency removes the
  *hard* CPU-offload penalty but not the intrinsic bandwidth ceiling. It clears the 3 tok/s
  usable floor — just barely.

### New ceiling at 96 GiB

- **The VRAM-resident ceiling rises to ~90 GiB of weights.** Q8-70B proves **70.7 GiB
  VRAM-resident** with ~**25 GiB headroom** left in the 96 GiB carveout, so models up to
  **~90 GiB weights** should load all-GPU with the override (not pulled here — headroom is
  measured, not a bigger rung).
- **Memory map (96 GiB):** VRAM **96.0 GiB** total (~69 GiB free at idle), GTT **48 GiB**,
  system RAM **30 GiB**. GTT stayed ~0 throughout (ROCm/llama.cpp does not use GTT for weight
  overflow — consistent with the 64 GiB findings).

### Practical guidance (which carveout to run)

- **For hands-off Ollama, 64 GiB is the better carveout:** auto-offload works and
  `gpt-oss:120b` runs VRAM-resident at ~30 tok/s with **62 GiB system RAM** to spare.
- **96 GiB pays off only with the override** (`num_gpu`, `use_mmap=false`) *and* costs system
  RAM (30 GiB) — worth it when you specifically need **>60 GiB models VRAM-resident** (e.g.
  Q8-70B or an ~80–90 GiB model). Do **not** rely on Ollama's defaults there.

Reproduce (on Halo-B, stack up):

```bash
# Default at 96 GiB (shows the auto CPU-offload):
SWEEP_TAGS="gpt-oss:120b llama3.1:70b-instruct-q8_0" NUM_CTX=4096 bash perf/maxmodel-sweep.sh
# Forced full-GPU residency (exploits the 96 GiB carveout):
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

| Phase | GUI | VRAM total / used | GTT total / used | System RAM used |
| --- | --- | --- | --- | --- |
| **Before** (graphical.target, pre-tune) | on (gnome-shell) | 64.00 / 0.16 GiB | **31.22** / 0.03 GiB | ~13 GiB (w/ stack) |
| **After** (headless, stack **down**) | off | 64.00 / 0.14 GiB | **48.00** / 0.02 GiB | **4.7 GiB** |
| **After** (headless, stack **up**, idle) | off | 64.00 / 0.14 GiB | **48.00** / 0.02 GiB | ~12 GiB |

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

| Component | Unified RAM |
| --- | --- |
| Router (Go + CPU-pinned ONNX classifiers) | **7.9 GiB** |
| Envoy / dashboard / sim / Grafana / Prometheus / Jaeger / Postgres / Redis | ~0.6 GiB |
| **Total stack footprint** | **≈8.5 GiB** system RAM |

Because `VLLM_SR_AMD_PRESERVE_CPU=1` pins the classifiers to CPU, the stack lands in
**system RAM**, not VRAM — so it does **not** shrink the 64 GiB VRAM carveout the models
load into; it taxes the system-RAM budget that also backs GTT.

## Ascending max-model sweep (stack co-resident)

`bash maxmodel-sweep.sh` — each rung: pull → sample VRAM/GTT/system while a real decode
runs → classify. `usable` = decode ≥ `OOM_MIN_TPS` (3); `gtt-spill` = peak GTT above 2
GiB (weights spilled past the carveout). All rungs run with the router **UP**.

| Rung | Type | Verdict | Mem mode | Decode tok/s | Peak VRAM | Peak GTT | TTFT |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `qwen2.5:32b` | 32B dense Q4 | **usable** | vram-fit | **10.9** | 26.7 GiB | ~0 | 231 ms |
| `llama3.1:70b` | 70B dense Q4 | **usable** | vram-fit | **3.6** | 48.2 GiB | ~0 | 461 ms |
| `llama3.1:70b` @ `num_ctx=131072` | 70B + max KV | **usable** | vram-fit | **3.9** | 55.9 GiB | ~0 | — |
| `gpt-oss:120b` | **120B MoE MXFP4** | **usable** | vram-fit | **30.4** | **56.6 GiB** | ~0 | 4.3 s |
| `llama3.1:70b-instruct-q8_0` | **70B dense Q8 (~69 GiB)** | **unusable(slow-spill)** | **vram-exceeded** | **2.1** | 56.4 GiB | ~0 | — |

Notes:
- **`gpt-oss:120b` is the max usable model** and, being a **Mixture-of-Experts**
  (~5.1B active params/token), it *decodes faster than the dense 70B* while being far
  larger — 30 tok/s vs 3.6 tok/s. Its 56.6 GiB footprint sits inside the 64 GiB carveout.
- `num_ctx` is **not** a reliable lever to force a spill here: Ollama **caps the KV-cache
  allocation**, so even `num_ctx=131072` leaves the 70B at ~56 GiB (vram-fit). Forcing a
  spill therefore requires a model whose **weights alone** exceed the carveout.

## Ceiling + failure mode

- **Max usable model under this topology: `gpt-oss:120b`** (120B MoE, MXFP4) at **~30
  tok/s**, VRAM-resident (no GTT spill), full vllm-sr stack co-resident.
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
- **Empirical ceiling:** usable up to **`gpt-oss:120b`** (56.6 GiB, all-GPU, ~30 tok/s);
  first unusable at **`llama3.1:70b-instruct-q8_0`** (~69 GiB, 50% CPU-offloaded, 2.1
  tok/s). The interactive max under the full co-resident topology is **`gpt-oss:120b`**.

## Contrast with Halo-A (perf-report §3)

| | Halo-A (94 GiB unified, GUI up) | Halo-B (headless, 64 GiB VRAM + 48 GiB GTT) |
| --- | --- | --- |
| Max usable | **`qwen2.5:32b`** (~10.7 tok/s) | **`gpt-oss:120b`** (~30 tok/s) |
| 70B | **fails to load** (GTT spill 48.9 GB → HTTP 500) | **usable, vram-fit** (48.2 GiB, 3.6 tok/s) |
| Governing limit | unified budget − stack; GTT spill aborts (hard) | 64 GiB VRAM carveout; overflow → CPU offload (soft, slow) |

## Reproduce

```bash
# On Halo-B, with the vllm-sr stack up (gateway-bring-up.sh):
SWEEP_TAGS="qwen2.5:32b llama3.1:70b gpt-oss:120b" bash perf/maxmodel-sweep.sh
# Force a footprint past the VRAM carveout to characterize the overflow (CPU offload):
SWEEP_TAGS="llama3.1:70b-instruct-q8_0" bash perf/maxmodel-sweep.sh   # ~69 GiB weights
```
