# TD045: mmBERT-32K PII Detector (`mmbert32k-pii-detector-merged`) Returns Zero Entities

## Status

Open

## Owner Plan

PL0035 Strix PoC PII Hardening

## Release Relevance

None - PoC-surfaced model debt

## Scope

`models/mmbert32k-pii-detector-merged` token-classification inference output on the Strix Halo ROCm PoC PII lane

## Summary

The `models/mmbert32k-pii-detector-merged` token classifier emits all-`O`
(outside / no-entity) token predictions on every input observed during the
Strix Halo PoC, including trivial PII strings. Lowering the detection confidence
threshold all the way down to `0.01` still yields zero detected entities, so the
model is effectively unusable as a PII detector in this PoC: the PII security
lane would never trigger on it. To get a working PII lane the PoC swapped the
detector to the repo's proven ModernBERT presidio token detector
(`models/pii_classifier_modernbert-base_presidio_token_model`) while keeping the
mmBERT-32K-consistent binding path (see TD044).

## Evidence

- This session's Strix Halo PoC bring-up findings: `mmbert32k-pii-detector-merged` produced all-`O` token predictions on trivial PII inputs, and dropping the threshold to `0.01` still returned zero entities.
- [deploy/recipes/strix-halo-poc/cpu-smoke.yaml](../../../deploy/recipes/strix-halo-poc/cpu-smoke.yaml) - the `classifier.pii` block comment records the PII swap: "the mmBERT-32K PII *model* returns zero entities at any confidence on this box (broken inference), so the security lane points at the repo's proven ModernBERT presidio token detector instead."
- [deploy/recipes/strix-halo-poc/poc-strix.yaml](../../../deploy/recipes/strix-halo-poc/poc-strix.yaml) - the `modules.classifier.pii` block points `model_id` and `pii_mapping_path` at `models/pii_classifier_modernbert-base_presidio_token_model`, not at `mmbert32k-pii-detector-merged`.
- The `models/mmbert32k-pii-detector-merged` model directory (local PoC artifact, not committed) is the subject of the failing inference; its `pii_type_mapping.json` / label set was not exercised because the detector never emitted a non-`O` label.

## Why It Matters

- A PII detector that returns zero entities at any confidence is silently
  non-functional: the PII security lane fails open and would pass PII through
  without ever flagging it.
- The PoC's working PII behavior depends on swapping to a different model
  (ModernBERT presidio), so the mmBERT-32K detector cannot be relied on for the
  PII lane until its inference is fixed or its defect is understood.
- Without a tracked entry, a future bring-up could re-point the PII lane at
  `mmbert32k-pii-detector-merged` and silently lose PII protection again.

## Desired End State

The PoC PII lane is backed by a detector that returns correct, non-empty PII
entities on trivial PII inputs. Either `mmbert32k-pii-detector-merged` is fixed
(re-export / retrain / corrected inference path) so it emits real entities, or
the PoC formally and durably adopts the ModernBERT presidio detector for the PII
lane with the rationale captured here rather than only in recipe comments.

## Exit Criteria

- `mmbert32k-pii-detector-merged` returns non-`O` entities on trivial PII inputs
  at a sane confidence threshold, OR the PoC formally adopts the ModernBERT
  presidio detector as the supported PII detector and the mmBERT-32K detector is
  removed from PII-lane consideration.
- The root cause of the all-`O` output (model export defect vs binding/threshold
  defect) is identified and recorded.
- The PoC recipes' PII detector choice is justified by a tracked decision rather
  than only an inline recipe comment.
