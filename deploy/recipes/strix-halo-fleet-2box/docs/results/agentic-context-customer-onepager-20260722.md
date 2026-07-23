# Agentic Context Window - Customer Evidence Brief (one page)

Host: HP Z2 Mini G1a, AMD Ryzen AI MAX+ PRO 395 / Radeon 8060S (gfx1151), 64 GiB VRAM carveout, ROCm 7.0.
Backend: Ollama 0.32.1 ROCm, model gemma4:26b-a4b-it-q8_0 (Q8_0). Serving window configured AND verified-loaded at 65,536 tokens, parallel=1, 0 restarts / 0 OOM.

## What how-much-context means here (three numbers)
- Router-declared metadata: 131,072 / 262,144 (model card / router advertisement).
- Backend-configured: 65,536 (OLLAMA_CONTEXT_LENGTH).
- Test-proven serving window: 65,536, verified by live ollama ps (100 percent GPU, CONTEXT=65536) and exercised end-to-end below.

## Four proofs (honest status)

### 1. Capacity - PARTIAL PASS
- 64K serving allocation verified (loaded-context provenance, 0 restart / 0 OOM, peak VRAM about 30.7 GB).
- Exact-token capacity spine at 2K / 8K / 16K / 32K / 64K: transport success and prompt-token accounting healthy on all 17 of 17 cells.
- 7 of 17 cells fully green across every gate. The other 10 fail ONLY the marker-accuracy gate under synthetic-filler stress (higher concurrency / prefix reuse) - a probe-construction artifact, not a serving failure.

### 2. Performance - MEASURED (no customer SLO agreed yet)
- Time-to-first-token (prefill-bound), p50: 2K about 3.8s, 8K about 5.9s, 16K about 12.6s, 32K about 30.4s, 64K about 83s.
- Prefix / APC reuse cross-checked on the VLLM sibling run: 32K c4 cold 144.3s to warm 30.8s.

### 3. Quality - NOT ACHIEVED
- Tool-call schema fidelity strong (native tool test: JSON 100, function-name 100, arguments 95.45).
- Long-horizon replay integrity passed (fixed checkpoints 8K/16K/32K/64K marker-preserved, 32 tool turns; branch-replay pass).
- BUT end-to-end agent task success was only 25 percent (VLLM smoke) and the 44-trial long-horizon quality suite never completed (background runner stopped when its SSH session ended; demo-002 lacks linger). No accepted quality result.

### 4. Reliability - NOT RUN
- No soak, no fault-injection/recovery, no side-effect idempotency test. Ollama 0 restart / 0 OOM is an incidental stability signal only.

## Bottom line
- Proven: the box serves a real 64K context window and accounts for tokens exactly from 2K to 64K, on GPU, with zero restarts/OOM.
- Not proven yet: matched router-vs-direct A/B, long-horizon multi-turn agent QUALITY (three reps), and reliability (soak + fault recovery).
- Fastest path to close the gap: enable linger (or a detached container) on demo-002 and re-run the three-rep quality suite with the applied Ollama-argument fix; stand up the router on demo-002 for the A/B; add one 32K/64K soak + fault-recovery run.

Artifacts: four-proof-status.json, evidence-index.json, capacity-direct-openai/, replay-direct-v3-rep1/, runtime/, blockers/, plus sibling bundle agentic-prefill-20260722/.
