# Hardware Re-run Checklist — CCP Hardening on the Strix Halo Fleet

A glanceable one-pager for **re-running** the 2-box hardware validation on Halo-A
and knowing exactly what "green" looks like. It reflects the landed fixes
(per-box remote key/cert paths, fail-fast agent preflight, truthful metrics). For
the why, the per-step rationale, and the optional N-box / warm-standby drills, read
the full [hardware validation runbook](hardware-validation-runbook.md).

> Everything runs **ON Halo-A, from the recipe dir**
> (`deploy/recipes/strix-halo-fleet-2box`). The one-shot orchestrator is
> [`run-hardware-validation.sh`](../run-hardware-validation.sh); it reuses the
> existing scripts and is **opt-in** (with none of the security env set the
> default flow is byte-identical HMAC-over-HTTP).

---

## Fleet addresses (from the last run's `fleet.env`)

```bash
export HALO_A_IP=10.96.30.46                 # Halo-A address reachable FROM Halo-B (CCP URL + cert SAN)
export HALO_B_IP=10.96.31.132
export HALO_B_SSH=test001@10.96.31.132
export HALO_B_REPO=<Halo-B semantic-router checkout>   # operator fills; likely /home/test001/yy/workspace/semantic-router
```

- `HALO_A_IP`, `HALO_B_IP`, `HALO_B_SSH`, `HALO_B_REPO` are all **required**;
  [`run-hardware-validation.sh`](../run-hardware-validation.sh) fails fast if any
  is unset (even for a dry run).
- `HALO_B_REPO` is **not** persisted in `fleet.env`, so the operator fills it each
  time — the Halo-B checkout root (gateway mode), likely
  `/home/test001/yy/workspace/semantic-router`.
- Prereq: key-based SSH to Halo-B so nothing ever prompts —
  `ssh-copy-id test001@10.96.31.132`.

---

## Step 0 — Offline gates (seconds, no hardware)

```bash
python3 verify_local.py     # expect tail: 20/20 checks passed
bash run-tests.sh           # expect tail: RESULT: PASS
```

- `verify_local.py` is the orchestrator's own Step 0 gate — the run aborts unless
  it reports `20/20 checks passed`.
- `bash run-tests.sh` is the separate offline authoring/CI gate: `bash -n` +
  `py_compile` + `shellcheck` + `test_fleet_metrics.py` + `test-remote-agent-env.sh`.

---

## Step 1 — Clean slate (clear leftover CCP/agents)

```bash
HALO_B_SSH=test001@10.96.31.132 bash teardown-fleet-2box.sh
```

Then confirm nothing stray is left behind:

```bash
pgrep -af 'ccp_server.py|fleet_agent.py'     # expect: no matches
# and nothing still listening on the CCP port :9300 (CCP_PORT default)
```

> [`teardown-fleet-2box.sh`](../teardown-fleet-2box.sh) stops the CCP + Halo-A
> router/agent and, over SSH, every remote router/agent, reaping a stale agent
> that outlived its pidfile via `pkill -f 'fleet_agent.py --tag <box>'`. It reads
> `HALO_B_SSH` from the environment or the saved `fleet.env`.

---

## Step 2 — Dry run (prints the plan, touches no hardware)

```bash
DRY_RUN=1 HALO_A_IP=10.96.30.46 HALO_B_IP=10.96.31.132 \
  HALO_B_SSH=test001@10.96.31.132 HALO_B_REPO=<repo> \
  bash run-hardware-validation.sh
```

Prints the ordered plan + a prerequisite check and exits 0; touches no hardware.

---

## Step 3 — The real run (on Halo-A, from the recipe dir)

```bash
HALO_A_IP=10.96.30.46 HALO_B_IP=10.96.31.132 \
  HALO_B_SSH=test001@10.96.31.132 HALO_B_REPO=<repo> \
  bash run-hardware-validation.sh
```

Optional flags (all default OFF):

- `FORCE=1` — re-mint keys/certs even if `./keys` + `./mtls-certs` already exist.
- `RUN_REGRESSION=1` — also run the Step 1 default-flow (HMAC/HTTP) baseline first.
- `RUN_STANDBY_DRILL=1` (+ `STANDBY_SSH` + `STANDBY_HOST`) — also run the Step 7
  warm-standby promotion drill.

What it does: Step 0 gate → **2a** mint + selftest Ed25519 keypair + mTLS material
→ **2b** stage each box's own pub key + CA + client cert/key to `~/keys` +
`~/mtls-certs` (sets `FLEET_REMOTE_STAGED=1`) → **2c** export the Ed25519 + TLS +
mTLS env → **2d** secure `run-all-2box.sh`, then `verify-hardening.sh` (covers
Steps 2–6).

> `FLEET_REMOTE_STAGED=1` is what makes the deploy forward each **remote** its OWN
> home-relative staged paths (`~/keys/ccp_ed25519.pub`, `~/mtls-certs/ca-cert.pem`,
> and that box's `~/mtls-certs/<box>-client-{cert,key}.pem`) instead of Halo-A's
> local `$PWD` paths.

---

## PASS checklist (what "green" means)

- **Fast-fail sanity:** a crash-on-start edge agent is now caught in ~3s at that
  box — `ERROR: [<box>] pull agent exited within 3s (crash on start).` plus a
  20-line agent-log tail — instead of a 120s mystery convergence timeout
  (`FLEET_AGENT_HEALTHCHECK_SECS`, default 3; set 0 to opt out). A clean run shows
  none of this.
- **Convergence:** the deploy `[5/6]` prints `all boxes converged (halo-a,halo-b).`;
  `python3 fleetctl.py status` (and `fleet-status.txt`) shows BOTH boxes at the
  desired version with `result=in_sync`, and `wait-converged` prints
  `converged: desired=vN [halo-a=ok halo-b=ok]`; NO
  `TIMEOUT waiting for convergence`.
- **Halo-B specifically (the original bug):** its `halo-b-agent.log` has NO
  `No such file ... ccp_ed25519.pub` and it reaches `applied`/`in_sync`; no
  `Connection reset by peer` spam anywhere.
- **Metrics (`metrics.txt` + `metrics.json`):** the summary shows
  `hash_agreement=True` and
  `convergence: N versions all-boxes (converged_all=True); cross-box span mean=... max=... s (poll=<n>s)`
  — a real `poll=` number (never `Nones`), `converged_all=True`, and
  `converged_versions` == the versions applied, with no absurd cross-run span.
  `metrics.json` also carries `hot_reload_latency_seconds` (p50/p95).
- **verify-hardening** ends `ALL VERIFY-HARDENING CHECKS PASSED` (expect
  `8 passed, 0 failed, 1 skipped`):

```
== verify-hardening (mode=gateway, sign=ed25519, ccp=https, boxes=halo-a,halo-b) ==
[PASS] R1 drift-heal on gateway (out-of-band comment reverted via /config/hash)
[PASS] auto-rollback R8 (bad config -> .bak restored, rolled_back, gateway still serving)
[PASS] CCP restart durability R6 (GET /fleet/desired keeps last version+hash, no v1 reset)
[PASS] Ed25519 fleet converges over https (R4/R5/C1: boxes=halo-a,halo-b)
[PASS] Ed25519 forge + HMAC-downgrade rejected by the deployed public key (R4)
[PASS] metrics R9: GET /metrics exposes version-lag + outcome counters (token-gated)
[PASS] metrics R9: fleet_metrics.py emits hot_reload_latency_seconds p50/p95 from audit.log
[SKIP] N-box R7 (2 box(es); needs >2 via fleet.hosts / FLEET_BOXES)
[PASS] warm-standby dry-run C2 (ccp-standby-sync.sh replica -> fresh CCPState restores latest)
== verify-hardening summary: 8 passed, 0 failed, 1 skipped ==
ALL VERIFY-HARDENING CHECKS PASSED (1 skipped as not-applicable)
```

- **Top-level:** `run-all-2box.sh` prints `PASS: deploy + verify completed (mode=gateway).`;
  the orchestrator ends `hardware validation completed; verify-hardening reported all checks passed`
  (rc=0).

---

## Evidence bundle to archive (auto-collected under `/tmp/vllm-sr-fleet/run-*`)

- `verify_local-*.txt` — the Step 0 `20/20 checks passed`.
- `run-all.log` — contains `PASS: deploy + verify completed`.
- `verify-hardening-*.log` — the full `[PASS]`/`[SKIP]` block above.
- `fleet-status.txt` + `fleet-audit.txt` + `audit.log` — converged version/hash + audit rows.
- `metrics.txt` + `metrics.json` — `converged_all=True` + `hot_reload_latency_seconds` p50/p95.
- `router-image-digests.txt` — the same pinned router image per box (R3).

> A concrete captured run is recorded in
> [`validation-record-20260717.md`](validation-record-20260717.md) (run
> `run-20260717-142520`: `8 passed, 0 failed, 1 skipped`, R8 `[PASS]`), with the
> committed human-readable proof files under
> [`validation-evidence/run-20260717-142520/`](validation-evidence/run-20260717-142520/).

---

## Troubleshooting quick map

| Symptom | Likely cause + fix |
| --- | --- |
| Convergence stalls; `[5/6]` prints `ERROR: boxes did not converge in time.` | Read the inline per-box tail under `==> [5/6] agent log tails (convergence failed):` — the real reason is at the box that failed. |
| `pull agent exited within 3s (crash on start)` at a box | The agent died on start (bad env / unreadable key file); its 20-line log tail is printed right there. Fix the reported cause and re-run. |
| `No such file ... ccp_ed25519.pub` on a remote | The per-box path fix should prevent it; if seen, confirm Step 2b staged `~/keys` + `~/mtls-certs` on that box and that `FLEET_REMOTE_STAGED=1` was exported. |
| `signature mismatch` / `Connection reset by peer` spam | Stale agent/keys from an old run — run `teardown-fleet-2box.sh`, then re-run (agent reaping is automatic now). |
