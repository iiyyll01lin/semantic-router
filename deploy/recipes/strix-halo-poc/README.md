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
| [`smoke_test.py`](smoke_test.py) | 純標準函式庫（urllib）冒煙測試：對 `:8899` 送出 4 個示範請求（簡單事實／困難推理／含 PII／jailbreak），印出狀態與 `x-vsr-*` 標頭並標示安全攔截。/ Stdlib-only (urllib) smoke test: POSTs the 4 demo requests (easy factual / hard reasoning / PII / jailbreak) to `:8899` and prints status, `x-vsr-*` headers, and security-block flags. |
| [`validate_poc_config.py`](validate_poc_config.py) | 離線 PyYAML 設定驗證器，本機即可跑（即本資料夾的 on-box 測試）。檢查模型解析、`default_model`、決策 `modelRefs`、決策 `rules` 的 signal 參照與 `provider_model_id`。/ An offline PyYAML config validator that runs on this box (the on-box test). Checks model resolution, `default_model`, decision `modelRefs`, decision-rule signal references, and `provider_model_id`. |
| [`poc-probes.yaml`](poc-probes.yaml) | 校準 probe 套件：balance 的 13 條決策 probe 加上期望路由到 `security_guard` 的安全 probe。/ The calibration probe suite: the balance 13-decision probes plus security probes expecting the `security_guard` decision. |

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

## 測試分工：本機靜態 vs Strix Halo 即時 / Test Split: On-box Static vs On-Strix-Halo Live

設定與腳本在開發箱（Windows + Python + Docker，無 Go／無 `vllm-sr`／無 AMD GPU）撰寫並做靜態驗證；即時 build／serve／校準在 Strix Halo 上進行。

The config and scripts are authored and statically validated on the dev box (Windows + Python + Docker, no Go / no `vllm-sr` / no AMD GPU); the live build/serve/calibration happens on the Strix Halo.

| 測試 / Test | 在哪裡跑 / Where | 指令 / Command |
| --- | --- | --- |
| 設定結構驗證 / config structural validation | 本機靜態 / on-box static | `python validate_poc_config.py poc-strix.yaml` |
| Python 編譯檢查 / Python compile check | 本機靜態 / on-box static | `python -m py_compile validate_poc_config.py smoke_test.py` |
| Markdown lint | 本機靜態 / on-box static | `npx markdownlint-cli -c tools/linter/markdown/markdownlint.yaml README.md` |
| DSL 驗證 / DSL validation | Strix Halo 即時 / on-Strix-Halo live | `go run ./cmd/dsl validate <poc-strix.dsl>` |
| 啟動與冒煙 / serve and smoke | Strix Halo 即時 / on-Strix-Halo live | `bash bring-up.sh` 然後 / then `python smoke_test.py` |
| 路由校準 / routing calibration | Strix Halo 即時 / on-Strix-Halo live | `router_calibration_loop.py --probes poc-probes.yaml` |

即時驗證與校準的完整步驟見操作手冊第 7 與第 8 節。

The full live-validation and calibration steps are in sections 7 and 8 of the runbook.

---

## 參考連結 / Reference Links

- 操作手冊 / Runbook: [docs/poc/03-strix-halo-runbook.md](../../../docs/poc/03-strix-halo-runbook.md)
- 參考路由設定 / Reference routing profile: [balance.yaml](../balance.yaml)
- 參考 probe 套件 / Reference probe suite: [balance.probes.yaml](../balance.probes.yaml)
