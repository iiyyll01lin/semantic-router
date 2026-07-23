# 2-Box Strix Halo Edge-Fleet Config Control Plane (Pull Mode)

Runnable counterpart of [PL-0036](../../../docs/agent/plans/pl-0036-edge-fleet-config-control-plane.md)
and [docs/poc/08 section 5](../../../docs/poc/08-topology-promotion-and-governance.md):
a **pull-mode** central control plane (CCP) that distributes **signed** router
config to a fleet of edge gateways, with **central audit** and **drift
self-heal**, demonstrated across two Strix Halo boxes.

It closes the gap that [strix-halo-2box](../strix-halo-2box/README.md) leaves
open: the operator centralizes config *in a Kubernetes cluster*, but a fleet of
bare edge AIPC gateways had no central, audited way to receive one rule change.
This recipe reuses the router's existing per-node primitives (fsnotify
hot-reload + `GET /config/hash`) and adds only the central distribution + audit +
signing layer.

> **✅ Verified on hardware (2026-07-01).** A full one-shot
> `HALO_A_MODE=gateway HALO_B_MODE=gateway bash run-all-2box.sh` across two Strix
> Halo boxes (Halo-A = HP Z2 Mini G1a; Halo-B = a bare box, auto-provisioned) ran
> a **real `vllm-sr` ROCm gateway on BOTH boxes** and **both converged to the same
> signed-config hash** (`a78aebc5fd5f`). `verify-fleet.sh` passed (edit-once /
> rollback / audit; drift-heal skipped in gateway mode) and the non-interactive
> demo ran the full loop — one central edit → both real routers hot-reload
> (`fc739baa…`) → central audit → one-edit rollback. Run bundle:
> `run-20260701-154843`. (An earlier `HALO_B_MODE=mock` run — `run-20260701-114428`,
> hash `76c08a3e…` — first proved real↔mock convergence.)
>
> **🔒 Validating the opt-in hardening on hardware.** To take the landed hardening
> to the fleet — Ed25519 + TLS + **mTLS** end-to-end, induced auto-rollback on a
> real router, CCP-restart durability, `GET /metrics` + p50/p95 hot-reload
> latency, N-box, and a warm-standby promotion drill — follow the ordered
> [**hardware validation runbook**](docs/hardware-validation-runbook.md). It
> drives [`verify-hardening.sh`](verify-hardening.sh), an opt-in, gateway-safe
> verifier kept **separate** from `verify-fleet.sh` (so the core verifier stays
> stable) that `[SKIP]`s any check whose env/hardware is absent. The same
> behaviors are proven offline (no hardware) by
> [`verify_local.py`](verify_local.py) — now **20/20**, including the mTLS
> handshake and warm-standby restore. Re-running the proof? The
> [hardware re-run checklist](docs/hardware-rerun-checklist.md) is the glanceable
> one-pager (addresses, commands, PASS/SKIP lines).

## What this proves (verified on hardware)

The dual-gateway run above put a **real `vllm-sr` ROCm router on BOTH boxes** under
one central control plane — not a stub, not a single box. Concretely it proves:

- **The signed-hash contract holds across two independent real routers.** The
  Python CCP signs `sha256(config_bytes)`; each Go router independently returns the
  same value from `GET /config/hash` over its bind-mounted source file. All three
  agreed (`a78aebc5fd5f`) — the make-or-break of the design, and something a
  mock↔mock or real↔mock run cannot show.
- **The real router accepts and parses the fleet config.** Unlike the mock (which
  only hashes bytes), the Go router validates the schema and builds the decision
  tree — exactly the check that caught the removed `session_aware` field. Serving
  means the distributed config is one the real router endorses.
- **In-place write + fsnotify hot-reload works on a live ROCm router, no restart.**
  One central edit converged both real gateways to a new hash (`fc739baa…`), and a
  one-edit rollback returned them to `a78aebc5fd5f`, with the router containers
  never restarting and continuing to serve.
- **Zero-touch onboarding of a bare edge box.** Halo-B started with no `vllm-sr`,
  no PII model, and an outdated config schema; the one-click provisioner installed
  `vllm-sr`, fetched the (public) PII model, and brought up a real gateway.
- **Signed + audited + pull-only.** Config is HMAC-signed (tampered/unsigned bundles
  are rejected — see `verify_local.py`), every apply lands in a central audit log
  (versions `v1`→`v5` across both boxes), and agents only dial **outbound**, so a
  NAT'd Halo-B needs no inbound exposure.

### What the real gateway adds over mock

| | `mock` | `gateway` (this run) |
| --- | --- | --- |
| the "router" | stdlib `mock_router.py` hashing one file | real `vllm-sr serve` ROCm stack (Ollama + tier models + PII ONNX + Envoy) |
| config schema validated | no (bytes only) | **yes** — real Go parser |
| hot-reload | nothing to reload | **fsnotify on a bind-mounted single file, in place, no restart** |
| proves | the control-plane logic (sign / fan-out / drift / rollback / audit) | all of that **against a production-shaped router that must accept + hot-reload + keep serving** |

The topology and the per-agent loop are in **How it works** below; the two modes
are in **Two modes**.

## How it works

```
                Central Control Plane (CCP, on Halo-A)
                - versions + signs the desired config
                - serves it; keeps the central audit log
                  GET /fleet/desired   POST /fleet/desired (edit once)
                  GET /fleet/status    POST /fleet/status  (agents report)
                        ^ pull (outbound only)   ^
        +---------------+------------+   +--------+----------------+
        | Halo-A edge node           |   | Halo-B edge node        |
        | router :8080 + pull agent  |   | router :8080 + agent    |
        | agent: verify sig -> if    |   | (same)                  |
        | drift, write config file   |   |                         |
        | -> fsnotify hot-reload     |   |                         |
        +----------------------------+   +-------------------------+
```

Each agent loop: `GET /fleet/desired` -> verify HMAC signature + content hash ->
`GET localhost/config/hash` -> if it differs from the desired hash, back up and
write the local config file (the router hot-reloads via fsnotify, no restart) ->
poll until converged -> `POST /fleet/status`. Agents are **pull-only** (outbound
to the CCP and to localhost), so a NAT'd/firewalled edge box needs no inbound
exposure.

## Two modes

- **`FLEET_MODE=mock` (default)** — each box runs a stdlib `mock_router.py` that
  implements `GET /config/hash` over a config file, so the WHOLE fan-out is
  one-click verifiable on the two boxes **without ROCm/models**. Best for proving
  the control plane and for the demo.
- **`FLEET_MODE=gateway`** — each box runs a REAL `vllm-sr serve` ROCm gateway
  (via `gateway-bring-up.sh`, which mirrors the proven single-box
  [strix-halo-poc](../strix-halo-poc/bring-up.sh): local Ollama + tier models +
  the ModernBERT PII ONNX export, served with `VLLM_SR_AMD_PRESERVE_CPU=1`). The
  agent manages the gateway's bind-mounted source config: `GET /config/hash`
  reads it, and an external write triggers the router's fsnotify hot-reload, so
  the same agent code path works unchanged. Editing the fleet marker line at the
  CCP converges both real routers.

## One-click on two Strix Halo

Run a single command on **Halo-A** (it provisions a bare Halo-B over SSH/scp):

```bash
# from this directory, on Halo-A:
HALO_A_IP=192.0.2.10 \
HALO_B_IP=192.0.2.20 \
HALO_B_SSH=ubuntu@192.0.2.20 \
  bash deploy-fleet-2box.sh
```

- `HALO_A_IP` must be the address of Halo-A **reachable from Halo-B** (the CCP URL the Halo-B agent pulls from).
- Defaults to `FLEET_MODE=mock`. Add `FLEET_MODE=gateway` once real routers are up on both boxes.
- The script starts the CCP + Halo-A node, SSH-provisions the Halo-B node, waits for **both** boxes to converge, then runs `verify-fleet.sh` and prints `PASS`.

Then:

```bash
bash demo-fleet.sh                                  # narrated edit-once demo
bash verify-fleet.sh                                # re-run headless PASS/FAIL
HALO_B_SSH=ubuntu@192.0.2.20 bash teardown-fleet-2box.sh
```

### Key-based SSH (authenticate once) — R10

The orchestrator connects to each remote box over SSH/scp many times. Set up
key-based SSH once so there are **zero** password prompts:

```bash
ssh-copy-id ubuntu@192.0.2.20          # add -p PORT for a non-standard port
```

`run-all-2box.sh` opens a **single shared SSH ControlMaster socket** and hands it
to `deploy-fleet-2box.sh`, the demo, log collection and teardown (via
`FLEET_SSH_CONTROL_PATH`), so the whole run authenticates to each box **once** and
reuses that connection — even with a passphrase-protected key, you unlock it a
single time. Running `deploy-fleet-2box.sh` on its own still opens (and cleans up)
its own ControlMaster. Per-box options: `HALO_B_SSH_KEY` (identity file) and
`HALO_B_SSH_PORT`, or the `ssh_key` / `ssh_port` columns in `fleet.hosts`.

### Fully hands-off (one shot + log bundle)

For an unattended run that does **deploy + verify + demo** in one go and
collects every relevant log into a single directory for offline review, use the
same env as `deploy-fleet-2box.sh`:

```bash
HALO_A_IP=192.0.2.10 HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 \
FLEET_MODE=gateway bash run-all-2box.sh
```

- The demo step runs non-interactively (no TTY needed); its exit code is the
  deploy/verify result. Add `SKIP_DEMO=1` to stop after verify.
- Win or lose, a `run-<timestamp>/` bundle (CCP log, both boxes' agent **and
  router container** logs, plus a final `fleetctl status`/`audit` snapshot) is
  printed at the end — share that whole directory if anything failed. The router
  container logs capture a hot-reload crash that the serve-wrapper log would not.

### Gateway mode (real `vllm-sr serve` on both boxes)

Prerequisites (BOTH boxes): the semantic-router repo checked out with the
**strix-halo-poc** recipe present (its committed `poc-strix.yaml`) and the
`vllm-sr` CLI installed (e.g. `pip install -e src/vllm-sr`) — though on **Halo-B**
the deploy **auto-installs `vllm-sr` for you** when it is missing (see
*Auto-provisioning Halo-B* below). Halo-B does **not**
need to be on the same branch as this fleet recipe (the orchestrator ships these
scripts to it), and `vllm-sr` does **not** need to be on the non-interactive SSH
`PATH` — the bring-up probes common conda/venv bin dirs, **including named-env
bin dirs** (`~/miniconda3/bin`, `~/anaconda3/bin`, `~/miniforge3/bin`,
`~/mambaforge/bin`, `~/.local/bin`, `/opt/conda/bin`, and `.../envs/*/bin`). If
`vllm-sr` lives somewhere else on Halo-B, set `VLLM_SR_BIN` to the directory that
holds it (e.g. `VLLM_SR_BIN=$HOME/miniconda3/envs/vsr/bin`); it is forwarded to
Halo-B and wins over the probe. Then, from Halo-A:

```bash
HALO_A_IP=192.0.2.10 \
HALO_B_IP=192.0.2.20 \
HALO_B_SSH=ubuntu@192.0.2.20 \
HALO_B_REPO=/home/ubuntu/yy/workspace/semantic-router \
FLEET_MODE=gateway \
  bash deploy-fleet-2box.sh
```

- `HALO_B_REPO` is the repo path on Halo-B. Gateway mode ships this recipe's own
  scripts to a temp dir on Halo-B and points them at `${HALO_B_REPO}/deploy/recipes/strix-halo-poc`
  (via `STRIX_POC_DIR`) for the proven `poc-strix.yaml` + staged models — so Halo-B
  only needs strix-halo-poc + the `vllm-sr` CLI, not this branch checked out.
- When `HALO_B_MODE=gateway` the deploy **auto-provisions** Halo-B (below):
  it installs `vllm-sr` if absent, downloads the public PII source model if the
  staged copy is missing, and lets the first serve pull any missing images. Only a
  missing (committed) `poc-strix.yaml` stops it — with the exact checkout fix.
- The CCP serves the rendered `poc-strix.yaml` (+ a `fleet-rule-marker` line) as
  the desired config; both real gateways converge to it. Model pulls + serve make
  the first run slow.
- Reload mechanism: the router bind-mounts the config as a single file
  (`config.yaml:/app/config.yaml`) and watches it with fsnotify, so the agent
  overwrites the config **in place** (same inode) rather than via an atomic
  rename — a rename would swap in a new inode the container never sees, so no
  hot-reload would fire. Do not change `fleet_agent._write_config` back to a
  temp-file rename.

#### Pinning the router image (avoid `:latest` version skew) — R3

Both boxes resolve the router image as `vllm-sr-rocm:latest` by default. Because
`:latest` moves, a box that pulls it later can get a **newer** image whose config
schema no longer matches the committed `poc-strix.yaml` — e.g. a fatal
`runtime_config_load_failed: removed config fields are no longer supported:
global.router.model_selection.session_aware`. A fleet serves ONE config to every
box, so they must run the **same** image. Pin it to a known-good digest.

**Recommended: a committed `versions.env.example` + a gitignored `versions.env`.**
`run-all-2box.sh` and `deploy-fleet-2box.sh` source `./versions.env` (if present)
near the top and forward the pin to **every** box (Halo-A inherits it locally;
each remote box gets it over SSH), so the whole fleet runs the same digest:

```bash
cp versions.env.example versions.env      # versions.env is gitignored
# Put your known-good digest in versions.env. Get it from a box that serves OK:
#   docker inspect --format '{{index .RepoDigests 0}}' \
#     ghcr.io/vllm-project/semantic-router/vllm-sr-rocm:latest
#   # -> ghcr.io/vllm-project/semantic-router/vllm-sr-rocm@sha256:…
HALO_A_MODE=gateway HALO_B_MODE=gateway HALO_B_SSH=… HALO_B_REPO=… \
  bash run-all-2box.sh
```

You can still pin ad-hoc via the environment (it wins over `versions.env`):

```bash
VLLM_SR_ROUTER_IMAGE=ghcr.io/vllm-project/semantic-router/vllm-sr-rocm@sha256:… \
HALO_A_MODE=gateway HALO_B_MODE=gateway HALO_B_SSH=… HALO_B_REPO=… \
  bash run-all-2box.sh
```

- `VLLM_SR_ROUTER_IMAGE` is read by `vllm-sr serve`; the deploy forwards it to
  every remote box, and Halo-A inherits it locally.
- Each run captures the **resolved** router image digest per box into the bundle
  (`run-<ts>/router-image-digests.txt`) so you can confirm every box ran the same
  image.
- `ci-check.sh` fails if the pin is not an immutable `@sha256` digest (kills the
  drift at review time) — see *CI / config lint* below.

#### Fail-fast config validation before the ~44 GB pull — R2

In gateway mode, `deploy-fleet-2box.sh` validates the **rendered** gateway config
**before** any Ollama model pull or cold start, so a schema mismatch fails in
**seconds** instead of ~9 minutes in. It runs `ci-check.sh`, which prefers the
real `vllm-sr validate` (canonical v0.3 schema) and falls back to the structural
[`../strix-halo-poc/validate_poc_config.py`](../strix-halo-poc/validate_poc_config.py).
It is advisory (a missing validator warns and proceeds) but a genuinely invalid
config **aborts** the deploy. Bypass with `FLEET_SKIP_VALIDATE=1`.

##### CI / config lint

`ci-check.sh` is the one gate CI and the deploy share. In CI it runs strict
(`CI_CHECK_STRICT=1`, wired in `.github/workflows/strix-fleet-config-lint.yml`):
it parses the committed `poc-strix.yaml` and **requires** `versions.env.example`
to pin an `@sha256` digest. Run it locally the same way:

```bash
CI_CHECK_STRICT=1 bash ci-check.sh
```

#### Optional security env pass-through

The control-plane core has **opt-in** asymmetric signing (Ed25519) and TLS (R4/R5)
with safe fallbacks (unset ⇒ today's HMAC-over-HTTP). The orchestrator forwards a
documented allow-list of these `FLEET_*` vars **only if they are set** (never
required); the names match what `ccp-bring-up.sh` / `node-bring-up.sh` read.

- **Agent-side** (exported for the Halo-A agent **and** forwarded to every remote
  agent over SSH): `FLEET_SIGN_MODE`, `FLEET_ED25519_PUBLIC`,
  `FLEET_ED25519_PUBLIC_FILE`, `FLEET_TLS_CA`, `FLEET_TLS_INSECURE`,
  `FLEET_BUNDLE_MAX_AGE`.
- **CCP-side** (exported for the local Halo-A CCP **only** — the private signing
  seed and TLS server key **never leave Halo-A**): `FLEET_SIGN_MODE`,
  `FLEET_ED25519_SECRET`, `FLEET_ED25519_SECRET_FILE`, `FLEET_BUNDLE_TS`,
  `CCP_TLS_CERT`, `CCP_TLS_KEY`, `CCP_TLS_CLIENT_CA`, `CCP_AUDIT_MEMORY_MAX`.
- **Extra agent knobs** (also forwarded so remotes match Halo-A; R8 health/rollback):
  `ROUTER_HEALTH_PATH`, `ROUTER_HEALTH_TIMEOUT`, `APPLY_BACKOFF`, `APPLY_BACKOFF_MAX`.

Override the lists via `FLEET_SECURITY_AGENT_VARS` / `FLEET_SECURITY_CCP_VARS` /
`FLEET_AGENT_EXTRA_VARS` if the core renames anything. Path-valued vars
(`*_FILE`, `FLEET_TLS_CA`) must exist on the box that reads them (stage the CA /
public key on each edge box; the CCP's private key stays on Halo-A).

#### End-to-end TLS (R5)

When `CCP_TLS_CERT` + `CCP_TLS_KEY` are set, the CCP serves HTTPS and the
orchestrator builds the agent-facing `CCP_URL` (local + remote, and the copy
persisted in `fleet.env`) as `https://…`, so the pull agents and `fleetctl`
actually use TLS (`fleet_lib` auto-enables it for an `https://` URL). Force the
scheme explicitly with `FLEET_CCP_SCHEME=https` (e.g. behind an external
terminator). With a self-signed cert the agents must trust it — set `FLEET_TLS_CA`
to the cert (forwarded to remotes) or `FLEET_TLS_INSECURE=1`. No TLS env ⇒ plain
`http://`, unchanged.

#### Auto-provisioning Halo-B (`HALO_B_PROVISION`)

Set `HALO_B_MODE=gateway` (or `FLEET_MODE=gateway`) and the deploy makes Halo-B
gateway-ready in **one shot**. It ships `provision-halo-b.sh` to Halo-B and runs
it there (native paths/pip, idempotent, **user-space only** — `pip --user`, no
`sudo`). The provisioner:

- **`vllm-sr` CLI** — if missing, installs it with
  `pip install --user -e ${HALO_B_REPO}/src/vllm-sr` (the console script lands in
  `~/.local/bin`, which the bring-up auto-detects), then re-verifies.
- **ModernBERT PII source model** — if the staged model dir is missing, downloads
  it from the **public** HF repo
  `LLM-Semantic-Router/pii_classifier_modernbert-base_presidio_token_model` (no
  token needed); `gateway-bring-up.sh` then exports its ONNX.
- **Runtime Docker images** — pulled on the first serve via
  `--image-pull-policy ifnotpresent` (override with `VLLM_SR_IMAGE_PULL_POLICY`).

Two things stay a one-time manual prep (the provisioner will **not** mutate your
git tree or guess credentials):

- **`poc-strix.yaml`** is committed, so if Halo-B's checkout lacks the
  strix-halo-poc recipe the provisioner fails fast with the exact fix
  (`git fetch && git checkout poc/strix-halo-single-box` on Halo-B).
- the large **Ollama tier models** are pulled by `gateway-bring-up.sh` itself.

Opt out with `HALO_B_PROVISION=skip` (the deploy then just fail-fast checks the
prereqs and leaves Halo-B for you to manage).

### Mixed fleet (real gateway on one box, mock edge on the other)

If only one box can run a real `vllm-sr` gateway (e.g. Halo-B is a minimal box
without `vllm-sr`/ROCm images), run each box in its own mode with `HALO_A_MODE`
and `HALO_B_MODE` (each defaults to `FLEET_MODE`):

```bash
HALO_A_IP=192.0.2.10 HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@192.0.2.20 \
HALO_A_MODE=gateway HALO_B_MODE=mock \
  bash deploy-fleet-2box.sh
```

- Halo-A runs the **real** gateway; Halo-B runs the pure-Python **mock** edge (no
  `vllm-sr`/GPU, no `HALO_B_REPO` needed). You still get the full control-plane
  story across both boxes: signed fan-out, convergence, drift self-heal, rollback,
  central audit.
- When any box is a gateway, the CCP's desired config is the **real** rendered
  gateway config (a mock edge just stores the bytes and reports their hash, so it
  converges too). `verify-fleet.sh`/`demo-fleet.sh` edit that real config.
- Upgrade Halo-B to a real gateway later by just setting `HALO_B_MODE=gateway` —
  the deploy **auto-provisions** it (installs `vllm-sr`, downloads the public PII
  model, pulls any missing ROCm images; see *Auto-provisioning Halo-B* above).
  Set `HALO_B_PROVISION=skip` to manage Halo-B yourself.

## Scaling beyond two boxes (N-box) — R7

The recipe defaults to two boxes (Halo-A + Halo-B). To run **more**, create a
`fleet.hosts` file (copy [`fleet.hosts.example`](fleet.hosts.example), which
documents the column format) listing each REMOTE edge box, one per line. Halo-A
stays the CCP + first edge node and is implicit (never listed). The orchestrator
loops provisioning, the convergence wait, log collection and teardown over every
listed box; a mixed fleet (some `gateway`, some `mock`) works too.

```bash
cp fleet.hosts.example fleet.hosts     # fleet.hosts is gitignored; then edit it
HALO_A_IP=192.0.2.10 FLEET_MODE=gateway bash run-all-2box.sh
```

When `fleet.hosts` is absent (or has no active lines) the classic 2-box behavior
driven by `HALO_B_SSH` / `HALO_B_IP` / `HALO_B_MODE` / `HALO_B_REPO` is unchanged,
so `bash run-all-2box.sh` with the documented 2-box env works exactly as before.
All N-box logic lives in the orchestrator scripts; the per-node bring-up scripts
are shipped unchanged.

## Verify the logic offline (no hardware)

```bash
python3 verify_local.py
```

Spins up the CCP + two mock routers + two agents in-process and asserts:
baseline converge, edit-once converge **via hot-reload (not restart)**, drift
self-heal, fleet rollback, **signed-bundle tamper rejection**, and central audit.
This is what proves the new logic in CI-like conditions.

## Research & metrics

Every run emits `metrics.json` (via [`fleet_metrics.py`](fleet_metrics.py)). The run
bundle also captures the CCP's raw JSON-lines `audit.log`, whose per-apply
`write→converge` timer lets `metrics.json` report sub-second hot-reload latency
(`hot_reload_latency_seconds`: p50/p95/mean, R9). For the
paper-oriented pipeline (figure), per-stage data specs, efficiency metrics, and the
novelty + feasibility argument, see
[`docs/research-pipeline.md`](docs/research-pipeline.md); the next research
directions (pick list) are in [`docs/research-roadmap.md`](docs/research-roadmap.md).
A git-attributed inventory of the tests, experiments, and data across the three
Strix Halo recipes is in [`docs/CONTRIBUTIONS.md`](docs/CONTRIBUTIONS.md).

## Files

| File | Description |
| --- | --- |
| [`deploy-fleet-2box.sh`](deploy-fleet-2box.sh) | One-click orchestrator (run on Halo-A): CCP + all edge nodes (2-box default, N-box via `fleet.hosts`) + convergence wait + verify. |
| [`run-all-2box.sh`](run-all-2box.sh) | Hands-off one-shot: deploy + verify + non-interactive demo, capturing a full log bundle (incl. per-box image digests) for offline review. |
| [`ccp_server.py`](ccp_server.py) | Central control plane: versions/signs/serves desired config, central audit log. |
| [`fleet_agent.py`](fleet_agent.py) | Pull agent: verify signature, detect drift via `/config/hash`, apply, report. |
| [`fleet_lib.py`](fleet_lib.py) | Shared stdlib helpers: hashing, HMAC sign/verify, tiny HTTP. |
| [`fleetctl.py`](fleetctl.py) | CLI the scripts call (no jq): set-desired, status, audit, wait-converged. |
| [`mock_router.py`](mock_router.py) | Stdlib mock of the per-node config API for the offline/mock paths. |
| [`node-bring-up.sh`](node-bring-up.sh) | Bring up one edge node (router + agent); mock or gateway mode. |
| [`gateway-bring-up.sh`](gateway-bring-up.sh) | Bring up a real self-contained `vllm-sr` ROCm gateway (Ollama + tier models + PII ONNX export + serve). |
| [`provision-halo-b.sh`](provision-halo-b.sh) | Shipped to Halo-B and run there to make it gateway-ready: installs `vllm-sr` and downloads the public PII source model if missing (`HALO_B_PROVISION`). |
| [`ccp-bring-up.sh`](ccp-bring-up.sh) | Start the CCP process. |
| [`verify-fleet.sh`](verify-fleet.sh) | Headless PASS/FAIL against the live fleet (converge / drift / rollback / audit). |
| [`verify_local.py`](verify_local.py) | Offline in-process end-to-end verifier (no hardware). |
| [`fleet_metrics.py`](fleet_metrics.py) | Distil a run bundle into `metrics.json`: convergence latency, hash agreement, router readiness, config size. |
| [`demo-fleet.sh`](demo-fleet.sh) | Narrated demo: edit one rule -> both boxes converge -> audit -> rollback. |
| [`teardown-fleet-2box.sh`](teardown-fleet-2box.sh) | Stop CCP + Halo-A node + every remote node (over SSH); N-box aware. |
| [`ci-check.sh`](ci-check.sh) | Config × pinned-image gate (R3+R2): CI strict-lint and the deploy-time pre-pull fail-fast validator. |
| [`versions.env.example`](versions.env.example) | Template for a gitignored `versions.env` that pins `VLLM_SR_ROUTER_IMAGE` to a digest (R3). |
| [`fleet.hosts.example`](fleet.hosts.example) | Template for a gitignored `fleet.hosts` inventory to scale past two boxes (R7). |
| [`sample-desired-config.yaml`](sample-desired-config.yaml) | The initial desired config the CCP serves. |

## Honest boundaries

### 2026-07-22/23 gfx1151 serving addendum

- A pinned official vLLM v0.25.1 image was measured on `demo-002` with BF16
  `Qwen/Qwen2.5-7B-Instruct`, `ROCM_ATTN`, chunked prefill, and localhost-only
  isolation. All **48/48** planned 16K/32K batching/APC cells produced valid
  checkpoints; **435/448** measured requests completed successfully, with no OOM
  or container restart. This supersedes the blanket statement that vLLM cannot
  execute on gfx1151 for that exact stack, but it is not a claim about arbitrary
  images, models, attention backends, or ROCm releases.
- Long-context transport success was not agentic correctness: the repetitive
  matrix produced zero required `MATRIX_OK` markers. Native short-context tools
  scored **21/22** correct arguments/steps, while the real-agent smoke met the
  exact final-answer contract on **1/4** tasks. See §9 of
  [`docs/perf-report.md`](docs/perf-report.md) for the separated interpretation
  and BF16-vs-GGUF caveat.
- Replay did not reach full acceptance: three v1 repetitions failed payload
  calibration with HTTP 400; later v2/v3 attempts passed fixed+branch semantics
  before being stopped to enforce the user's explicit decision to skip the
  remaining replay. **Quality rows = 0** and repetitions 2/3 were not run. The
  scope-abort records do not attribute the stop to a model/OOM failure. Missing
  login linger remains a future unattended-rerun durability risk, not the
  recorded abort reason. A new same-host `demo-002` llama.cpp validation remained
  deferred. See the complete
  [campaign ledger](docs/results/agentic-prefill-campaign-20260722.md).
- The direct Ollama service was configured and verified loaded at **65,536
  tokens**. Its largest tested input was **65,152 tokens**; adding output 256 and
  reserved headroom 128 gives the exact 65,536-token budget. The selected scope
  finished **17 cells / 174 measured requests** with **174/174 HTTP successes**
  and exact backend-reported prompt usage, **150/174 required markers**, and
  **7/17 cells passing every gate**. The ten failed cells missed only the
  response-marker gate for this probe; the 65,152-token c2/c4 cells passed every
  gate. See the focused [customer brief](docs/results/agentic-context-customer-onepager-20260722.md)
  and [structured proof](docs/results/agentic-context-customer-20260722-four-proof-status.json).

- The router's own `/config/*` API has **no native authentication** today, so the
  agent calls it on **localhost only**; the cross-box trust boundary is the
  **signed CCP bundle** (HMAC) plus the shared CCP token. mTLS / native API auth
  is a follow-up, not part of this PoC.
- This is a **topology / governance** PoC. Both boxes are gfx1151 APUs; there are
  no Instinct/MI350P performance or TCO claims here (continues the honest split
  of the poc docs).
- `mock` mode proves the control plane without the gateway; `gateway` mode runs
  the real router and is validated on the ROCm hardware, not in CI.
- The agent runs as a plain stdlib Python process (pull-only). Container
  packaging is optional and not required for the PoC.
