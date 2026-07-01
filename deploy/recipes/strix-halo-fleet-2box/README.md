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
> `HALO_A_MODE=gateway HALO_B_MODE=mock bash run-all-2box.sh` across two Strix
> Halo boxes (Halo-A = HP Z2 Mini G1a, Halo-B = a minimal box) passed end to end:
> Halo-A brought up a **real `vllm-sr` ROCm gateway** (router ready, `/config/hash`
> live), Halo-B ran the pure-Python mock edge, and **both converged to the same
> signed-config hash** (`76c08a3e…`). `verify-fleet.sh` passed (edit-once /
> rollback / audit; drift-heal skipped in gateway mode) and the non-interactive
> demo ran the full loop — one central edit → both boxes hot-reload → audit trail
> → one-edit rollback. Run bundle: `run-20260701-114428`. See **Mixed fleet** and
> **Gateway mode** below.

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

## Verify the logic offline (no hardware)

```bash
python3 verify_local.py
```

Spins up the CCP + two mock routers + two agents in-process and asserts:
baseline converge, edit-once converge **via hot-reload (not restart)**, drift
self-heal, fleet rollback, **signed-bundle tamper rejection**, and central audit.
This is what proves the new logic in CI-like conditions.

## Files

| File | Description |
| --- | --- |
| [`deploy-fleet-2box.sh`](deploy-fleet-2box.sh) | One-click orchestrator (run on Halo-A): CCP + both edge nodes + convergence wait + verify. |
| [`run-all-2box.sh`](run-all-2box.sh) | Hands-off one-shot: deploy + verify + non-interactive demo, capturing a full log bundle for offline review. |
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
| [`demo-fleet.sh`](demo-fleet.sh) | Narrated demo: edit one rule -> both boxes converge -> audit -> rollback. |
| [`teardown-fleet-2box.sh`](teardown-fleet-2box.sh) | Stop CCP + both nodes (Halo-B over SSH). |
| [`sample-desired-config.yaml`](sample-desired-config.yaml) | The initial desired config the CCP serves. |

## Honest boundaries

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
