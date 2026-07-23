# Agentic Context Window — Customer Evidence Brief

_Measured 2026-07-22/23 on `demo-002`: HP Z2 Mini G1a, AMD Ryzen AI
MAX+ PRO 395 / Radeon 8060S (gfx1151), 64 GiB VRAM carveout, ROCm 7.0._

Backend: Ollama 0.32.1 ROCm, `gemma4:26b-a4b-it-q8_0` (Q8_0),
`OLLAMA_NUM_PARALLEL=1`. The backend was configured and verified loaded at
**65,536 tokens**, with **0 restarts / 0 OOM** during the campaign.

## Context ladder — do not collapse these values

- **Declared metadata:** 131,072 / 262,144. This was not proven on this backend.
- **Backend configured and loaded-verified:** 65,536 (`OLLAMA_CONTEXT_LENGTH`
  and live `ollama ps`, 100% GPU).
- **Maximum tested input:** 65,152 tokens. With 256 output tokens and 128
  reserved headroom, the required total was exactly **65,536 tokens**.

## Four proofs (honest status)

### 1. Capacity — PARTIAL PASS

- **17 cells / 174 measured requests:** 174/174 HTTP successes, backend-reported
  prompt usage exactly matched each target for every cold/warm cohort, and
  150/174 responses returned the required marker.
- **7/17 cells passed every gate.** The other 10 failed only the response-marker
  correctness gate for this synthetic-filler probe; they were not transport,
  token-accounting, JSON, OOM, or restart failures.
- The tested input spine was 2,048 / 8,192 / 16,384 / 32,768 / 65,152. The
  65,152-token c1/c2/c4 paths passed transport and exact usage; c2/c4 also
  passed every marker gate.

### 2. Performance — MEASURED (no agreed customer SLO)

- Direct Ollama cold TTFT p50: 2,048 **3.8s**; 8,192 **5.9s**; 16,384
  **12.6s**; 32,768 **30.4s**; 65,152 **83.2s**.
- Direct Ollama prefix-reuse cells showed **no measured TTFT acceleration**:
  warm was approximately cold and `cached_prompt_ratio` was not reported.
- A separate sibling **VLLM APC** run improved 32K c4 from 144.3s cold to
  30.8s warm. Do not attribute that VLLM result to Ollama.

### 3. Quality — NOT ACHIEVED

- Native tools: 22/22 valid JSON and names; 21/22 correct arguments/steps
  (**95.45%**).
- Real-agent smoke: 16/16 transport successes, but only 1/4 tasks (**25%**)
  met the exact final-answer contract.
- Replay v2/v3 fixed+branch integrity passed with 28 regular + 4
  checkpoint/padding tool turns (**32 total**), but the quality phase produced
  **0 rows**. The runners were stopped to enforce the user's explicit decision
  to skip the remaining replay; repetitions 2/3 were not run. Missing login
  linger remains a future unattended-rerun risk, not the recorded abort reason.
  No long-horizon quality pass is claimed.

### 4. Reliability — NOT RUN

- No soak, fault-injection/recovery, or side-effect idempotency test ran.
  Ollama's 0 restart / 0 OOM observation is incidental stability evidence only.

## Bottom line

- **Proven:** 65,536 configured/loaded allocation; exact tested inputs through
  65,152; 174/174 transport success and exact backend usage on the selected
  direct workload.
- **Not proven:** declared 131K/262K operation, matched router/direct A/B,
  three-repetition long-horizon quality, or reliability soak/recovery.
- **Next:** use linger or a detached container for quality replay; install the
  router for matched A/B; then run a representative 32K/65,152 soak and recovery
  test.

Canonical structured facts: [four-proof status](agentic-context-customer-20260722-four-proof-status.json)
and [evidence index](agentic-context-customer-20260722-evidence-index.json).
Technical interpretation: [`../perf-report.md` §9](../perf-report.md).
