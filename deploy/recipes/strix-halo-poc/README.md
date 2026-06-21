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
| [`bring-up.sh`](bring-up.sh) | 在 Strix Halo 上端到端啟動：建立 Docker 網路、啟動 Ollama（ROCm，GPU passthrough）、下載 5 個 tier 模型、以 `--platform amd` 啟動 router。/ End-to-end bring-up on the Strix Halo: create the Docker network, start Ollama (ROCm, GPU passthrough), pull the 5 tier models, and serve the router with `--platform amd`. |
| [`smoke_test.py`](smoke_test.py) | 純標準函式庫（urllib）冒煙測試：對 `:8899` 送出 4 個示範請求（簡單事實／困難推理／含 PII／jailbreak），印出狀態與 `x-vsr-*` 標頭，並標示輸入端 fast_response 攔截（`x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`）與輸出端 `response_jailbreak` 的 HTTP 403。/ Stdlib-only (urllib) smoke test: POSTs the 4 demo requests (easy factual / hard reasoning / PII / jailbreak) to `:8899`, prints status and `x-vsr-*` headers, and flags the input-side fast_response block (`x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`) and the output-side `response_jailbreak` HTTP 403. |
| [`validate_poc_config.py`](validate_poc_config.py) | 離線 PyYAML 設定驗證器，本機即可跑（即本資料夾的 on-box 測試）。檢查模型解析、`default_model`、決策 `modelRefs`、決策 `rules` 的 signal 參照與 `provider_model_id`。/ An offline PyYAML config validator that runs on this box (the on-box test). Checks model resolution, `default_model`, decision `modelRefs`, decision-rule signal references, and `provider_model_id`. |
| [`gen-dsl.sh`](gen-dsl.sh) | 由 `poc-strix.yaml` 產生並驗證 `poc-strix.dsl`（從 `src/semantic-router` 執行 `go run ./cmd/dsl decompile` 再 `validate`，需要 Go）。`.dsl` 是產生物，不入庫。/ Generates and validates `poc-strix.dsl` from `poc-strix.yaml` (runs `go run ./cmd/dsl decompile` then `validate` from `src/semantic-router`, requires Go). The `.dsl` is a generated artifact and is not committed. |
| [`poc-probes.yaml`](poc-probes.yaml) | 校準 probe 套件：balance 的 13 條決策 probe 加上期望路由到 `security_guard` 的安全 probe。/ The calibration probe suite: the balance 13-decision probes plus security probes expecting the `security_guard` decision. |
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
# 1. 啟動 Ollama + 下載模型 + 啟動 router / start Ollama, pull models, serve the router
bash bring-up.sh

# 2. 送出 4 個示範請求並觀察路由與安全攔截 / send the 4 demo requests and observe routing + security blocks
python smoke_test.py            # 預設 / default --base-url http://localhost:8899

# 3. 離線驗證設定的內部一致性 / offline-validate the config's internal consistency
python validate_poc_config.py poc-strix.yaml
```

> 本機（開發箱）僅有 `python`（非 `python3`）。在 Strix Halo（Ubuntu）上兩者通常皆可。
> This dev box only has `python` (not `python3`). On the Strix Halo (Ubuntu) either usually works.

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
