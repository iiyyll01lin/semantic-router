# Strix Halo 單機 PoC 套件 / Strix Halo Single-box PoC Bundle

> 本資料夾是 [docs/poc/03-strix-halo-runbook.md](../../../docs/poc/03-strix-halo-runbook.md) 的可執行版本：一份完整的 v0.3 路由設定（含安全 lane）、啟動腳本、冒煙測試、離線設定驗證器與校準 probe 套件。
> This folder is the runnable counterpart to [docs/poc/03-strix-halo-runbook.md](../../../docs/poc/03-strix-halo-runbook.md): a complete v0.3 routing config (with the security lane), a bring-up script, a smoke test, an offline config validator, and a calibration probe suite.

採用操作手冊選定的**方法 C**：同一台 Ubuntu Strix Halo（Ryzen AI Max+ 395，gfx1151）上以單一 Ollama（ROCm）server 提供每個 tier 一個真正不同的本地模型，完全離線。

It uses the runbook's selected **approach C**: a single Ollama (ROCm) server on one Ubuntu Strix Halo box (Ryzen AI Max+ 395, gfx1151) serving a genuinely different local model per tier, fully offline.

---

## 檔案說明 / What Each File Is

| 檔案 / File | 說明 / Description |
| --- | --- |
| [`poc-strix.yaml`](poc-strix.yaml) | 完整、可獨立載入的 v0.3 路由設定。由 [balance.yaml](../balance.yaml) 改寫：5 個模型重新指向本地 Ollama（保留 `name` 與 `pricing`），保留全部 13+1 條決策、signals、projections、modelCards，並新增安全 lane（jailbreak/pii signals、`security_guard` 決策、`prompt_guard` 與 pii 分類器模組）。/ A complete, self-contained v0.3 routing config. Adapted from [balance.yaml](../balance.yaml): the 5 models are repointed at local Ollama (keeping each `name` and `pricing`), all balance decisions, signals, projections, and modelCards are kept intact, and the security lane is added (jailbreak/pii signals, a `security_guard` decision, and the `prompt_guard` + pii classifier modules). |
| [`bring-up.sh`](bring-up.sh) | 在 Strix Halo 上端到端啟動：先以唯讀模式檢查既有 runtime，安全啟動固定 image digest／64K context 的 Ollama、下載目前自動路由使用的 5 個模型（含 Gemma Q8 預設與 Q4 fast lane），再以 `--platform amd` 啟動 router。也提供 `--runtime-preflight`、`--runtime-only`、`--runtime-proof`。/ End-to-end Strix Halo bring-up: read-only inspect the existing runtime, safely start digest-pinned Ollama with an explicit 64K context, pull the 5 models currently used by auto-routing (including the Gemma Q8 default and Q4 fast lane), then serve the router with `--platform amd`. It also exposes `--runtime-preflight`, `--runtime-only`, and `--runtime-proof`. |
| [`ollama-runtime.sh`](ollama-runtime.sh) | Ollama runtime 的 fail-closed 管理器：不會自動重啟／移除／重建不相符或使用中的 container；固定 image、context、parallel slots，僅下載缺少的模型。/ Fail-closed Ollama runtime manager: it never automatically restarts, removes, or recreates a mismatched/in-use container; it pins image, context, and parallel slots and pulls only missing models. |
| [`runtime_context_proof.py`](runtime_context_proof.py) | 唯讀蒐集 runtime/model/context provenance；選配一個 1-token load probe 後，驗證 `ollama ps` 的實際 `CONTEXT` 與 CPU/GPU offload。輸出僅含 allowlist 環境欄位，不收集 container secrets。/ Read-only runtime/model/context provenance collector; after an optional one-token load probe, verifies the actual `ollama ps` `CONTEXT` and CPU/GPU offload. Output includes only allowlisted environment fields, never the container's secrets. |
| [`smoke_test.py`](smoke_test.py) | 純標準函式庫（urllib）冒煙測試：對 `:8899` 送出 4 個示範請求（簡單事實／困難推理／含 PII／jailbreak），印出狀態與 `x-vsr-*` 標頭，並標示輸入端 fast_response 攔截（`x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`）與輸出端 `response_jailbreak` 的 HTTP 403。/ Stdlib-only (urllib) smoke test: POSTs the 4 demo requests (easy factual / hard reasoning / PII / jailbreak) to `:8899`, prints status and `x-vsr-*` headers, and flags the input-side fast_response block (`x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`) and the output-side `response_jailbreak` HTTP 403. |
| [`validate_poc_config.py`](validate_poc_config.py) | 離線 PyYAML 設定驗證器，本機即可跑（即本資料夾的 on-box 測試）。檢查模型解析、`default_model`、決策 `modelRefs`、決策 `rules` 的 signal 參照與 `provider_model_id`。/ An offline PyYAML config validator that runs on this box (the on-box test). Checks model resolution, `default_model`, decision `modelRefs`, decision-rule signal references, and `provider_model_id`. |
| [`gen-dsl.sh`](gen-dsl.sh) | 由 `poc-strix.yaml` 產生並驗證 `poc-strix.dsl`（從 `src/semantic-router` 執行 `go run ./cmd/dsl decompile` 再 `validate`，需要 Go）。`.dsl` 是產生物，不入庫。/ Generates and validates `poc-strix.dsl` from `poc-strix.yaml` (runs `go run ./cmd/dsl decompile` then `validate` from `src/semantic-router`, requires Go). The `.dsl` is a generated artifact and is not committed. |
| [`poc-probes.yaml`](poc-probes.yaml) | 校準 probe 套件：balance 的 13 條決策 probe 加上期望路由到 `security_guard` 的安全 probe。/ The calibration probe suite: the balance 13-decision probes plus security probes expecting the `security_guard` decision. |
| [`run-bench.sh`](run-bench.sh) | 對運作中的 `vllm-sr serve` 堆疊一鍵跑 live bench 證據（GA 診斷 probe、agentic session 路由、cached-token、選配品質維持），預設正確 port（listener `:8899`、metrics `:9190`，baseline 本地 Ollama `:11434`），避開 bench 預設的手動 dev port。/ One-command live-bench evidence against the running `vllm-sr serve` stack (GA diagnostic probe, agentic session routing, cached-token, optional quality retention), pre-wired to the correct ports (listener `:8899`, metrics `:9190`, baseline local Ollama `:11434`), avoiding the bench manual-dev defaults. |
| [`cpu-smoke.yaml`](cpu-smoke.yaml) / [`cpu-smoke.sh`](cpu-smoke.sh) | 免 GPU 的 CPU 端到端煙霧變體：安全 lane 與 `poc-strix.yaml` 完全相同，僅 providers/backends 改指向 `llm-katan` echo 後端（見下方 CPU 煙霧測試）。/ The GPU-free CPU end-to-end smoke variant: the security lane is identical to `poc-strix.yaml`, only providers/backends point at the `llm-katan` echo backend (see CPU smoke below). |
| [`REHEARSAL.md`](REHEARSAL.md) | Strix Halo demo 前的彩排與 Go/No-Go 檢查表（ROCm、安全模型、DSL 驗證、bring-up、煙霧證據、dashboard）。/ The pre-demo rehearsal and Go/No-Go checklist for the Strix Halo (ROCm, security models, DSL validation, bring-up, smoke evidence, dashboard). |

---

## 前置需求 / Prerequisites

完整前置需求（Ubuntu x86_64、針對 gfx1151 的 ROCm、Docker GPU passthrough、`video`/`render` 群組、`vllm-sr` CLI）請見操作手冊第 1 與第 4 節：[docs/poc/03-strix-halo-runbook.md](../../../docs/poc/03-strix-halo-runbook.md)。

For the full prerequisites (Ubuntu x86_64, ROCm for gfx1151, Docker GPU passthrough, the `video`/`render` groups, and the `vllm-sr` CLI), see sections 1 and 4 of the runbook: [docs/poc/03-strix-halo-runbook.md](../../../docs/poc/03-strix-halo-runbook.md).

---

## 快速開始 / Quickstart

在 Strix Halo 上完成 `git pull` 後，於本資料夾執行：

After `git pull` on the Strix Halo, run from this folder:

```bash
# 0. 唯讀檢查既有 runtime；不建立／啟動／重啟任何服務
#    read-only inspect existing runtime; creates/starts/restarts nothing
bash bring-up.sh --runtime-preflight

# 1. 啟動 Ollama + 下載模型 + 啟動 router / start Ollama, pull models, serve the router
bash bring-up.sh

# 2. 以 1-token load probe 證明實際 allocation，並保存 provenance
#    prove the actual allocation with a one-token load probe and save provenance
bash bring-up.sh --runtime-proof

# 3. 送出 4 個示範請求並觀察路由與安全攔截 / send the 4 demo requests and observe routing + security blocks
python smoke_test.py            # 預設 / default --base-url http://localhost:8899

# 4. 離線驗證設定的內部一致性 / offline-validate the config's internal consistency
python validate_poc_config.py poc-strix.yaml
```

> 本機（開發箱）僅有 `python`（非 `python3`）。在 Strix Halo（Ubuntu）上兩者通常皆可。
> This dev box only has `python` (not `python3`). On the Strix Halo (Ubuntu) either usually works.

---

## Ollama serving context 契約 / Ollama Serving Context Contract

- 預設 serving context 明確固定為 **65,536 tokens (64K)**；`OLLAMA_NUM_PARALLEL=1`、`OLLAMA_MAX_LOADED_MODELS=1`，避免 parallel slots 或多模型同駐時讓 KV allocation 的意義不明。`poc-strix.yaml` 的 provider model cards 同樣標示 65,536，代表 backend serving limit，不是模型架構最大值。/ The default serving context is explicitly fixed at **65,536 tokens (64K)**, with `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_MAX_LOADED_MODELS=1` so parallel slots or co-resident models cannot obscure the KV allocation. Provider model cards in `poc-strix.yaml` also say 65,536; this is the backend serving limit, not the architecture maximum.
- Ollama ROCm image 預設使用 `@sha256:` digest；`VLLM_SR_OLLAMA_IMAGE` 可覆寫，但仍必須是 digest-pinned reference。/ The Ollama ROCm image defaults to an `@sha256:` digest. `VLLM_SR_OLLAMA_IMAGE` may override it, but the override must also be digest-pinned.
- 預設 provisioning 已與目前 config 對齊：`gemma4:26b-a4b-it-q8_0`（default）、`gemma4:26b`（fast）、`qwen2.5:7b`、`qwen2.5:14b`、`qwen3:14b`。QAT／31B／120B explicit-only profiles 不會意外下載；可用 `VLLM_SR_OLLAMA_MODELS` 明確加入。/ Default provisioning now matches the current config: `gemma4:26b-a4b-it-q8_0` (default), `gemma4:26b` (fast), `qwen2.5:7b`, `qwen2.5:14b`, and `qwen3:14b`. Explicit-only QAT/31B/120B profiles are not downloaded unexpectedly; add them explicitly with `VLLM_SR_OLLAMA_MODELS`.
- `preflight` 對不相符或使用中的 container 採 fail closed：不自動 stop/restart/remove/recreate。`provision` 也只拉缺少的 model，不刷新既有 tag。/ `preflight` fails closed on a mismatched or in-use container: it never automatically stops, restarts, removes, or recreates one. `provision` also pulls only missing models and does not refresh existing tags.
- 128K（131,072）只允許作明確 experimental 配置，需同時設定 `VLLM_SR_OLLAMA_CONTEXT_LENGTH=131072` 與 `VLLM_SR_ALLOW_EXPERIMENTAL_CONTEXT=1`，並應使用專用／空的 runtime。未完成後續 exact-token capacity、品質與可靠性 acceptance 前，不可宣稱 128K 支援。/ 128K (131,072) is allowed only as an explicit experimental configuration, requiring both `VLLM_SR_OLLAMA_CONTEXT_LENGTH=131072` and `VLLM_SR_ALLOW_EXPERIMENTAL_CONTEXT=1`, preferably on a dedicated/empty runtime. Do not claim 128K support until the later exact-token capacity, quality, and reliability acceptance passes.

`--runtime-only` 在 model pull 後保存 configured provenance；`--runtime-proof` 再送一個不含 `num_ctx` 的 1-token request，讓 server default 生效，然後要求 `/api/ps`／`ollama ps` 顯示 65,536 並記錄 processor/offload。JSON 預設寫入被 git 忽略的 `.agent-harness/experiments/runtime-context-proof/`，含 host/ROCm/Docker/Ollama/image/model digest/quant/context/processor facts，但不含完整 container env、labels、command 或 prompt。這只證明配置與 allocation，**不等於** 64K exact-prefill、品質或 soak 證據。

`--runtime-only` saves configured provenance after model pull. `--runtime-proof` then sends one 1-token request without `num_ctx`, allowing the server default to take effect, and requires `/api/ps` / `ollama ps` to report 65,536 while recording processor/offload. JSON defaults to the gitignored `.agent-harness/experiments/runtime-context-proof/` and includes host/ROCm/Docker/Ollama/image/model digest/quant/context/processor facts, but never full container environment, labels, commands, or prompts. This proves configuration and allocation only; it is **not** 64K exact-prefill, quality, or soak evidence.

---

## 安全攔截的真實機制 / How Security Blocking Actually Works

在此訊號驅動的 router 中，`pii` 與 `jailbreak` 是**訊號**：命中後只會把請求路由到 `security_guard` 決策，本身不會攔截。輸入端的攔截來自該決策上的 `fast_response` plugin（回 HTTP 200 + 制式拒絕訊息 + `x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`，且不呼叫任何上游模型）。`response_jailbreak` 是第二層，只在 LLM **輸出**被標記時回 HTTP 403。路由路徑中**沒有**內聯 PII 遮罩，遮罩只在 `/api` 分類服務提供。`fast_response` plugin 只接受 `message` 欄位（型別見 [plugin_config.go](../../../src/semantic-router/pkg/config/plugin_config.go)）。

In this signal-driven router, `pii` and `jailbreak` are signals: matching one only routes the request to the `security_guard` decision, it does not block by itself. The input-side block comes from the `fast_response` plugin on that decision (HTTP 200 + a canned refusal + `x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`, with no upstream model call). `response_jailbreak` is the second layer that returns HTTP 403 only when the LLM output is flagged. There is no inline PII masking in the routing path; masking is available only via the `/api` classification service. The `fast_response` plugin accepts only a `message` field (type in [plugin_config.go](../../../src/semantic-router/pkg/config/plugin_config.go)).

---

## 測試分工：本機靜態 vs Strix Halo 即時 / Test Split: On-box Static vs On-Strix-Halo Live

設定與腳本在開發箱（Windows + Python + Docker，無 Go／無 `vllm-sr`／無 AMD GPU）撰寫並做靜態驗證；即時 build／serve／校準在 Strix Halo 上進行。

The config and scripts are authored and statically validated on the dev box (Windows + Python + Docker, no Go / no `vllm-sr` / no AMD GPU); the live build/serve/calibration happens on the Strix Halo.

| 測試 / Test | 在哪裡跑 / Where | 指令 / Command |
| --- | --- | --- |
| 設定結構驗證 / config structural validation | 本機靜態 / on-box static | `python validate_poc_config.py poc-strix.yaml` |
| Python 編譯檢查 / Python compile check | 本機靜態 / on-box static | `python -m py_compile validate_poc_config.py smoke_test.py` |
| Markdown lint | 本機靜態 / on-box static | `npx markdownlint-cli -c tools/linter/markdown/markdownlint.yaml README.md` |
| DSL 產生與驗證 / DSL generate and validate | Strix Halo 即時 / on-Strix-Halo live | `bash gen-dsl.sh`（decompile `poc-strix.yaml` -> `poc-strix.dsl` 再 validate）/ (decompile `poc-strix.yaml` -> `poc-strix.dsl` then validate) |
| 啟動與冒煙 / serve and smoke | Strix Halo 即時 / on-Strix-Halo live | `bash bring-up.sh` 然後 / then `python smoke_test.py` |
| 路由校準 / routing calibration | Strix Halo 即時 / on-Strix-Halo live | `router_calibration_loop.py --probes poc-probes.yaml` |
| Live bench 證據 / live bench evidence | Strix Halo 即時 / on-Strix-Halo live | `BASELINE_BASE_URL=http://localhost:11434/v1 bash run-bench.sh`（選加 `--with-reasoning`）/ (optionally `--with-reasoning`) |

即時驗證與校準的完整步驟見操作手冊第 7 與第 8 節。

The full live-validation and calibration steps are in sections 7 and 8 of the runbook.

---

## 本機 CPU 煙霧測試（Linux/WSL2）/ Local CPU Smoke (Linux/WSL2)

用途：一個免 GPU 的端到端開發檢查，使用 [`cpu-smoke.yaml`](cpu-smoke.yaml)（所有 tier 都指向同一個 `llm-katan` echo 後端）搭配 [`cpu-smoke.sh`](cpu-smoke.sh)，證明 classify -> decide -> route -> security 的管線能完整跑通，無需任何模型下載或 GPU。

Purpose: a GPU-free end-to-end dev check that uses [`cpu-smoke.yaml`](cpu-smoke.yaml) (all tiers point at a single `llm-katan` echo backend) together with [`cpu-smoke.sh`](cpu-smoke.sh) to prove the classify -> decide -> route -> security pipeline runs end to end, with no model download and no GPU.

快速開始 / Quickstart：

```bash
bash cpu-smoke.sh
```

腳本會建立 Docker 網路、啟動 `llm-katan` echo 後端、確保 `vllm-sr` CLI 已安裝、以 CPU 執行 `vllm-sr serve --config cpu-smoke.yaml --minimal`，最後對 listener 跑 `python smoke_test.py`。

The script creates the Docker network, starts the `llm-katan` echo backend, ensures the `vllm-sr` CLI is installed, runs `vllm-sr serve --config cpu-smoke.yaml --minimal` on CPU, then fires `python smoke_test.py` at the listener.

> 重要平台限制：`vllm-sr serve` 無法在原生 Windows 上執行（CLI 會以 "Run from WSL2 or another Linux environment with Docker" 拒絕），請從 WSL2 或 Linux 執行本流程。在原生 Windows 上仍可做靜態驗證（`python validate_poc_config.py cpu-smoke.yaml`）並啟動 `llm-katan` 後端，但無法跑 router。
> IMPORTANT platform note: `vllm-sr serve` does NOT run on native Windows (the CLI refuses with "Run from WSL2 or another Linux environment with Docker"); run this flow from WSL2 or Linux. On native Windows you can still validate statically (`python validate_poc_config.py cpu-smoke.yaml`) and run the `llm-katan` backend, but not the router.
>
> 已於 Windows 驗證：`cpu-smoke.yaml` 通過驗證器，且 `llm-katan` echo 後端可啟動並提供 `test-model`；完整即時執行確認需要 Linux/WSL2。
> Verified on Windows: `cpu-smoke.yaml` passes the validator and the `llm-katan` echo backend runs and serves `test-model`; the full live run was confirmed to require Linux/WSL2.

---

## 參考連結 / Reference Links

- 操作手冊 / Runbook: [docs/poc/03-strix-halo-runbook.md](../../../docs/poc/03-strix-halo-runbook.md)
- 參考路由設定 / Reference routing profile: [balance.yaml](../balance.yaml)
- 參考 probe 套件 / Reference probe suite: [balance.probes.yaml](../balance.probes.yaml)
