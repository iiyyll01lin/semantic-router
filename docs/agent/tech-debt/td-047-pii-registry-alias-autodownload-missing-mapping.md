# TD047: PII Registry Alias Auto-Download Target Is Missing `pii_type_mapping.json`

## Status

Open

## Owner Plan

PL0035 Strix PoC PII Hardening

## Release Relevance

None - PoC-surfaced registry/model-asset debt

## Scope

The model registry alias resolution for the presidio PII token classifier
(`pii_classifier_modernbert-base_presidio_token_model`) and the auto-download
path taken when `<config_dir>/models` does not already contain the model.

## Summary

The config path `models/pii_classifier_modernbert-base_presidio_token_model`
is a registry alias that resolves auto-download to the Hugging Face repo
`llm-semantic-router/mmbert-pii-detector-merged`. That repo does not ship
`pii_type_mapping.json`. As a result, any fresh or empty `models/` directory
that relies on auto-download for this PII path fatals at router startup with:

```text
failed to read PII mapping file: models/pii_classifier_modernbert-base_presidio_token_model/pii_type_mapping.json: no such file or directory
```

The presidio detector that does ship the mapping (plus an exported
`onnx/model.onnx`) is the one pre-staged under
`deploy/recipes/strix-halo-poc/models/`, but auto-download never produces an
equivalent dir, so any recipe or deployment depending on auto-download for this
PII path is broken until the model is pre-staged out of band.

## Evidence

- [src/semantic-router/pkg/config/registry.go](../../../src/semantic-router/pkg/config/registry.go) - lines ~98-105: the registry entry with `RepoID: "llm-semantic-router/mmbert-pii-detector-merged"` lists `pii_classifier_modernbert-base_presidio_token_model` among its `Aliases`, so that config path resolves auto-download to a repo that does not include `pii_type_mapping.json`.
- [src/vllm-sr/cli/docker_start.py](../../../src/vllm-sr/cli/docker_start.py) - lines ~458-504: `models_dir = os.path.join(config_dir, "models")` is created if missing and mounted as `<config_dir>/models` -> `/app/models`, so an empty (or freshly created) models dir triggers the broken auto-download at startup.
- [deploy/recipes/strix-halo-poc/poc-strix.yaml](../../../deploy/recipes/strix-halo-poc/poc-strix.yaml) and [deploy/recipes/strix-halo-2box/poc-client-edge.yaml](../../../deploy/recipes/strix-halo-2box/poc-client-edge.yaml) - the PII classifier sets its mapping path under `models/pii_classifier_modernbert-base_presidio_token_model`, which is the alias above.
- [deploy/recipes/strix-halo-2box/client-bring-up.sh](../../../deploy/recipes/strix-halo-2box/client-bring-up.sh) - the 2-box recipe works around this by pre-staging/symlinking `.vllm-sr-rendered/models` to the shared single-box `../strix-halo-poc/models` tree (which contains the presidio model with `pii_type_mapping.json` + `onnx/model.onnx`) and hard-failing before serve if either file is missing.

## Why It Matters

- Any deployment that does not pre-stage this exact presidio model directory
  cannot start the router on the default PII path; the failure is a hard fatal
  at startup, not a degraded-but-running state.
- The fix at the recipe level (pre-staging + symlinking) hides a base-level
  registry/model-asset gap: the alias advertises an auto-download path that
  cannot produce a working model dir, which is surprising to operators and
  agents who expect auto-download to "just work" like other registry models.

## Desired End State

Auto-download for the
`pii_classifier_modernbert-base_presidio_token_model` path yields a model
directory that contains `pii_type_mapping.json` (and the artifacts the router
needs to load the token classifier), so the router starts without out-of-band
pre-staging. This is achieved either by publishing `pii_type_mapping.json` (and
any other required assets) into `llm-semantic-router/mmbert-pii-detector-merged`,
or by re-pointing the alias/registry entry to a repo that ships the mapping.

## Exit Criteria

- The repo that `pii_classifier_modernbert-base_presidio_token_model` resolves
  to includes `pii_type_mapping.json`, or the registry alias is changed to a
  repo that does.
- A fresh, empty `<config_dir>/models` that relies on auto-download for this PII
  path starts the router without the `failed to read PII mapping file` fatal.
- The Strix Halo recipes no longer need to pre-stage/symlink the presidio model
  solely to supply `pii_type_mapping.json` for this PII path (the 2-box
  workaround in `client-bring-up.sh` can be removed or simplified).
