# Production Hardening — CCP Core (R4/R5/R6/R8/R9)

This documents the production-hardening added to the pull-mode control plane core
(`fleet_lib.py`, `ccp_server.py`, `fleet_agent.py`, `fleet_metrics.py`, plus the
vendored `_ed25519.py`). It implements plan todos **R8** (health-gated apply +
auto-rollback), **R6** (CCP durability), **R4** (asymmetric signing + anti-downgrade
+ constant-time token), **R5** (opt-in TLS) + **C1** (mTLS client certs), and
**R9** (observability).

> **Everything here is OPT-IN with a safe fallback.** With no new env set, the
> recipe behaves exactly as before (HMAC-signed bundles over plain HTTP), and the
> offline proof `python3 verify_local.py` stays green (now **16/16**). The stdlib-
> only / importable-on-a-bare-box property is preserved: Ed25519 is a vendored
> pure-Python reference implementation, TLS uses stdlib `ssl`.

---

## What changed, per todo

- **R8 — health-gated apply + auto-rollback (`fleet_agent.py`).** After writing a
  new config and waiting for hash convergence, the agent now confirms the router
  still *serves* (liveness on `GET /config/hash`, plus an optional stronger probe
  at `ROUTER_HEALTH_PATH`). On an **unhealthy** apply **or** a **non-converged**
  apply it restores the `.bak` (in place, same inode — the fsnotify-safe path is
  preserved) and reports `rolled_back`. It then **backs off** the failed version
  (exponential, capped) so one bad config cannot thrash the router every cycle. A
  later, different version clears the backoff immediately.

- **R6 — CCP durability (`ccp_server.py`).** On startup the CCP restores the latest
  persisted `desired/<vN>.yaml` (config + version counter) and the tail of
  `audit.log` (running total + last status per box). A restart no longer forgets
  the desired config (previously it 404'd until someone re-POSTed) nor resets the
  version counter (previously it re-issued `v1` and collided with history). The
  unbounded in-memory audit list is now a **bounded deque** (full history stays on
  disk); `audit_count` still reports the true running total, so the HTTP shape is
  unchanged.

- **R4 — asymmetric signing + anti-downgrade + constant-time token.**
  `fleet_lib` gained an **Ed25519** signing mode (CCP signs with a private seed;
  agents verify with the public key only). HMAC remains the default/fallback. The
  verifier enforces the declared algorithm, so an Ed25519 agent rejects a
  downgraded HMAC bundle. The agent rejects any bundle whose version is **older**
  than the last applied (`anti-downgrade`), and can optionally reject stale bundles
  by timestamp. `ccp_server._authed` now uses `hmac.compare_digest` (no token
  timing side-channel).

- **R5/C1 — opt-in TLS + mTLS (`fleet_lib.py`, `ccp_server.py`).** The CCP serves
  HTTPS when `CCP_TLS_CERT`/`CCP_TLS_KEY` are set (optional mTLS via
  `CCP_TLS_CLIENT_CA`); the client (agent / `fleetctl` / `fleet_metrics`) uses TLS
  automatically for any `https://` CCP URL, trusting `FLEET_TLS_CA` (or the system
  store). **C1** closes the loop: when `FLEET_TLS_CLIENT_CERT` +
  `FLEET_TLS_CLIENT_KEY` are both set, `client_ssl_context` also `load_cert_chain`s
  so the client *presents* a certificate and an mTLS CCP accepts it (either var
  alone is ignored, so the server-auth-only path is unchanged). The bare
  `python3 ccp_server.py` **default bind is now `127.0.0.1`** (was `0.0.0.0`); the
  multi-box deploy explicitly opts into a reachable bind.

- **R9 — observability.** The agent times the **write→converge** window and reports
  it (`apply_seconds`); status reports are **buffered locally** across CCP downtime
  and re-sent on recovery (no lost outcomes). The CCP exposes a Prometheus
  `GET /metrics` (per-box version-lag, last apply seconds, apply-outcome counters).
  `fleet_metrics.py` computes **sub-second p50/p95 hot-reload latency** from the
  new timer.

---

## New opt-in environment variables

All default to today's behavior when unset. Existing vars (`CCP_URL`, `ROUTER_API`,
`CONFIG_FILE`, `FLEET_SIGNING_KEY`, `FLEET_TOKEN`, `BOX_ID`, `POLL_INTERVAL`,
`APPLY_TIMEOUT`) are unchanged.

### Ed25519 asymmetric signing (R4)

| Var | Side | Meaning |
| --- | --- | --- |
| `FLEET_SIGN_MODE` | both | `hmac` (default) or `ed25519`. Must match on CCP + agents. |
| `FLEET_ED25519_SECRET` | CCP | 64-hex (32-byte) private seed the CCP signs with. |
| `FLEET_ED25519_SECRET_FILE` | CCP | Path to a file holding the hex seed (preferred; keep `0600`). |
| `FLEET_ED25519_PUBLIC` | agent | 64-hex (32-byte) public key the agent verifies with. |
| `FLEET_ED25519_PUBLIC_FILE` | agent | Path to a file holding the hex public key. |
| `FLEET_BUNDLE_TS` | CCP | `1` → stamp each bundle with a signed UTC timestamp (freshness). |
| `FLEET_BUNDLE_MAX_AGE` | agent | Seconds; reject a signed bundle older than this (needs `FLEET_BUNDLE_TS`). `0` = off. |

Generate a keypair (no third-party tools needed):

```bash
python3 _ed25519.py keygen --out-dir ./keys
#   -> ./keys/ccp_ed25519.seed  (private, 0600)  -> FLEET_ED25519_SECRET_FILE (on the CCP)
#   -> ./keys/ccp_ed25519.pub   (public)          -> FLEET_ED25519_PUBLIC_FILE (on every agent)
python3 _ed25519.py selftest    # validates the vendored impl vs the RFC 8032 vectors
```

Then, e.g. on the CCP box: `FLEET_SIGN_MODE=ed25519 FLEET_ED25519_SECRET_FILE=./keys/ccp_ed25519.seed …`
and on each agent: `FLEET_SIGN_MODE=ed25519 FLEET_ED25519_PUBLIC_FILE=./keys/ccp_ed25519.pub …`.
In Ed25519 mode the agent no longer needs `FLEET_SIGNING_KEY`.

> The anti-downgrade version check and the constant-time token compare are **always
> on** and need no env; they are backward compatible (equal versions are still
> allowed so drift self-heal can re-apply the current desired config).

### TLS + mTLS (R5/C1)

| Var | Side | Meaning |
| --- | --- | --- |
| `CCP_TLS_CERT` | CCP | Path to the server certificate (PEM). Enables HTTPS with `CCP_TLS_KEY`. |
| `CCP_TLS_KEY` | CCP | Path to the server private key (PEM). |
| `CCP_TLS_CLIENT_CA` | CCP | Optional: require + verify client certs (mTLS). |
| `CCP_HOST` | CCP | Bind address. Code default `127.0.0.1`; `ccp-bring-up.sh` default `0.0.0.0` (override to tighten). |
| `FLEET_TLS_CA` | client | Path to a CA/cert to trust the CCP's TLS certificate. |
| `FLEET_TLS_INSECURE` | client | `1` → skip verification (self-signed / dev only). |
| `FLEET_TLS_CLIENT_CERT` | client | Path to the client certificate (PEM) to **present** for mTLS. Needs `FLEET_TLS_CLIENT_KEY`. |
| `FLEET_TLS_CLIENT_KEY` | client | Path to the client private key (PEM). Set BOTH or neither — either alone is ignored. |

To use TLS end-to-end the CCP URL the agents pull must be `https://…` (set
`CCP_URL=https://<host>:<port>`). Generate a self-signed **server** cert for a lab:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout ccp-key.pem -out ccp-cert.pem \
  -subj "/CN=<halo-a-host-or-ip>" -addext "subjectAltName=IP:<halo-a-ip>"
# CCP:    CCP_TLS_CERT=ccp-cert.pem CCP_TLS_KEY=ccp-key.pem
# agents: CCP_URL=https://<halo-a-ip>:9300 FLEET_TLS_CA=ccp-cert.pem
```

#### End-to-end mTLS with the provisioning helper (C1)

Server-auth TLS proves only the CCP's identity to agents. **mTLS** additionally
proves each agent's identity to the CCP: the CCP requires a client certificate
(`CCP_TLS_CLIENT_CA`) and every client *presents* one (`FLEET_TLS_CLIENT_CERT` +
`FLEET_TLS_CLIENT_KEY`). `make-mtls-certs.sh` mints all of it with `openssl` only
(no Python deps) — a self-signed CA, a SAN-bound CCP server cert, and one client
cert per agent box — and prints the env-var mapping for every file:

```bash
bash make-mtls-certs.sh --host <halo-a-host-or-ip> --agents "halo-a halo-b"
#   -> ./mtls-certs/{ca,ccp}-*.pem + <box>-client-*.pem  (see printed mapping)
```

Then wire the files on (all opt-in; unset ⇒ today's HMAC-over-HTTP is unchanged):

```bash
# CCP (Halo-A): its server cert + the CA it verifies client certs against.
export CCP_TLS_CERT=mtls-certs/ccp-cert.pem CCP_TLS_KEY=mtls-certs/ccp-key.pem
export CCP_TLS_CLIENT_CA=mtls-certs/ca-cert.pem
# Each agent box: the CA (to verify the CCP) + THIS box's client cert/key.
export FLEET_TLS_CA=mtls-certs/ca-cert.pem
export FLEET_TLS_CLIENT_CERT=mtls-certs/<box>-client-cert.pem
export FLEET_TLS_CLIENT_KEY=mtls-certs/<box>-client-key.pem
```

`deploy-fleet-2box.sh` forwards `FLEET_TLS_CLIENT_CERT`/`FLEET_TLS_CLIENT_KEY` to
remote agents (they ship in the default `FLEET_SECURITY_AGENT_VARS`) and builds
`https://` CCP URLs automatically once `CCP_TLS_CERT`/`CCP_TLS_KEY` are set. As
with the other path-valued vars, the *value is a path*: stage each box's own
client cert at that path on that box (the CA private key never leaves wherever you
ran the helper). `ccp-bring-up.sh` also presents the client cert on its **local
liveness probe**, so CCP bring-up still succeeds when mTLS is required.

### Health-gated apply + auto-rollback (R8)

| Var | Side | Meaning |
| --- | --- | --- |
| `ROUTER_HEALTH_PATH` | agent | Extra readiness probe hit after apply (e.g. `/health`). Unset = liveness via `GET /config/hash` 200 only. |
| `ROUTER_HEALTH_TIMEOUT` | agent | Seconds for the health probe (default `5`). |
| `APPLY_BACKOFF` | agent | Initial backoff (seconds) after a failed apply (default `30`). |
| `APPLY_BACKOFF_MAX` | agent | Backoff cap in seconds (default `300`). |

### CCP durability + audit (R6) / status buffering (R9)

| Var | Side | Meaning |
| --- | --- | --- |
| `CCP_AUDIT_MEMORY_MAX` | CCP | Bounded in-memory audit view size (default `1000`; full history on disk). |
| `STATUS_BUFFER` | agent | Path to the local status buffer (default: beside the config file). |
| `STATUS_BUFFER_MAX` | agent | Max buffered status reports kept across a CCP outage (default `1000`). |
| `CCP_AUDIT_LOG` | metrics | `fleet_metrics.py`: path to a JSON `audit.log` to compute p50/p95 latency offline. |

---

## Observability (R9)

- **`GET /metrics`** (Prometheus text; behind the bearer token — a scraper sends
  `Authorization: Bearer <token>`):
  - `fleet_desired_version_number` — current desired version.
  - `fleet_box_version_lag{box_id}` — desired minus the box's applied version.
  - `fleet_box_last_apply_seconds{box_id}` — last write→converge time reported.
  - `fleet_apply_outcomes_total{box_id,result}` — counters (`applied`, `in_sync`,
    `rolled_back`, `rejected`, …).
  - `fleet_audit_records_total`, `fleet_boxes`.
- **Hot-reload latency**: agents report `apply_seconds` on each apply; the CCP keeps
  it on each audit record. `fleet_metrics.py` reads a JSON audit source (a bundled
  `audit.log`/`fleet-audit.jsonl`, or `CCP_AUDIT_LOG=`) and emits
  `hot_reload_latency_seconds` (`p50`/`p95`/`mean`/`min`/`max`/`n`) — the sub-second
  measurement the research pipeline (§4) called out as missing.

---

## Forwarding these vars from the deploy scripts

`deploy-fleet-2box.sh` has an opt-in security-var pass-through that (a) exports set
vars locally (so the Halo-A CCP + agent inherit them) and (b) forwards the
**agent-side** ones to remote boxes over SSH. The default lists already carry the
finalized names — including the C1 client-cert vars — so **no override is needed**
for the standard setup (shown here for reference; override only to rename):

```bash
FLEET_SECURITY_CCP_VARS="FLEET_SIGN_MODE FLEET_ED25519_SECRET FLEET_ED25519_SECRET_FILE FLEET_BUNDLE_TS CCP_TLS_CERT CCP_TLS_KEY CCP_TLS_CLIENT_CA CCP_AUDIT_MEMORY_MAX"
FLEET_SECURITY_AGENT_VARS="FLEET_SIGN_MODE FLEET_ED25519_PUBLIC FLEET_ED25519_PUBLIC_FILE FLEET_TLS_CA FLEET_TLS_INSECURE FLEET_TLS_CLIENT_CERT FLEET_TLS_CLIENT_KEY FLEET_BUNDLE_MAX_AGE"
FLEET_AGENT_EXTRA_VARS="ROUTER_HEALTH_PATH ROUTER_HEALTH_TIMEOUT APPLY_BACKOFF APPLY_BACKOFF_MAX"
```

Notes for the deploy:
- `FLEET_SIGN_MODE` must reach **both** the CCP and every agent (it is in the agent
  list the deploy forwards to remotes, and the deploy also exports both lists
  locally so the Halo-A CCP + agent see it).
- TLS end-to-end is automatic: when `CCP_TLS_CERT`/`CCP_TLS_KEY` are set the deploy
  builds the agent `CCP_URL` as `https://…` (or force it with `FLEET_CCP_SCHEME=https`).
- Client-cert paths are **per-box**: the deploy forwards the *variable* to each
  remote, so stage that box's own `FLEET_TLS_CLIENT_CERT`/`_KEY` file at the
  exported path on each box (same convention as `FLEET_TLS_CA` / the `*_FILE` vars).
- `ccp-bring-up.sh` / `node-bring-up.sh` (owned by the core) pass all of these
  through to the Python processes when set; unset = unchanged behavior.

---

## Limitations / follow-ups

- **Vendored Ed25519 is a reference implementation** (correct — validated against
  the RFC 8032 test vectors and byte-for-byte against `libsodium`/`cryptography` —
  but not fast and not side-channel hardened). A production fleet should swap in a
  native library (`cryptography`/PyNaCl); the keys and wire signatures are
  compatible, so no re-keying is needed.
- **mTLS is now supported end-to-end (C1).** The client presents a cert via
  `FLEET_TLS_CLIENT_CERT`/`FLEET_TLS_CLIENT_KEY`, the deploy forwards those to remote
  agents, and `make-mtls-certs.sh` provisions the CA + server + per-agent client
  certs. What remains is operational, not code: the helper issues **self-signed**
  certs and rotation / expiry / revocation (CRL/OCSP) are manual — for production,
  issue from your own CA/PKI and script renewal before the `--days` window lapses.
- **Resolved earlier follow-ups.** The deploy now builds `https://` CCP URLs when
  TLS is enabled (force with `FLEET_CCP_SCHEME=https`), and the run bundle captures
  the raw JSON `audit.log` (and points `fleet_metrics.py` at it via `CCP_AUDIT_LOG`),
  so p50/p95 hot-reload latency is computed from real timer samples — both were
  open items in earlier revisions of this doc.
