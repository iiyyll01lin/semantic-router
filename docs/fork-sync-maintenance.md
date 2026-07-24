# Fork Sync & Conflict-Resolution Playbook

How to keep this fork's `main` current with upstream while preserving the
Strix Halo 2-box fleet PoC integration and the fork-original `failover` looper.

> This file is **fork-specific** (it does not exist upstream), so it never
> conflicts during an upstream sync.

## 1. What this fork's `main` is

`main` = latest `upstream/main` **+** the Strix Halo 2-box fleet PoC (recipe,
agentic-context evidence reports, report/config gates) **+** the fork-original
`failover` looper. It **intentionally diverges** from upstream, so upstream
syncs are **merges** (not fast-forwards). Most are clean; conflicts only appear
on the small "algorithm registration" surface listed in section 4.

Remotes:

- `origin` = fork (`iiyyll01lin/semantic-router`)
- `upstream` = `vllm-project/semantic-router`

## 2. One-time setup (per machine you sync from)

```bash
git config rerere.enabled true   # record + auto-replay conflict resolutions
```

`rerere` makes the recurring `failover` conflict a solved problem: resolve it
once, and git replays the same resolution on every future sync.

## 3. Routine sync (do this often — small, frequent merges = tiny conflicts)

```bash
git fetch upstream
git fetch origin

# Preview whether the merge is clean before touching anything:
git merge-tree --write-tree origin/main upstream/main | grep -c CONFLICT   # 0 = clean

# Merge on a scratch worktree so your normal checkout is undisturbed:
git worktree add -b sync-main /tmp/sync origin/main
cd /tmp/sync
git merge --no-ff upstream/main -m "Merge upstream/main into main (sync)" \
                                -m "Signed-off-by: <Your Name> <you@example.com>"
# ... resolve conflicts per section 4 if any, then verify (section 5) ...
git push origin HEAD:main
cd - && git worktree remove --force /tmp/sync && git branch -D sync-main
```

If it was clean, you are done after the push. If git reports conflicts, go to
section 4.

## 4. Conflict recipe — the `failover` surface

Conflicts happen **only** when upstream re-touches the algorithm-registration
code. The rule is always the same:

> **Take upstream's version of the conflict, then re-add the `failover` lines.
> Drop `session_aware` / `elo` (upstream deprecated them; they are inherited,
> not fork-original).**

The `failover` support code (`pkg/looper/failover.go`,
`config/algorithm/looper/failover.yaml`,
`website/docs/tutorials/algorithm/looper/failover.md`, and the `*_failover_test.go`
files) lives in separate files and merges automatically — you only re-wire the
registration points below.

| File | Re-add |
|---|---|
| `src/semantic-router/pkg/looper/looper.go` | `case "failover":` returning `NewFailoverLooper(cfg)` |
| `src/semantic-router/pkg/config/decision_config.go` | struct field `Failover *FailoverAlgorithmConfig` with yaml tag `failover,omitempty` |
| `src/semantic-router/pkg/dsl/compiler_algorithms.go` | map entry `"failover"` calling `algo.Failover = c.compileFailoverAlgo(fields)` |
| `src/semantic-router/pkg/dsl/decompiler_algorithms.go` | map entry `"failover"` → `failoverAlgorithmToFields(algo.Failover, fields)` |
| `src/semantic-router/pkg/config/routing_surface_catalog.go` | `{Type: "failover", Tier: "supported"},` |
| `src/semantic-router/pkg/config/validator_decision.go` | `expectedBlockByType["failover"] = "failover"` **and** a `case "failover":` calling `validateFailoverAlgorithmConfig(algorithm.Failover)` |
| `src/semantic-router/pkg/extproc/req_filter_looper.go` | add `"failover"` to the looper-type `case` list |
| `src/semantic-router/pkg/extproc/req_filter_looper_test.go` | add `"failover"` to the looper-algorithms test slice |
| `src/semantic-router/pkg/config/docs_contract_algorithm_plugin_test.go` | bucket `"failover": "looper",` |
| `src/semantic-router/pkg/config/fragment_catalog_test.go` | `"failover": filepath.Join("looper", "failover.yaml"),` |
| `config/README.md` | add `failover` to the `looper/` policy list |

Non-code conflicts are simple unions/keeps:

- `.gitignore`, `src/vllm-sr/cli/commands/runtime_support.py`,
  `tools/make/docker.mk` → keep both sides' additions.
- `tools/agent/repo-manifest.yaml`, `docs/agent/plans/README.md` → union
  (upstream entries + fork entries).
- `.github/workflows/*`, `SECURITY.md`, `website/docs/tutorials/algorithm/overview.md`
  → take upstream.

**Note on doc indices:** the PoC's plans/tech-debt were renumbered to avoid
colliding with upstream's: `pl-0035→pl-0039`, `pl-0036→pl-0040`,
`td-044→td-053` (upstream owns `pl-0035..0038` and `td-044-flow-tool-state-durability-gap`).
Keep those fork numbers when resolving `repo-manifest.yaml` / the READMEs.

After resolving, run `gofmt -w` on the touched Go files, then verify.

## 5. Verify before pushing `main`

```bash
cd src/semantic-router
go build ./pkg/config/... ./pkg/dsl/... ./pkg/looper/... ./pkg/extproc/... ./pkg/apiserver/...
go vet   ./pkg/config/... ./pkg/dsl/...
go test  ./pkg/config/... ./pkg/dsl/...      # failover validation + DSL roundtrip
cd ../..
make agent-validate                          # manifest/doc-governance consistency
make validate-agentic-context-reports        # PoC report consistency (21 tests)
make agent-lint                              # needs go + markdownlint on PATH
```

(The `looper`/`extproc`/`apiserver` **test binaries** need the Rust bindings
built — `cargo build --release` in `candle-binding`/`ml-binding` — to link. If
Rust is unavailable, `go build` + `go vet` on those packages is the fallback;
the `failover` change there is a one-line dispatch addition covered indirectly
by the `config`/`dsl` tests.)

## 6. The permanent fix — shrink the divergence

The cleanest way to make syncs conflict-free long term is to **upstream the
`failover` looper** as a PR (see the `feat/failover-looper` branch). Once
upstream accepts it, `failover` is part of upstream and this entire conflict
surface disappears. Keep only the genuinely fork-specific pieces (Strix hardware
recipe, evidence reports, gates) on `main`.
