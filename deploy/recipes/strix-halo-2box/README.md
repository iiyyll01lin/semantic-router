# 2-Box Strix Halo Client/Server PoC (Edge Gateway)

This folder is the runnable counterpart to
[docs/poc/07-client-server-topology.md](../../../docs/poc/07-client-server-topology.md):
a two-box client/server PoC where the LLM Gateway (Envoy + semantic router) runs
on the CLIENT/EDGE box and a second box plays the Instinct datacenter, with zero
double-hops.

It is intentionally kept SEPARATE from the single-box recipe in
[../strix-halo-poc](../strix-halo-poc) so that working recipe is never disturbed.
The single-box recipe runs everything on one box; this one splits the same
routing config across two boxes.

## Topology

```text
            +-------------------------------------------+        +--------------------------+
  client -> |  Halo-A  (CLIENT / EDGE box)              |        |  Halo-B (DATACENTER box) |
  app       |                                           |        |  = pretend Instinct      |
  :8899     |  LLM Gateway: Envoy + semantic router     |        |                          |
            |  + local Ollama (small/edge models)       |        |  plain Ollama, NO router |
            |    - llama3.2:3b  -> qwen/qwen3.5-rocm     |        |    - qwen2.5:14b          |
            |    - qwen2.5:7b   -> gemini-2.5-flash-lite |        |    - qwen3:14b           |
            |  (frontier -> real Anthropic API)         |        |    - qwen2.5:32b          |
            +-------------------------------------------+        +--------------------------+
                  |       |                                            ^
  premium/frontier|       |  routine request: 0 hops (local on Halo-A) |
  -> api.anthropic.com:443 (HTTPS, ANTHROPIC_API_KEY)                  |
                  v       |                                            |
   [ Anthropic public API: claude-opus-4.6 ]                          |
        hard request       --------- 1 hop -> HALO_B_IP:11434 ---------+
```

- The router does NOT carry data or pick endpoints; it only decides the model
  and rewrites the request. Envoy load-balances to `backend_refs[].endpoint`.
- Routine tokens never cross the network (edge models on Halo-A); only hard
  escalations hit Halo-B. There is no double-hop: the gateway is co-located with
  the edge models, not on the datacenter box.
- This is a TOPOLOGY/routing/cost PoC only. Halo-B is not a real Instinct part,
  so there are no performance/throughput claims here; real Instinct perf/TCO is
  extrapolated via fleet-sim, not measured on a box.

## Files

| File | Description |
| --- | --- |
| [`deploy-2box.sh`](deploy-2box.sh) | Run on Halo-A. The one-click orchestrator: preflight, SSH/scp-provision Halo-B, reachability checks, frontier mock, gateway serve, and smoke test. |
| [`teardown-2box.sh`](teardown-2box.sh) | Run on Halo-A. One-click cleanup: stop the gateway, remove `llm-katan`, and remove the Halo-B `ollama` container over SSH. |
| [`server-bring-up.sh`](server-bring-up.sh) | Run on Halo-B. Starts `ollama/ollama:rocm` on `0.0.0.0:11434` with AMD GPU passthrough and pulls only the big tier models (`qwen2.5:14b`, `qwen3:14b`, `qwen2.5:32b`). No router. |
| [`poc-client-edge.yaml`](poc-client-edge.yaml) | The gateway config on Halo-A. Derived from [../strix-halo-poc/poc-strix.yaml](../strix-halo-poc/poc-strix.yaml) with identical routing/decisions/modelCards/global/security; only the backend wiring is split (edge tiers local, datacenter tiers to `HALO_B_IP:11434`). |
| [`client-bring-up.sh`](client-bring-up.sh) | Run on Halo-A. Starts local Ollama with only the small/edge models, does the one-time ModernBERT PII ONNX export, renders the config with `HALO_B_IP` substituted, then serves the gateway with `--platform amd`. |
| [`smoke_test.py`](smoke_test.py) | Stdlib-only cross-box smoke test against the gateway. Reads `x-vsr-selected-model` and asserts routine -> edge (Halo-A) and hard -> datacenter (Halo-B). |
| [`gen-dsl.sh`](gen-dsl.sh) | Generates and validates the routing DSL from `poc-client-edge.yaml` (requires Go). The `.dsl` is a generated artifact and is not committed. |
| [`export-replay-trace.sh`](export-replay-trace.sh) | Read-only exporter: pages the gateway's `/v1/router_replay` API and reshapes it into fleet-sim's `semantic_router` JSONL for a TCO simulation. See "Fleet-sim TCO closer" below. |
| [`wan-latency-experiment.sh`](wan-latency-experiment.sh) | Injects `tc netem` WAN latency on Halo-B (0/20/50 ms) and re-measures the network hop from Halo-A, with a cleanup trap. Needs sudo on Halo-B + SSH. See "WAN-latency contrast" below. |

## Run Order

### One-click (recommended)

Run a single command on Halo-A. It provisions a BARE Halo-B over SSH/scp (no
repo checkout needed there), brings up both sides with hard preflight and
reachability checks, and finishes with the cross-box smoke test:

```bash
HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@halob bash deploy-2box.sh
```

`deploy-2box.sh` copies `server-bring-up.sh` to Halo-B and runs it there, waits
for Halo-B's Ollama, verifies a container on `vllm-sr-network` can reach
`HALO_B_IP:11434` (the make-or-break check), starts the `llm-katan` frontier
mock, runs `client-bring-up.sh` to serve the gateway, polls `:8899`, then runs
`smoke_test.py` and prints a PASS/FAIL summary with log paths and the teardown
command.

#### Inputs

| Env var | Required | Default | Meaning |
| --- | --- | --- | --- |
| `HALO_B_IP` | yes | — | Data-plane Ollama address of Halo-B (`HALO_B_IP:11434`). |
| `HALO_B_SSH` | no | `HALO_B_IP` | Control address `user@host` for ssh/scp. Host defaults to `HALO_B_IP`. |
| `HALO_B_SSH_PORT` | no | (ssh default) | SSH port for Halo-B. |
| `HALO_B_SSH_KEY` | no | (ssh default) | SSH identity file for Halo-B. |
| `SKIP_FRONTIER` | no | unset | Set to `1` to skip the `llm-katan` frontier mock. |
| `SKIP_SMOKE` | no | unset | Set to `1` to skip the final smoke test. |

#### One password (SSH multiplexing)

Halo-B does NOT need passwordless SSH preconfigured. `deploy-2box.sh` opens one
shared SSH master (`ControlMaster=auto`, `ControlPersist=2m`) so every ssh/scp
call reuses it and you are prompted for the password at most once. If the
initial connection fails, the script prints `ssh-copy-id` guidance.

#### Hardware prereqs (cannot be automated)

These must be in place on the boxes before running `deploy-2box.sh`:

- Both boxes: ROCm for `gfx1151`, Docker with `/dev/kfd` + `/dev/dri`
  passthrough, and the user in the `video`/`render` groups.
- Halo-B: TCP `11434` open to Halo-A (firewall + same routable network) so the
  gateway's data-plane container can escalate to `HALO_B_IP:11434`.
- Halo-A: the ModernBERT PII model under
  `../strix-halo-poc/models/...` (see `../strix-halo-poc/REHEARSAL.md` Gate B).
  `deploy-2box.sh` hard-fails preflight if it is missing.

#### Teardown

```bash
HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@halob bash teardown-2box.sh
```

`teardown-2box.sh` stops the gateway (`vllm-sr stop`), removes the local
`llm-katan` mock, and removes the `ollama` container on Halo-B over SSH (reusing
the same env vars as `deploy-2box.sh`). It leaves `vllm-sr-network` in place and
keeps the local Ollama by default (set `STOP_LOCAL_OLLAMA=1` to remove it too).

### Manual run order

Bring up the datacenter box first, then the edge gateway, then smoke-test.

```bash
# 1. On Halo-B (the datacenter box): start the remote Ollama with the big models.
bash server-bring-up.sh
#    verify: curl http://localhost:11434/api/tags

# 2. On Halo-A (the edge/gateway box): set the real Halo-B address, then serve.
export HALO_B_IP=192.0.2.20        # the routable IP/host of Halo-B
bash client-bring-up.sh
#    verify: vllm-sr status

# 3. From Halo-A (or any host that can reach the gateway): run the smoke test.
python smoke_test.py               # default --base-url http://localhost:8899
```

The smoke test makes cross-box routing the headline: an easy factual request
must be served by an EDGE model (`qwen/qwen3.5-rocm` or
`google/gemini-2.5-flash-lite`) on Halo-A, and a hard reasoning request must be
served by a DATACENTER model (`google/gemini-3.1-pro` or `openai/gpt5.4`) on
Halo-B.

## Dashboard login

The dashboard (status / monitoring / tracing UI) requires an admin login. To
keep the PoC viewable without a manual first-run bootstrap, `client-bring-up.sh`
provisions a demo admin and forwards it to the gateway container (these are on
the `vllm-sr serve` env passthrough allowlist):

| Env var | Default | Notes |
| --- | --- | --- |
| `DASHBOARD_ADMIN_EMAIL` | `admin@demo.local` | Login email. |
| `DASHBOARD_ADMIN_PASSWORD` | `vllmsr-demo` | Override for any non-demo use. |
| `DASHBOARD_ADMIN_NAME` | `Admin` | Display name. |

Override any of them in the environment before bring-up, e.g.:

```bash
DASHBOARD_ADMIN_EMAIL=me@example.com DASHBOARD_ADMIN_PASSWORD='s3cret' \
  HALO_B_IP=192.0.2.20 bash client-bring-up.sh
```

The dashboard's `EnsureBootstrapAdmin` is idempotent: it creates the admin only
if that email does not already exist, so re-running bring-up against an
already-bootstrapped database is safe — **no volume wipe is needed** to get a
working login.

## Networking

This is the part that makes or breaks the two-box run.

1. **Use a routable IP/host for remote backends, not the docker DNS name.**
   In the single-box recipe the backend endpoint is the docker-network DNS name
   `ollama:11434`, which only resolves on the box that runs that container. For
   the datacenter tiers we therefore use `HALO_B_IP:11434` (a routable IP/host).
   The committed `poc-client-edge.yaml` keeps a literal `HALO_B_IP` placeholder
   so the remote backends are obvious; `client-bring-up.sh` renders a runtime
   copy with the `HALO_B_IP` env var substituted in (it never mutates the
   committed file). The edge tiers stay on the local docker DNS name
   (`ollama:11434`); the frontier/premium tier now calls the real external
   Anthropic public API (`https://api.anthropic.com`) instead of a local mock.

2. **Open the right ports and verify reachability.**
   - Halo-A: `8899` (the OpenAI-compatible gateway ingress the app calls).
   - Halo-A outbound: `api.anthropic.com:443` (HTTPS egress for the
     frontier/premium tier). Also `export ANTHROPIC_API_KEY=sk-ant-...` before
     serving — `vllm-sr serve` auto-injects it into the gateway container. If
     unset, local tiers still work and only premium requests fail.
   - Halo-A outbound (optional, AMD premium): `llm-api.amd.com:443` for the
     `amd/claude-opus-4.8` tier (AMD's Anthropic-compatible gateway). Set
     `export AMD_OCP_APIM_KEY=<subscription-key>` before serving;
     `client-bring-up.sh` renders it into the runtime config as the
     `Ocp-Apim-Subscription-Key` header (the committed yaml holds only the
     `__AMD_OCP_APIM_KEY__` placeholder, and the rendered copy under
     `.vllm-sr-rendered/` is gitignored). If unset, every other tier still works
     and only `amd/claude-opus-4.8` requests fail.
   - Halo-B: `11434` (the Ollama endpoint the gateway escalates to).
   The Envoy data-plane runs inside a container on Halo-A and must be able to
   reach `HALO_B_IP:11434`. Open the firewall on both boxes and confirm the two
   are on the same routable network. Because the call originates from inside a
   container, you may need host networking or the docker host-gateway route so
   the container can reach the LAN address `HALO_B_IP`. Verify end to end with:

   ```bash
   # on Halo-A host:
   curl http://${HALO_B_IP}:11434/api/tags
   # from inside the gateway/edge container, e.g.:
   docker exec <gateway-or-ollama-container> curl -s http://${HALO_B_IP}:11434/api/tags
   ```

3. **Validate the config before serving.**
   Use the gen-dsl flow to statically check the routing surface (requires Go and
   the prebuilt candle binding):

   ```bash
   bash gen-dsl.sh
   ```

   The DSL only encodes `routing.*`, so the `HALO_B_IP` placeholder in the
   provider endpoints does not affect decompile/validate.

## Models

The gateway container mounts `<config_dir>/models` -> `/app/models`, where
`config_dir` is the directory of the config passed to `vllm-sr serve`. Because
`client-bring-up.sh` serves the rendered config from `.vllm-sr-rendered/`, the
mounted models dir is `.vllm-sr-rendered/models`. To avoid duplicating the
large model downloads, `client-bring-up.sh` symlinks
`.vllm-sr-rendered/models` to the pre-staged single-box models tree
(`../strix-halo-poc/models`) so the router uses the presidio PII model that
ships `pii_type_mapping.json` and the exported `onnx/model.onnx`.

Pre-staging is required, not optional. The config path
`models/pii_classifier_modernbert-base_presidio_token_model` is a registry
alias whose auto-download target is the Hugging Face repo
`llm-semantic-router/mmbert-pii-detector-merged`, which does NOT ship
`pii_type_mapping.json`. If the mounted models dir is empty, the router
auto-downloads that repo and then fatals at startup with
`failed to read PII mapping file: ...pii_type_mapping.json: no such file or directory`.
`client-bring-up.sh` therefore hard-fails before serving if the pre-staged
presidio dir is missing either `pii_type_mapping.json` or `onnx/model.onnx`;
prepare them via the single-box download (Gate B) and bring-up `[4/5]` ONNX
export in [../strix-halo-poc/REHEARSAL.md](../strix-halo-poc/REHEARSAL.md).

## Multi-candidate selection demo

Most decisions in `poc-client-edge.yaml` have a single `modelRefs` entry, so
runtime selection short-circuits to `single`. The `reasoning_deep` decision is
the explicit multi-candidate demo: it offers **two** candidates —
`google/gemini-3.1-pro` (datacenter complex tier) and
`google/gemini-2.5-flash-lite` (edge medium tier) — and an `algorithm:` block of
`type: multi_factor`, which scores candidates on quality / latency / cost / load.
`multi_factor` is dependency-free (unlike `session_aware`, which needs a
pretrained KNN model), so it works in the offline PoC. Send a deep-reasoning
prompt and confirm the response's `x-vsr` selection method is not `single`.

## Fleet-sim TCO closer

`router-replay` is enabled in the gateway config (`global.services.router_replay`,
`store_backend: postgres`) and exposed read-only at `GET :8899/v1/router_replay`.
[`export-replay-trace.sh`](export-replay-trace.sh) pages that API and reshapes the
records into fleet-sim's `semantic_router` JSONL (renames `completion_tokens` ->
`generated_tokens`, RFC3339 -> epoch seconds, drops null-token rows):

```bash
BASE_URL=http://localhost:8899 OUT=poc-trace.jsonl bash export-replay-trace.sh
# then drive a fleet-sim capacity/TCO simulation from the real PoC decisions:
pip install -e ../../../src/fleet-sim
python3 ../../../src/fleet-sim/examples/semantic_router_trace_replay.py poc-trace.jsonl selected_model
```

**Honest caveat:** fleet-sim's GPU pools are hardcoded NVIDIA (`a100`/`a10g`), so
the resulting `$/yr` and node/GPU counts are a **pipeline demonstration with
default profiles, NOT an Instinct-calibrated TCO**. A calibrated MI350P profile
is tracked as follow-up tech debt.

## WAN-latency contrast

On the LAN the per-hop cost of escalating to Halo-B is ~0.2 ms, which understates
the edge-gateway advantage. [`wan-latency-experiment.sh`](wan-latency-experiment.sh)
injects synthetic WAN latency on Halo-B with `tc netem` at 0 / 20 / 50 ms and
re-measures the network hop from Halo-A, then prints a `delay vs measured hop`
table:

```bash
HALO_B_IP=192.0.2.20 HALO_B_SSH=ubuntu@halob bash wan-latency-experiment.sh
```

It needs **sudo on Halo-B** (for `tc`) and SSH access from Halo-A (same env vars
as `deploy-2box.sh`; the NIC is auto-detected or set via `HALO_B_IFACE`). A
cleanup trap always removes the netem qdisc on exit, so Halo-B is never left
with injected latency. The point: routine (edge-served) requests take 0 network
hops and are unaffected, while only datacenter escalations pay the added WAN
cost — which is exactly why the gateway lives at the edge.

## Frontier / premium tier

The frontier/premium alias is `anthropic/claude-opus-4.6`. The recipe supports
two backends for it:

- **Offline `llm-katan` echo mock (intended PoC default).** `deploy-2box.sh`
  starts an `llm-katan` container (`--backend echo`) on `vllm-sr-network:8000`
  (skip with `SKIP_FRONTIER=1`). It is an instant echo, **not real generation** —
  fit for validating the pipeline offline, not as a latency/cost baseline. To
  route the frontier tier at it, point the `anthropic/claude-opus-4.6`
  `backend_refs` endpoint at `llm-katan:8000`.
- **Real Anthropic public API.** Point the `backend_refs` at
  `https://api.anthropic.com` and export `ANTHROPIC_API_KEY` (already on the
  `vllm-sr serve` env passthrough allowlist; Halo-A also needs outbound HTTPS
  egress to `api.anthropic.com:443`). If the key is unset, the local edge and
  datacenter tiers still work and only premium requests fail.

> Note: the committed `poc-client-edge.yaml` currently wires this tier to the
> real Anthropic API. For a fully offline demo, repoint its `backend_refs` to the
> `llm-katan:8000` mock that `deploy-2box.sh` already launches.

## Related

- Topology doc: [../../../docs/poc/07-client-server-topology.md](../../../docs/poc/07-client-server-topology.md)
- Single-box recipe (the base this is derived from): [../strix-halo-poc](../strix-halo-poc)
- Reference routing profile: [../balance.yaml](../balance.yaml)
- Multi-node and operator path: [../../../docs/poc/06-multi-node-and-operator.md](../../../docs/poc/06-multi-node-and-operator.md)
