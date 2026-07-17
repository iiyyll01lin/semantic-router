# 2-Box Hardware Validation Record — 2026-07-17

Durable, in-repo record of the Strix Halo 2-box CCP-hardening hardware
validation. The full machine-generated bundle (container/audit logs, raw JSON)
is archived **outside** the repo; the human-readable proof files are committed
next to this record under
[`validation-evidence/run-20260717-142520/`](validation-evidence/run-20260717-142520/).

## Run

- **Run ID:** `run-20260717-142520`
- **Date:** 2026-07-17
- **Mode:** gateway (real routers), `sign=ed25519`, `ccp=https` (mTLS), boxes `halo-a,halo-b`
- **Fleet:** `HALO_A_IP=10.96.30.46`, `HALO_B_IP=10.96.31.132`

## Validated commit

- **Validated tree:** pre-cleanup tip `fc8074cc`, preserved as branch
  `backup/poc-strix-pre-cleanup` and tag `pre-cleanup-20260717`.
- The same validated content is what the current curated history restructures
  (7 commits `c5b73529 … b32e6ab3`, minus the machine-generated benchmark
  artifacts). This record is committed on top of that curated history.

## verify-hardening result

Summary line: `== verify-hardening summary: 8 passed, 0 failed, 1 skipped ==` —
R8 auto-rollback `[PASS]`.

```text
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

The verify-hardening log itself is archive-only (a `.log`, gitignored); its
integrity is pinned by the SHA256 below.

## Convergence and metrics

- `converged_all=True`, `hash_agreement=True`, `poll=3.0s`.
- Post-deploy convergence checkpoint (deploy `[5/6]`): desired `v27`, both boxes
  `in_sync` (`converged: desired=v27 [halo-a=ok halo-b=ok]`).
- Final captured `fleet-status.txt`: desired `v33`, both boxes `applied` at the
  same config hash `298d9463cdb3` (the demo phase exercised further edits after
  the deploy checkpoint; both boxes stayed converged throughout).
- Hot-reload latency (write→converge): p50 `0.010s`, p95 `0.019s`, mean `0.012s`
  (n=52).

From [`validation-evidence/run-20260717-142520/metrics.txt`](validation-evidence/run-20260717-142520/metrics.txt):

```text
== fleet metrics (run-20260717-142520, mode=gateway) ==
boxes=halo-a,halo-b desired=v33 audit=244 hash_agreement=True
convergence: 1 versions all-boxes (converged_all=True); cross-box span mean=1.0 max=1.0 s (poll=3.0s)
desired_config: 108818 bytes sha256=298d9463cdb3
hot_reload_latency (write->converge): p50=0.010s p95=0.019s mean=0.012s (n=52)
```

## Router image digests (R3 drift evidence)

Both boxes ran the same pinned ROCm router image
([`validation-evidence/run-20260717-142520/router-image-digests.txt`](validation-evidence/run-20260717-142520/router-image-digests.txt)):

- `halo-a` — `ghcr.io/vllm-project/semantic-router/vllm-sr-rocm@sha256:99c96cae498c006b0094ed851446a53b88f81294ec872e66b1a449469c7708b8`
- `halo-b` — `ghcr.io/vllm-project/semantic-router/vllm-sr-rocm@sha256:d84b27f04df25c24cd75b5c69432d5c6d4ad6af45d45bea52bb5cf4637bfe63b`

## Halo-B max-model capacity (2026-07-12)

Preserved capacity sweep committed at
[`../perf/maxmodel-sweep-halo-b.json`](../perf/maxmodel-sweep-halo-b.json):
max usable tag `qwen2.5:32b` (decode 10.9 tok/s median, TTFT ~242 ms, peak VRAM
26.7 GiB) on the Halo-B 64 GiB VRAM / 48 GiB GTT memory map. The companion
forced-residency ceiling run is
[`../perf/maxmodel-sweep-halo-b-96g-forced.json`](../perf/maxmodel-sweep-halo-b-96g-forced.json).

## Evidence integrity (SHA256)

Committed proof files (this directory):

| file | sha256 |
| --- | --- |
| `metrics.txt` | `090d37efcd413af0e6110e35efe17a8f388331e6a8398a72bbc6f516c0b83061` |
| `fleet-status.txt` | `e06790240afdafbb2c14d9a1143ad2058171a2ba409fb29e7ee38cd3a8a6100a` |
| `router-image-digests.txt` | `dc67b2816cf7484e1d54a6fd9682b7e9e76762549476ebe9f04fbe87cb6971a8` |

Archive-only files (not committed; preserved in the external bundle):

| file | sha256 |
| --- | --- |
| `verify-hardening-20260717-142737.log` | `2d2ec28a0a09013068360700dca67ad9e3dbe85821804f09c3742386665bca94` |
| `audit.log` | `0e2a1cbeaf8606aeddf53673f3837ecf05cf1364135558c02bbbb091f7b9592c` |
| `fleet-audit.txt` | `b5afbfa328be33962b848aa50d31c26561416de8095c92d06c1b5fa6882df835` |
| `metrics.json` | `0bb6295523d3462e24cb0715d7bbd3b077268e8db198ca532f5678e48518d6a7` |
| `halo-a-router-container.log` | `e473ec8a6c14bcbd1447ec4514340e18337ad11cffc498c80064bddc4a1fb642` |
| `halo-b-router-container.log` | `9ec62cc61f194de7e01d84c639f8da8090f086493c105ecb7336000f30e3be05` |
| `run-all.log` | `bc524df35376c62594df725d1f07844cc9b5e33f415f2424d034df844c6d519f` |
| `run-all-secure-20260717-142520.log` | `8fbbe5796541e48b84a991a08aa3e2e3edcab10b6301b83e0f8f7d43ea605290` |

The complete manifest (all 15 files) is committed at
[`validation-evidence/run-20260717-142520/SHA256SUMS`](validation-evidence/run-20260717-142520/SHA256SUMS).

## External archive

The full bundle (all logs + raw JSON) is preserved outside the repo at:

```text
/home/aup/vllm-sr-evidence/run-20260717-142520/
```

It contains 16 files (13 run-bundle files + the `verify-hardening` and
`run-all-secure` logs + `SHA256SUMS`). The transient `/tmp/vllm-sr-fleet`
working directory was removed after archiving.
