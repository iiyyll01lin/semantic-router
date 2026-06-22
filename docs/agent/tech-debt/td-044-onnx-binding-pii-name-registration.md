# TD044: onnx-binding PII Token Classifier Registers as `bert_token` but Inference Looks Up `pii`

## Status

Open

## Owner Plan

PL0035 Strix PoC PII Hardening

## Release Relevance

None - PoC-surfaced binding debt

## Scope

`onnx-binding` PII token-classifier name registration vs request-time lookup (the `use_mmbert_32k: false` PII path in the ONNX/ROCm binding)

## Summary

In the ONNX/ROCm token-classifier binding, the PII path is only name-consistent
when `use_mmbert_32k: true`. The auto-detect path taken when
`use_mmbert_32k: false` registers the token classifier under the name
`bert_token`, but request-time inference always looks the classifier up under
the name `pii`. Initialization succeeds, so the misconfiguration is silent until
the first request, which then fails with `PII classifier 'pii' not found`.

Concretely, the Go-side `PIIInitializerImpl.Init` first calls
`candle_binding.InitCandleBertTokenClassifier`, which in the ONNX binding
registers the model under `"bert_token"`. The matching auto-detect inference
path (`ClassifyCandleBertTokens` -> `ClassifyMmBert32KPII`) calls
`detect_pii("pii")`, which never finds a `bert_token`-keyed entry. Only the
mmBERT-32K path is consistent: `InitMmBert32KPIIClassifier` registers `"pii"`
and `ClassifyMmBert32KPII` looks up `"pii"`. Because the loader is
model-agnostic and loads whatever ONNX lives under the model dir, the PoC works
around the bug by setting `use_mmbert_32k: true` on the PII classifier while
still pointing `model_id` at the ModernBERT presidio detector.

## Evidence

- [onnx-binding/semantic-router.go](../../../onnx-binding/semantic-router.go) - `InitCandleBertTokenClassifier` calls `initTokenClassifier("bert_token", ...)` (registers `bert_token`); `InitMmBert32KPIIClassifier` and `InitModernBertPIITokenClassifier` call `initTokenClassifier("pii", ...)`; `ClassifyMmBert32KPII` calls `detect_pii` with `C.CString("pii")`; `ClassifyCandleBertTokens` delegates to `ClassifyMmBert32KPII`.
- [onnx-binding/src/ffi/classification.rs](../../../onnx-binding/src/ffi/classification.rs) - `init_token_classifier` inserts the model into `TOKEN_CLASSIFIERS` keyed by the supplied `name`; `detect_pii` looks the classifier up by `classifier_name` and returns `PII classifier '{}' not found` when the key is missing.
- [src/semantic-router/pkg/classification/classifier_pii_init.go](../../../src/semantic-router/pkg/classification/classifier_pii_init.go) - `PIIInitializerImpl.Init` tries `InitCandleBertTokenClassifier` (registers `bert_token`) first; its `PIIInferenceImpl.ClassifyTokens` calls `ClassifyCandleBertTokens` (looks up `pii`). The `MmBERT32KPIIInitializerImpl`/`MmBERT32KPIIInferenceImpl` pair is name-consistent on `pii`.
- [deploy/recipes/strix-halo-poc/poc-strix.yaml](../../../deploy/recipes/strix-halo-poc/poc-strix.yaml) - the `modules.classifier.pii` block sets `use_mmbert_32k: true` with `model_id: models/pii_classifier_modernbert-base_presidio_token_model` to take the name-consistent path.
- [deploy/recipes/strix-halo-poc/cpu-smoke.yaml](../../../deploy/recipes/strix-halo-poc/cpu-smoke.yaml) - the `classifier.pii` block carries the explanatory comment documenting that `use_mmbert_32k: false` "registers `bert_token` but inference still looks up `pii`", so `use_mmbert_32k` is kept `true` on purpose.
- [deploy/recipes/strix-halo-poc/REHEARSAL.md](../../../deploy/recipes/strix-halo-poc/REHEARSAL.md) - Gate B note recording the `PII classifier 'pii' not found` failure observed at request time on the `use_mmbert_32k: false` path during PoC bring-up.

## Why It Matters

- A valid-looking PII config (`use_mmbert_32k: false` with a real token model) initializes successfully but fails every request with `PII classifier 'pii' not found`, so the PII security lane silently fails open until the first request hits it.
- The only working configuration on the ONNX/ROCm binding forces `use_mmbert_32k: true` even for a ModernBERT presidio model, which is a misleading flag value chosen for name-consistency rather than for selecting an mmBERT-32K model.
- Operators and agents reading the recipe cannot tell that `use_mmbert_32k` here is a binding workaround, not a real model-architecture switch, without the inline comment.

## Desired End State

The ONNX/ROCm PII token-classifier path is name-consistent regardless of the
`use_mmbert_32k` flag: the init function that the auto-detect PII path calls
registers the classifier under the same name (`pii`) that request-time inference
looks up. PII detection works with `use_mmbert_32k: false`, and the PoC recipes
no longer need `use_mmbert_32k: true` purely to dodge the registration mismatch.

## Exit Criteria

- The auto-detect PII init path (`InitCandleBertTokenClassifier`) registers the
  token classifier under `pii` (or inference is changed to look up the same name
  init used), so init and inference agree.
- A PII config with `use_mmbert_32k: false` and a valid token model serves
  requests without `PII classifier 'pii' not found`.
- The Strix Halo PoC recipes no longer set `use_mmbert_32k: true` solely to work
  around the name mismatch, or the flag is formally documented as the supported
  PII contract for this binding.
