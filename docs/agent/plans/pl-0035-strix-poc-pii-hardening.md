# Strix PoC PII Hardening

## Goal

- Own the durable PII-path technical debt surfaced while bringing up the Strix
  Halo ROCm PoC.
- Track each debt item to a real fix or an explicit decision, with current
  source evidence, instead of leaving the gaps only in PoC recipe comments.
- Keep this scope small and PoC-focused; it is not a general PII or binding
  redesign plan.

## Scope

- PoC PII-path technical debt:
  - [TD044](../tech-debt/td-044-onnx-binding-pii-name-registration.md)
  - [TD045](../tech-debt/td-045-mmbert32k-pii-detector-zero-entities.md)
- The ONNX/ROCm token-classifier name registration vs lookup mismatch in the
  `onnx-binding` PII path.
- The `models/mmbert32k-pii-detector-merged` token detector returning zero
  entities, and the PoC's swap to the ModernBERT presidio detector.

Out of scope:

- Non-release architecture debt consolidation; that belongs to
  [PL0032](pl-0032-architecture-scorecard-ratchet.md).
- v0.3 Themis release closure; that belongs to
  [PL0033](pl-0033-v0-3-themis-release-closure.md).
- Rewriting the candle/onnx binding token-classifier API beyond what is needed
  to make the `pii` name consistent.
- Retraining or re-exporting the mmBERT-32K PII model as part of the PoC.

## Exit Criteria

- Both TD entries are either retired with current source evidence or explicitly
  re-scoped into a release-owned item if they become release-critical.
- The ONNX/ROCm PII path no longer requires `use_mmbert_32k: true` as a
  name-consistency workaround for the `bert_token` vs `pii` registration
  mismatch, or the workaround is documented as the intended contract.
- The PoC PII detector choice is backed by a detector that returns non-empty
  entities on trivial PII inputs, with the rationale captured in the TD rather
  than only in recipe comments.

## Task List

- [ ] `SPP001` Confirm TD044 root cause against current `onnx-binding` source and
  decide between fixing the registration name (`InitCandleBertTokenClassifier`
  registering `pii`) or documenting `use_mmbert_32k: true` as the supported PII
  contract.
- [ ] `SPP002` Confirm TD045 by re-running the mmBERT-32K PII detector on trivial
  PII inputs at low confidence and record whether the zero-entity behavior is a
  model export defect or a binding/threshold defect.
- [ ] `SPP003` Retire or re-scope TD044 once the registration mismatch is fixed
  or documented as intended.
- [ ] `SPP004` Retire or re-scope TD045 once the PoC has a durable, non-empty PII
  detector decision (fix the model, replace it, or formally adopt the ModernBERT
  presidio detector).

## Next Action

- Pick one TD (TD044 or TD045), reproduce it against current source on a box
  that can run the ROCm router, and either land the fix or capture the decision
  in the TD's Exit Criteria.

## Operating Rules

- Keep this plan PoC-scoped: only the two PII-path debts above and their direct
  fixes belong here.
- Do not fold general binding or classification refactors into this plan; open a
  separate owner if the work grows beyond the PoC PII path.
- Use current-source evidence (binding code, recipe config, model behavior) to
  decide whether each TD is still open.

## Related Docs

- [Tech Debt README](../tech-debt/README.md)
- [TD044 onnx-binding PII name-registration mismatch](../tech-debt/td-044-onnx-binding-pii-name-registration.md)
- [TD045 mmBERT-32K PII detector returns zero entities](../tech-debt/td-045-mmbert32k-pii-detector-zero-entities.md)
- [Strix Halo PoC rehearsal checklist](../../../deploy/recipes/strix-halo-poc/REHEARSAL.md)
