# Edge-Fleet Config Control Plane — Research Roadmap (pick list)

Next directions to harden and extend the [pipeline](research-pipeline.md), framed
as research items. Each has a **goal**, a **method**, the **metrics/data to
collect on Strix Halo** (so every claim is data-backed), and a rough **effort +
risk**. Nothing here is shipped yet — pick what to pursue.

Legend — Effort: S ≤ half-day · M ≤ 2 days · L > 2 days. Risk: how likely it
perturbs the working green run.

## Theme A — Harden the proof (turn `[SKIP]`/assumptions into data)

### R1 · Real-gateway drift self-heal on hardware
- **Goal.** Turn the gateway-mode `[SKIP]` into a `[PASS]` with data, without
  risking a live gateway.
- **Method.** Out-of-band append a *harmless comment line* to the bind-mounted
  config (a change the router accepts), confirm the agent detects drift via
  `GET /config/hash` and reverts it in-place; gate behind
  `FLEET_VERIFY_DRIFT_ON_GATEWAY=1`.
- **Metrics/data.** drift-detect latency (write → revert), reload count delta,
  `hash` returns to desired, router keeps serving (no 5xx).
- **Effort/Risk.** S / low (reversible, non-schema edit).

### R2 · Fail-fast config × image lint before the 44 GB pull
- **Goal.** Cut a schema-mismatch failure (e.g. the `session_aware` removal) from
  ~9 min (real cold-start) to seconds.
- **Method.** After render, validate the config against the *pinned router image*
  before Ollama pulls — either a `vllm-sr` dry-run/validate entrypoint (investigate;
  propose upstream if missing) or `docker run --rm <image> <router> --validate`.
  Fall back to the structural [`validate_poc_config.py`](../../strix-halo-poc/validate_poc_config.py).
- **Metrics/data.** time-to-first-failure (target < 10 s), false-negative rate vs
  real serve.
- **Effort/Risk.** M / low. Depends on an image-level validate entrypoint.

### R3 · Deterministic image pinning + CI parse-against-image
- **Goal.** Kill `:latest` drift (the root cause of this session's outage).
- **Method.** A committed `versions.env.example` + optional `versions.env`
  (gitignored) that sets `VLLM_SR_ROUTER_IMAGE=...@sha256:...`, sourced by
  `run-all`; CI job parses the committed `poc-strix.yaml` against that pinned image.
- **Metrics/data.** config-vs-image compatibility = pass/fail in CI; both boxes
  report the same image digest in `metrics.json` (add `router_image_digest`).
- **Effort/Risk.** S / low.

## Theme B — Security (demo-grade → production-grade)

### R4 · Asymmetric signing (Ed25519) + key rotation
- **Goal.** Today's HMAC is a *shared secret* — any box that can verify can also
  forge desired config. Move to CCP-signs-private / agents-verify-public.
- **Method.** Swap `fleet_lib` sign/verify to Ed25519 (needs `cryptography`/`pynacl`,
  i.e. relax the stdlib-only constraint, or vendor a minimal Ed25519); add key-id +
  rotation in the bundle.
- **Metrics/data.** sign/verify cost (µs), bundle size delta, tamper-rejection still
  8/8 in `verify_local`, negative test: a compromised edge cannot forge.
- **Effort/Risk.** M / medium (touches the crypto core + a dependency).

### R5 · TLS for the CCP endpoints
- **Goal.** Config is signed (integrity) but travels in cleartext (no
  confidentiality). Add TLS for sensitive configs.
- **Method.** Wrap the CCP HTTP server in TLS (self-signed for the PoC; document
  cert provisioning); agents verify the cert.
- **Metrics/data.** handshake overhead per poll, throughput delta.
- **Effort/Risk.** M / low-medium.

## Theme C — Scale & operations

### R6 · CCP HA / durable desired+audit store
- **Goal.** The CCP is a single point of failure on Halo-A; the desired+audit store
  is in-process. Persist and/or replicate it.
- **Method.** Back desired+audit with a durable store (file→sqlite→replicated);
  document recovery; optional standby CCP.
- **Metrics/data.** recovery time after CCP restart, zero audit loss across restart.
- **Effort/Risk.** L / medium.

### R7 · N-box scale-out
- **Goal.** The design is N-agent; the recipe is hard-wired to 2. Parameterize and
  measure how convergence scales with fleet size.
- **Method.** Fleet list from a file; loop provisioning/verify over N boxes.
- **Metrics/data.** convergence span vs N (3, 5, …), CCP CPU/RAM vs N, network vs N.
- **Effort/Risk.** M / low.

### R8 · Health-gated apply + auto-rollback
- **Goal.** A bad desired config that fails to load should not silently leave a box
  down; the fleet should self-protect.
- **Method.** Agent reports *unhealthy* if the new config fails to load/serve; CCP
  surfaces it and can auto-roll back to the prior good version.
- **Metrics/data.** bad-config blast radius (# boxes affected), auto-rollback time,
  false-positive rate.
- **Effort/Risk.** M / medium.

### R9 · Convergence/latency observability (Prometheus)
- **Goal.** Beyond the audit log, expose fleet health as metrics.
- **Method.** Export per-box version-lag + apply-outcome + convergence span to
  Prometheus; add sub-second write→converge timing in the agent (feeds R1/pipeline
  §4).
- **Metrics/data.** hot-reload latency distribution (p50/p95), version-lag gauge.
- **Effort/Risk.** M / low.

### R10 · Key-based SSH + ControlMaster reuse (ops polish)
- **Goal.** Remove the repeated password prompts during log collection.
- **Method.** Document `ssh-copy-id`; share the deploy's SSH ControlMaster socket
  with `run-all` so the whole run authenticates once.
- **Metrics/data.** prompts per run → 0; wall-clock delta.
- **Effort/Risk.** S / low.

## Theme D — Routing features

### R11 · Restore cross-request learning (`global.router.learning`)
- **Goal.** The migration off `session_aware` dropped session stickiness. Re-add it
  via the new schema and measure routing quality.
- **Method.** Add `global.router.learning.{adaptation,protection}` to
  `poc-strix.yaml` (schema confirmed from upstream), **re-validate on hardware**
  before committing (it touches the live config).
- **Metrics/data.** same-conversation model-stickiness rate, routing-decision
  distribution before/after, no serve regression.
- **Effort/Risk.** M / medium (must not re-break the green run — validate on HW first).

## Recommended sequencing

1. **R3 + R2** — kill the two failure modes this session actually hit (image drift,
   late schema failure). Highest value, lowest risk.
2. **R1 + R9** — close the last `[SKIP]` and get sub-second hot-reload data (best
   paper numbers).
3. **R7** — scale evidence (convergence vs N) strengthens the systems contribution.
4. **R4** (security) and **R6/R8** (resilience) when moving toward production.
5. **R10** (ops) and **R11** (routing feature) opportunistically.

> Which items do you want next? A reasonable first cut for a paper is
> **R3 → R2 → R1 → R9 → R7** (reproducibility + fail-fast + full-proof + latency
> data + scale), leaving security/HA/features as "future work".
