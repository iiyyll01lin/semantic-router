# TD052: Commit Large `.cache/ml-models` JSON via Git-LFS

## Status

Open - proposal only. This is an upstream-wide repository change, not something
to migrate on a PoC/feature branch (the blobs are byte-identical to `main`, so a
branch-local migration would only create a divergent history).

## Owner Plan

PL0032 Architecture Debt Consolidation

## Release Relevance

None - repository asset-management / clone-cost hygiene.

## Scope

The pre-trained classifier JSON assets committed as plain Git blobs under
`src/semantic-router/.cache/ml-models/`.

## Summary

`src/semantic-router/.cache/ml-models/` carries ~116 MB of JSON model assets as
ordinary Git objects, dominated by `svm_model.json` (~82 MB), plus
`knn_model.json` (~26 MB) and `mlp_model.json` (~6 MB). Because these are large,
low-compressibility blobs stored in normal history, every clone and fetch pays
their full cost, and any future rewrite of them would bloat the pack. Migrating
them to Git-LFS (or another large-file store) would keep working copies intact
while removing the blobs from the default clone path.

This must be done upstream on `main`, once, for the whole repository: the files
here are byte-identical to `main`, so migrating them on a feature branch would
diverge history without fixing the shared cost.

## Evidence

- `src/semantic-router/.cache/ml-models/svm_model.json` (~82 MB),
  `knn_model.json` (~26 MB), `mlp_model.json` (~6 MB), `kmeans_model.json`
  (~0.2 MB) - all tracked as plain Git blobs (`git ls-files` lists them).
- `du -sh src/semantic-router/.cache/ml-models` reports 116 MB.
- `git diff --stat main..HEAD -- src/semantic-router/.cache/ml-models/` is empty:
  the blobs are unchanged from `main`, confirming this is an upstream-wide concern.

## Why It Matters

- Every clone/fetch downloads ~116 MB of model JSON regardless of whether a
  contributor touches the classifier assets.
- Large plain blobs in history are expensive to repack and impossible to prune
  without a coordinated, upstream history rewrite.

## Desired End State

- The large `.cache/ml-models` JSON assets are tracked via Git-LFS (or an
  equivalent large-file mechanism) on `main`, with `.gitattributes` filters, so
  working copies are unchanged but the default clone no longer carries the blobs.

## Exit Criteria

- An upstream decision on Git-LFS (or an alternative) for these assets is
  recorded, and if accepted, `.gitattributes` tracks
  `src/semantic-router/.cache/ml-models/*.json` via LFS on `main` with the build
  and tests still resolving the models from the same paths.
