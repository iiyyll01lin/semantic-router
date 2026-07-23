# TD046: onnx-binding Concurrent ROCm ONNX Session Creation Segfaults on Classifier (Re)Initialization

## Status

Open

## Owner Plan

PL0035 Strix PoC PII Hardening

## Release Relevance

None - PoC-surfaced binding/runtime concurrency debt

## Scope

`onnx-binding` ROCm sequence/token classifier session creation
(`init_sequence_classifier` / `init_token_classifier`) when the Go classifier
runtime initializes multiple GPU classifiers in parallel (startup and every
`PUT /config/router` fsnotify reload).

## Summary

When the AMD/ROCm router initializes its built-in classifiers with the GPU
provider (`use_cpu: false`, i.e. `--platform amd` without
`VLLM_SR_AMD_PRESERVE_CPU=1`), the Go classifier runtime runs its init tasks in
parallel (`modelruntime.Execute` with `MaxParallelism = NumCPU`). Several of
those tasks (jailbreak, factcheck, feedback, and the PII token classifier) each
call into the cgo binding to create a ROCm ONNX Runtime session at the same
time. Concurrent ROCm/ONNX Runtime session creation is not thread-safe and the
process takes `SIGSEGV: segmentation violation` inside `init_sequence_classifier`
/ `init_token_classifier`, which kills the whole router process (including the
apiserver on `:8080`).

This is especially visible from the routing calibration loop: its
`PUT /config/router` triggers an in-container runtime config sync plus an
fsnotify hot-reload, and the reload re-runs `Classifier.InitializeRuntime`,
concurrently re-creating the GPU classifier sessions and crashing the router.

The PoC currently avoids this entirely by keeping the classifiers on CPU
(`VLLM_SR_AMD_PRESERVE_CPU=1`, now propagated into the container - see
[runtime_support.py](../../../src/vllm-sr/cli/commands/runtime_support.py)). That
sidesteps the crash for the PoC but does not fix the underlying binding
thread-safety gap on the GPU path.

## Evidence

- Router container crash trace (`docker logs vllm-sr-router-container`):
  `SIGSEGV: segmentation violation` / `signal arrived during cgo execution`,
  with concurrent goroutines in
  `candle-binding._Cfunc_init_sequence_classifier` and
  `candle-binding._Cfunc_init_token_classifier`, reached via
  `extproc.(*Server).reloadRouterFromFile` ->
  `reloadRouterFromConfig` -> `createRouterClassifier` ->
  `Classifier.InitializeRuntime`. The cgo init args show `useGPU=1`.
- [src/semantic-router/pkg/classification/classifier_lifecycle.go](../../../src/semantic-router/pkg/classification/classifier_lifecycle.go) - `InitializeRuntime` runs the per-classifier tasks with `MaxParallelism: modelruntime.DefaultParallelism(len(tasks))`, allowing concurrent session creation.
- [src/semantic-router/pkg/modelruntime/executor.go](../../../src/semantic-router/pkg/modelruntime/executor.go) - `DefaultParallelism` returns `min(NumCPU, taskCount)`, so multiple init tasks run on separate goroutines.
- [onnx-binding/src/ffi/classification.rs](../../../onnx-binding/src/ffi/classification.rs) - `init_sequence_classifier` / `init_token_classifier` call `MmBertSequenceClassifier::load` / `MmBertTokenClassifier::load` with `ClassifierExecutionProvider::Auto` (ROCm) and only lock the global `HashMap` after the session is built; session creation itself is unsynchronized.
- [src/semantic-router/pkg/extproc/server_config_watch.go](../../../src/semantic-router/pkg/extproc/server_config_watch.go) - every config write triggers `reload()` -> `reloadRouterFromFile`, with no content-diff guard, so each `PUT /config/router` re-initializes classifiers.

## Why It Matters

- Any `--platform amd` deployment that keeps classifiers on the GPU (the default
  when `VLLM_SR_AMD_PRESERVE_CPU` is unset) will crash the entire router process
  on startup or on the first config reload, taking down routing and the
  apiserver `:8080` with no graceful error.
- The crash is timing-dependent, so it can intermittently survive startup and
  then crash later on a reload, which is hard to diagnose from the symptom alone
  (`Remote end closed connection without response`).
- The current mitigation (force CPU) leaves GPU classifier acceleration on AMD
  effectively unusable, contradicting the intent of `--platform amd` flipping
  classifiers to GPU.

## Desired End State

Classifier session creation is safe to run concurrently (or is serialized) so
that initializing multiple ROCm ONNX classifiers - at startup and on every
config reload - never segfaults. `--platform amd` with GPU classifiers
(`VLLM_SR_AMD_PRESERVE_CPU` unset) starts and hot-reloads without crashing.

## Exit Criteria

- ROCm ONNX session creation is serialized or otherwise made thread-safe (for
  example a dedicated creation mutex in the onnx-binding FFI, or forcing
  `MaxParallelism = 1` for the classifier runtime-init tasks), so concurrent
  `init_sequence_classifier` / `init_token_classifier` calls cannot segfault.
- A `--platform amd` router with GPU classifiers survives startup and a
  `PUT /config/router` hot-reload (e.g. the routing calibration loop) without
  `SIGSEGV` / `exit 2`.
- The Strix Halo PoC no longer depends solely on `VLLM_SR_AMD_PRESERVE_CPU=1` to
  avoid the crash (the flag remains a valid GPU-reservation choice, not a
  crash workaround).
