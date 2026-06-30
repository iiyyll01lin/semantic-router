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

### Gateway mode (real `vllm-sr serve` on both boxes)

Prerequisites (BOTH boxes): the semantic-router repo checked out with the
**strix-halo-poc** recipe present (its committed `poc-strix.yaml`) and the
`vllm-sr` CLI installed (e.g. `pip install -e src/vllm-sr`). Halo-B does **not**
need to be on the same branch as this fleet recipe (the orchestrator ships these
scripts to it), and `vllm-sr` does **not** need to be on the non-interactive SSH
`PATH` — the bring-up also probes common conda/venv bin dirs
(`~/miniconda3/bin`, `~/anaconda3/bin`, `~/miniforge3/bin`, `~/mambaforge/bin`,
`~/.local/bin`, `/opt/conda/bin`). Then, from Halo-A:

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
- The deploy preflights `poc-strix.yaml` on Halo-B and fails early if it is
  missing; `vllm-sr` is resolved (probing conda/venv dirs) by the bring-up, which
  fails fast with the exact fix before any slow model pulls if it is truly absent.
- The CCP serves the rendered `poc-strix.yaml` (+ a `fleet-rule-marker` line) as
  the desired config; both real gateways converge to it. Model pulls + serve make
  the first run slow.
- Reload mechanism: the router bind-mounts the config as a single file
  (`config.yaml:/app/config.yaml`) and watches it with fsnotify, so the agent
  overwrites the config **in place** (same inode) rather than via an atomic
  rename — a rename would swap in a new inode the container never sees, so no
  hot-reload would fire. Do not change `fleet_agent._write_config` back to a
  temp-file rename.

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
| [`ccp_server.py`](ccp_server.py) | Central control plane: versions/signs/serves desired config, central audit log. |
| [`fleet_agent.py`](fleet_agent.py) | Pull agent: verify signature, detect drift via `/config/hash`, apply, report. |
| [`fleet_lib.py`](fleet_lib.py) | Shared stdlib helpers: hashing, HMAC sign/verify, tiny HTTP. |
| [`fleetctl.py`](fleetctl.py) | CLI the scripts call (no jq): set-desired, status, audit, wait-converged. |
| [`mock_router.py`](mock_router.py) | Stdlib mock of the per-node config API for the offline/mock paths. |
| [`node-bring-up.sh`](node-bring-up.sh) | Bring up one edge node (router + agent); mock or gateway mode. |
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
