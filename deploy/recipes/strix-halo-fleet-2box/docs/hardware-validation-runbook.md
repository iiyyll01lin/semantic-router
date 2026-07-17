# Hardware Validation Runbook — CCP Hardening on the Strix Halo Fleet (Part A)

This is the ordered, copy-pasteable procedure for turning the **offline 20/20**
([`verify_local.py`](../verify_local.py)) into **hardware evidence** on the real
2-box (and optional 3-box) Strix Halo fleet: Ed25519 + TLS + mTLS end-to-end, an
induced auto-rollback on a real router, CCP-restart durability, `/metrics` with
p50/p95 hot-reload latency, N-box convergence, and a warm-standby promotion
drill. It drives the opt-in hardware verifier
[`verify-hardening.sh`](../verify-hardening.sh) plus the C1/C2 helpers
([`make-mtls-certs.sh`](../make-mtls-certs.sh), [`_ed25519.py`](../_ed25519.py),
[`ccp-standby-sync.sh`](../ccp-standby-sync.sh),
[`promote-standby.sh`](../promote-standby.sh)).

> **Re-running?** For a glanceable one-pager — fleet addresses, the exact
> commands, and the PASS/SKIP lines that mean "green" — see the
> [hardware re-run checklist](hardware-rerun-checklist.md). This runbook is the
> full detail behind it.

> **These steps run ON the Strix Halo boxes, not in CI / the authoring
> environment.** Everything here is **opt-in**; with none of the new env set the
> default flow is byte-identical to before (HMAC over plain HTTP). The offline,
> hardware-free proofs of the same behaviors already pass in `verify_local.py`
> (run it first — Step 0).

---

## What each step proves

| Step | Proves | Todo |
| --- | --- | --- |
| 0 | Offline logic is green before touching hardware | — |
| 1 | Default flow still converges both real routers (regression) | baseline |
| 2 | Fleet converges under **Ed25519 + TLS + mTLS**, forged/HMAC bundles rejected | R4/R5/C1 |
| 3 | A config the **real router rejects** triggers **auto-rollback**; gateway keeps serving | R8 |
| 4 | A **CCP restart** keeps the desired version+hash (no 404, no v1 reset) | R6 |
| 5 | `GET /metrics` counters + **p50/p95 hot-reload latency** from `audit.log` | R9 |
| 6 | **N-box** convergence with a 3-entry `fleet.hosts` | R7 |
| 7 | **Warm-standby promotion**: recovery time + zero audit loss | C2/R6 |

Steps 2–6 are all executed by a single `verify-hardening.sh` run; they are broken
out below so you know which env unlocks each check, what PASS looks like, and what
evidence to capture. Step 7 is a separate operator drill.

---

## Prerequisites

- A working 2-box fleet per the [README](../README.md): Halo-A (CCP + edge node)
  and at least Halo-B, with **key-based SSH** set up (`ssh-copy-id`) so runs are
  password-free.
- This recipe directory checked out on Halo-A; commands below run **from it**:
  ```bash
  cd deploy/recipes/strix-halo-fleet-2box
  ```
- For gateway-mode steps (2–6), `vllm-sr` must come up on both boxes (see the
  README "Gateway mode" section); `run-all-2box.sh` / `deploy-fleet-2box.sh`
  auto-provision Halo-B when `HALO_B_MODE=gateway`.
- Pick the fleet-facing addresses once and reuse them:
  ```bash
  export HALO_A_IP=192.0.2.10          # Halo-A address reachable FROM Halo-B
  export HALO_B_IP=192.0.2.20
  export HALO_B_SSH=ubuntu@192.0.2.20
  export HALO_B_REPO=/home/ubuntu/yy/workspace/semantic-router   # gateway mode
  ```

### Conventions used throughout

- **`FLEET_STATE_DIR`** defaults to `${TMPDIR:-/tmp}/vllm-sr-fleet`; the deploy
  writes `fleet.env` there and `run-all-2box.sh` drops each run bundle in
  `run-<timestamp>/`. Export the SAME `FLEET_STATE_DIR` in every shell of a given
  drill.
- **Security env is NOT persisted in `fleet.env`.** `FLEET_SIGN_MODE`, the
  `FLEET_ED25519_*`, `CCP_TLS_*`, and `FLEET_TLS_*` vars are read from your
  **current shell** by the deploy AND by `verify-hardening.sh` / a CCP restart.
  Export them once (Step 2) and keep using that same shell for Steps 2–7.
- **Evidence** goes into the run bundle. Save each verifier console log next to it:
  ```bash
  RUN=$(ls -dt "${FLEET_STATE_DIR:-/tmp/vllm-sr-fleet}"/run-* | head -1)   # newest bundle
  echo "latest run bundle: ${RUN}"
  ```

---

## Step 0 — Offline gate (no hardware)

Always confirm the offline proof is green before spending hardware time.

```bash
python3 verify_local.py
```

**Expected (tail):**

```
[PASS] mTLS: client presenting FLEET_TLS_CLIENT_CERT/KEY is accepted (C1)
[PASS] mTLS: certless client is rejected at the TLS handshake (C1)
[PASS] warm-standby sync replicates desired/ + audit.log (C2, ccp-standby-sync.sh)
[PASS] warm-standby restore: fresh CCPState = latest version+config+audit (C2)

20/20 checks passed
```

**Evidence:** save the full output as `verify_local-YYYYmmdd.txt`.

---

## Step 1 — Default-flow regression (`run-all-2box.sh`)

Prove the **default** (HMAC-over-HTTP) flow still deploys, converges both real
routers, runs the demo, and captures a bundle — the byte-identical baseline the
hardening must not disturb.

```bash
export FLEET_STATE_DIR=/tmp/vllm-sr-fleet          # reuse this dir for the whole drill
HALO_A_MODE=gateway HALO_B_MODE=gateway \
  bash run-all-2box.sh
```

**Expected (tail):**

```
PASS: deploy + verify completed (mode=gateway).
Log bundle (share this whole directory if anything failed):
  /tmp/vllm-sr-fleet/run-YYYYmmdd-HHMMSS
```

`verify-fleet.sh` runs inside it and prints `ALL VERIFY CHECKS PASSED`
(edit-once / rollback / audit; drift-heal `[SKIP]` in gateway mode — that gap is
exactly what Step 2's R1 check closes).

**Evidence to capture (already in the bundle):** `fleet-status.txt`,
`fleet-audit.txt`, `audit.log`, `metrics.json` / `metrics.txt`,
`router-image-digests.txt`, both boxes' `*-router-container.log`. Record the
converged config **hash** (from `fleet-status.txt`) — it goes in the README note
(Step 8).

---

## Step 2 — Ed25519 + TLS + mTLS, then `verify-hardening.sh` (R4/R5/C1)

### 2a. Mint keys + certs (once, on Halo-A)

```bash
# Ed25519 keypair (vendored, stdlib-only). Validate the impl, then generate.
python3 _ed25519.py selftest                       # -> "ed25519 selftest OK (RFC 8032 vectors)"
python3 _ed25519.py keygen --out-dir ./keys
#   -> ./keys/ccp_ed25519.seed  (private, 0600)
#   -> ./keys/ccp_ed25519.pub   (public)

# mTLS material: CA + SAN-bound CCP server cert + one client cert per agent box.
bash make-mtls-certs.sh --host "${HALO_A_IP}" --agents "halo-a halo-b"
#   -> ./mtls-certs/{ca,ccp}-*.pem + halo-a-client-*.pem + halo-b-client-*.pem
```

### 2b. Stage the per-box files on each remote box

Path-valued vars must exist **on the box that reads them**. Copy the CA, the
public key, and **that box's** client cert/key to the same paths on Halo-B:

```bash
ssh "${HALO_B_SSH}" 'mkdir -p ~/keys ~/mtls-certs'
scp ./keys/ccp_ed25519.pub                 "${HALO_B_SSH}:~/keys/"
scp ./mtls-certs/ca-cert.pem               "${HALO_B_SSH}:~/mtls-certs/"
scp ./mtls-certs/halo-b-client-cert.pem    "${HALO_B_SSH}:~/mtls-certs/"
scp ./mtls-certs/halo-b-client-key.pem     "${HALO_B_SSH}:~/mtls-certs/"
```

> The CCP's **private** seed (`ccp_ed25519.seed`) and TLS key (`ccp-key.pem`) and
> the CA **private** key (`ca-key.pem`) never leave Halo-A. Only public/agent
> material is staged out.

### 2c. Export the security env (this shell drives Steps 2–7)

```bash
# --- CCP side (Halo-A only): private seed + server cert + client-CA (mTLS) ---
export FLEET_SIGN_MODE=ed25519
export FLEET_ED25519_SECRET_FILE=$PWD/keys/ccp_ed25519.seed
export CCP_TLS_CERT=$PWD/mtls-certs/ccp-cert.pem
export CCP_TLS_KEY=$PWD/mtls-certs/ccp-key.pem
export CCP_TLS_CLIENT_CA=$PWD/mtls-certs/ca-cert.pem

# --- Agent side (Halo-A uses these local paths; remotes get their OWN staged
#     paths automatically -- see FLEET_REMOTE_STAGED below) --------------------
export FLEET_ED25519_PUBLIC_FILE=$PWD/keys/ccp_ed25519.pub
export FLEET_TLS_CA=$PWD/mtls-certs/ca-cert.pem
export FLEET_TLS_CLIENT_CERT=$PWD/mtls-certs/halo-a-client-cert.pem
export FLEET_TLS_CLIENT_KEY=$PWD/mtls-certs/halo-a-client-key.pem

# Tell the deploy to forward each REMOTE its own home-relative staged paths (from
# 2b) instead of Halo-A's local ones. run-hardware-validation.sh sets this for
# you in stage_certs; set it by hand only for a manual 2b/2c/2d run.
export FLEET_REMOTE_STAGED=1
```

> With `FLEET_REMOTE_STAGED=1` the deploy resolves the four agent **path** vars
> per box: each remote's agent gets `~/keys/ccp_ed25519.pub`,
> `~/mtls-certs/ca-cert.pem`, and **its own** `~/mtls-certs/<box>-client-{cert,key}.pem`
> (the `~` expands to that box's `$HOME`) -- exactly what 2b staged. Halo-A (local)
> keeps the `$PWD` paths above. Without the signal the deploy forwards Halo-A's
> values verbatim (byte-identical to the pre-fix behavior), which is why a remote
> whose home differs from Halo-A's would otherwise hit `ENOENT` on the pub key.
> Because `CCP_TLS_CERT`/`CCP_TLS_KEY` are set, the deploy builds `https://` CCP
> URLs automatically.

### 2d. Re-deploy with the secure transport, then run the verifier

```bash
# Bring the fleet up again under Ed25519 + TLS + mTLS (writes the https fleet.env).
HALO_A_MODE=gateway HALO_B_MODE=gateway bash run-all-2box.sh

# Now the opt-in hardware verifier, in the SAME shell (it reads the security env
# and fleet.env). Enable the gateway drift-heal check explicitly.
FLEET_VERIFY_DRIFT_ON_GATEWAY=1 FLEET_VERIFY_STANDBY=1 \
  bash verify-hardening.sh 2>&1 \
  | tee "${FLEET_STATE_DIR}/verify-hardening-$(date +%Y%m%d-%H%M%S).log"
```

> **Before a re-run (clear stale agents + state).** If a previous secure run
> aborted (e.g. a box failed to converge), tear the fleet down and kill any
> leftover agent BEFORE re-running, so a stale pull agent from the old run cannot
> keep polling the CCP with old key/HMAC and shadow the fresh one:
>
> ```bash
> bash teardown-fleet-2box.sh                 # stop the CCP + local/remote agents
> # If an agent outlived its pidfile (e.g. a ~1d9h-old PID still polling), find and
> # kill it explicitly (it will otherwise keep re-registering an old version):
> pgrep -af fleet_agent.py                     # list any leftover agent(s)
> kill <pid>                                   # e.g. kill 3415909  (escalate: kill -9)
> ```
>
> `node-bring-up.sh` also stops any pre-existing agent for the SAME box before
> starting a fresh one, so this is belt-and-suspenders for an agent that predates
> the guard or was started outside the recipe.

**Expected (full 2-box gateway run):**

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

The **R4/R5/C1** evidence is the two `Ed25519 …` PASS lines: convergence over
`ccp=https` proves the agents pulled a TLS bundle **and presented their client
certs** (mTLS — `fleetctl`/the agents use the same client stack), and the
forge/HMAC-downgrade PASS proves the deployed public key rejects anything not
signed by the real seed.

**Evidence:** the `verify-hardening-*.log` you tee'd; copy it into the run bundle
(`cp "${FLEET_STATE_DIR}"/verify-hardening-*.log "${RUN}/"`). Also grab the CCP
banner line `CCP listening on https://…` from `${FLEET_STATE_DIR}/ccp.log`.

---

## Step 3 — Induced auto-rollback (R8)

This is the `auto-rollback R8` check from Step 2 (gateway mode only). It pushes a
config the **real router rejects on reload** (valid file bytes, invalid YAML), so
the agent never converges to the bad hash, restores the `.bak` in place, reports
`rolled_back`, and the gateway keeps serving. The check **always restores the good
desired** and reconverges before returning.

To watch it directly (optional), tail the audit while the check runs, or inspect
afterward:

```bash
python3 fleetctl.py audit | tail -n 15      # look for a 'rolled_back' row for halo-a
python3 fleetctl.py status                  # both boxes back on the good version+hash
```

**Expected:** an audit row `… halo-a vN rolled_back …` for the bad version, then a
later `applied`/`in_sync` row on the restored good version; `fleetctl status`
shows both boxes agreeing on the good hash again.

**Evidence:** the `auto-rollback R8` PASS line (in the tee'd log); the
`rolled_back` audit row (`python3 fleetctl.py audit > "${RUN}/rollback-audit.txt"`);
the box's `*-router-container.log` line showing the rejected reload
(`runtime_config_load_failed` / parse error) — proof the **real** router refused
it, not a mock.

---

## Step 4 — CCP restart durability (R6)

Also part of Step 2 (`CCP restart durability R6`). The verifier records the
desired version+hash, stops the local CCP (`ccp.pid`), restarts it via
`ccp-bring-up.sh` (which `_restore()`s the persisted state on boot), and asserts
the version+hash are unchanged.

Manual cross-check (optional):

```bash
python3 fleetctl.py desired-version         # e.g. v7
python3 fleetctl.py desired-hash            # e.g. a78aebc5fd5f…
fleet_stop_pidfile "${FLEET_STATE_DIR}/ccp.pid" 2>/dev/null || \
  kill "$(cat "${FLEET_STATE_DIR}/ccp.pid")"
bash ccp-bring-up.sh                         # restart (same shell => same TLS/sign env)
python3 fleetctl.py desired-version          # SAME version (not v1, not 404)
python3 fleetctl.py desired-hash             # SAME hash
```

**Expected:** identical version and hash before/after the restart; the CCP log
shows `CCP listening on https://… (desired_version=vN)` with the **restored** vN.

**Evidence:** the `CCP restart durability R6` PASS line; the pre/post
`desired-version` + `desired-hash` outputs; the `desired_version=vN` line from
`ccp.log`.

---

## Step 5 — `/metrics` + p50/p95 capture (R9)

Also part of Step 2 (`metrics R9` checks). Capture the scrape and the latency
percentiles as standalone artifacts:

```bash
# Token-gated Prometheus scrape (TLS/mTLS-aware via fleet_lib).
python3 - <<'PY' | tee "${RUN:-/tmp}/metrics-scrape.txt"
import os, fleet_lib
url = os.environ["CCP_URL"].rstrip("/") + "/metrics"
st, body = fleet_lib.http_get_text(url, token=os.environ["FLEET_TOKEN"])
print("HTTP", st)
print(body)
PY

# p50/p95 hot-reload latency from the CCP's raw JSON audit.log.
mkdir -p /tmp/lat-bundle && cp "${FLEET_STATE_DIR}/ccp/audit.log" /tmp/lat-bundle/
python3 fleet_metrics.py --bundle /tmp/lat-bundle
```

**Expected:** the scrape is `HTTP 200` and contains `fleet_desired_version_number`,
`fleet_box_version_lag{box_id="halo-a"} 0`, `fleet_box_version_lag{box_id="halo-b"} 0`,
and `fleet_apply_outcomes_total{…,result="applied"}` / `…result="in_sync"` counters.
`fleet_metrics.py` prints a line like:

```
hot_reload_latency (write->converge): p50=0.42s p95=0.87s mean=0.51s (n=6)
```

and writes `hot_reload_latency_seconds` (p50/p95/mean/min/max/n) into
`/tmp/lat-bundle/metrics.json`.

> If the p95 line is missing, there are no `applied` samples with `apply_seconds`
> yet — drive one edit first (`python3 fleetctl.py set-desired <file>` then
> `wait-converged`) and re-run. The verifier `[SKIP]`s p50/p95 in that case rather
> than failing.

**Evidence:** `metrics-scrape.txt` and `metrics.json` (with the
`hot_reload_latency_seconds` block). Copy both into the run bundle.

---

## Step 6 — N-box convergence (R7)

Add a **third** box so `FLEET_BOXES` lists >2 entries, then the verifier's N-box
check runs instead of skipping.

```bash
cp fleet.hosts.example fleet.hosts          # gitignored; then edit it
# Add one active line per REMOTE box (halo-a is implicit), e.g.:
#   halo-b  ubuntu@192.0.2.20  192.0.2.20  gateway  /home/ubuntu/yy/workspace/semantic-router
#   halo-c  ubuntu@192.0.2.30  192.0.2.30  gateway  /home/ubuntu/yy/workspace/semantic-router

# Stage halo-c's Ed25519 pub + client cert/key + CA on halo-c (as in Step 2b),
# minting a halo-c client cert first if needed:
bash make-mtls-certs.sh --host "${HALO_A_IP}" --agents "halo-c"    # reuses the CA

HALO_A_MODE=gateway bash run-all-2box.sh    # N-box: loops over fleet.hosts
FLEET_VERIFY_DRIFT_ON_GATEWAY=1 bash verify-hardening.sh 2>&1 \
  | tee "${FLEET_STATE_DIR}/verify-hardening-nbox-$(date +%Y%m%d-%H%M%S).log"
```

**Expected:** the N-box line now PASSES with all three boxes:

```
[PASS] N-box R7 (all 3 boxes converged: halo-a,halo-b,halo-c)
```

**Evidence:** the N-box PASS line; `fleet-status.txt` from the N-box bundle showing
all three boxes on the same version+hash.

---

## Step 7 — Warm-standby promotion drill (C2)

Full recovery drill with [`ccp-standby-sync.sh`](../ccp-standby-sync.sh) +
[`promote-standby.sh`](../promote-standby.sh). See
[`docs/ha-standby.md`](ha-standby.md) for the architecture; below is the
**planned / graceful (zero-RPO)** drill. `STANDBY` is a second box that runs the
**same** `ccp_server.py` against the replicated state with the **same token +
keys/TLS**.

```bash
export STANDBY_SSH=ubuntu@192.0.2.30
export STANDBY_HOST=192.0.2.30              # address agents will reach the standby on
export STANDBY_STATE_DIR=/tmp/vllm-sr-fleet/ccp

# 1) Final sync (zero RPO), then confirm the stamp.
STANDBY_HOST="${STANDBY_SSH}" SYNC_ONCE=1 bash ccp-standby-sync.sh
cat "${FLEET_STATE_DIR}/ccp-standby-sync.status"

# 2) Capture the baseline audit count and stop the ACTIVE CCP.
source "${FLEET_STATE_DIR}/fleet.env"
EXPECT=$(python3 fleetctl.py status | sed -n 's/.*audit_count=\([0-9]*\).*/\1/p')
fleet_stop_pidfile "${FLEET_STATE_DIR}/ccp.pid" 2>/dev/null || \
  kill "$(cat "${FLEET_STATE_DIR}/ccp.pid")"

# 3) Start the standby ccp_server on the STANDBY box against the synced state dir
#    (same FLEET_TOKEN + keys/TLS as the active). Example, ON the standby:
#      export CCP_STATE_DIR=/tmp/vllm-sr-fleet/ccp FLEET_TOKEN=<same> CCP_HOST=0.0.0.0
#      export FLEET_SIGN_MODE=ed25519 FLEET_ED25519_SECRET_FILE=~/keys/ccp_ed25519.seed
#      export CCP_TLS_CERT=~/mtls-certs/ccp-cert.pem CCP_TLS_KEY=~/mtls-certs/ccp-key.pem
#      export CCP_TLS_CLIENT_CA=~/mtls-certs/ca-cert.pem
#      python3 ccp_server.py           # _restore()s the replicated desired + audit
ssh "${STANDBY_SSH}" '…start ccp_server.py as above…'

# 4) Promote: measure recovery time + zero audit loss, repoint CCP_URL in fleet.env.
STANDBY_HOST="${STANDBY_HOST}" EXPECT_AUDIT_COUNT="${EXPECT}" \
  PROMOTE_SINCE=$(date +%s) bash promote-standby.sh

# 5) Re-broadcast: restart each agent at the new CCP_URL (the promote script prints
#    the exact commands; PROMOTE_APPLY=1 also restarts the local halo-a agent),
#    then confirm convergence against the standby.
source "${FLEET_STATE_DIR}/fleet.env"      # now carries the standby CCP_URL
python3 fleetctl.py wait-converged --boxes "${FLEET_BOXES}" --timeout 120
```

**Expected (tail of `promote-standby.sh`):**

```
==> [1/4] waiting up to 60s for the standby CCP to serve a version
    standby is serving desired v7
    recovery time: 3s (since PROMOTE_SINCE)
==> [2/4] confirming zero audit loss (audit record counts active vs standby)
    ZERO AUDIT LOSS: PASS (vs captured EXPECT_AUDIT_COUNT)
…
  zero audit loss:  PASS (active=42 standby=42)
```

and `wait-converged` prints `converged: desired=v7 [halo-a=ok halo-b=ok]`.

**Evidence:** the full `promote-standby.sh` output (recovery time + `ZERO AUDIT
LOSS: PASS`); the `ccp-standby-sync.status` stamp; the post-promotion
`wait-converged` line; `fleet.env.bak` (the pre-promotion CCP_URL) alongside the
rewritten `fleet.env`.

> **Dry cross-check:** `verify-hardening.sh` with `FLEET_VERIFY_STANDBY=1`
> (Step 2) already proves the replicate→restore half locally (a fresh `CCPState`
> on the synced copy restores the latest version). Step 7 adds the real
> cross-box takeover + RTO/RPO numbers.

---

## Step 8 — Update the README "Verified on hardware" note

After a clean run, record the evidence in the [README](../README.md) hardware
note (owned section only): the **date**, the converged config **hash(es)** (from
`fleet-status.txt`), the **run bundle** name(s), and that `verify-hardening.sh`
passed under Ed25519 + TLS + mTLS (list which checks PASSed vs SKIPPED, e.g.
N-box SKIP on a 2-box run). Keep the honest-boundaries framing (topology/
governance PoC; both boxes are gfx1151 APUs).

---

## Evidence bundle checklist

Collect these into the run bundle (`${RUN}`) and archive the whole directory:

- [ ] `verify_local.py` output — **20/20** (Step 0)
- [ ] `run-all.log` + `PASS: deploy + verify completed` (Step 1)
- [ ] `verify-hardening-*.log` — the PASS/SKIP block (Steps 2–6)
- [ ] `fleet-status.txt` / `fleet-audit.txt` / `audit.log` (converged hash + rows)
- [ ] `rollback-audit.txt` + the router-container reject log (Step 3)
- [ ] pre/post `desired-version` + `desired-hash` + `ccp.log` `desired_version=` (Step 4)
- [ ] `metrics-scrape.txt` + `metrics.json` (`hot_reload_latency_seconds`) (Step 5)
- [ ] N-box `fleet-status.txt` (all 3 boxes agree) (Step 6)
- [ ] `promote-standby.sh` output + `ccp-standby-sync.status` + `fleet.env.bak` (Step 7)
- [ ] `router-image-digests.txt` (every box ran the same pinned image — R3)

---

## Safety notes

- **Opt-in, default byte-identical.** None of this runs unless you set the env /
  flags above; the default `run-all-2box.sh` path is unchanged.
- **R1 drift-heal is safe on a live gateway:** it appends only a **comment** line
  (still a config the router accepts) and the agent reverts it — it never pushes
  the mock/minimal config to a real router.
- **Induced rollback self-restores:** `verify-hardening.sh` always sets the good
  desired back and reconverges (and a trap restores it even on early exit), so the
  fleet ends serving byte-identical config (a new version number is expected).
- **CCP restart / standby** only affect the CCP process and `fleet.env`; agents
  are pull-only and simply re-pull. Full auto-failover (floating IP / quorum) is
  out of scope (see [`docs/ha-standby.md`](ha-standby.md)).
