# Strix Halo PoC 彩排與 Go/No-Go 檢查表 / Strix Halo PoC Rehearsal and Go/No-Go Checklist

> demo 前在 Strix Halo（Ryzen AI Max+ 395，gfx1151）上的一次完整乾跑：逐一通過每個關卡，每關都有明確的通過條件與證據。任一關卡 No-Go 就先修好再往下走。
> A full pre-demo dry run on the Strix Halo (Ryzen AI Max+ 395, gfx1151): pass each gate in order, each with an explicit pass condition and evidence. Any No-Go gate must be fixed before moving on.

本檔是 [docs/poc/03-strix-halo-runbook.md](../../../docs/poc/03-strix-halo-runbook.md) 的彩排對照表；指令細節以操作手冊為準。

This file is the rehearsal companion to [docs/poc/03-strix-halo-runbook.md](../../../docs/poc/03-strix-halo-runbook.md); the runbook remains the source of truth for command details.

---

## 時程建議 / Suggested timing

- 建議 demo 前一天完成關卡 A–D，demo 當天開場前 30 分鐘重跑 E–G。
  Finish gates A-D the day before; re-run E-G 30 minutes before the demo.

---

## 快速 CPU 預彩排（選配，WSL2/Linux）/ Quick CPU pre-rehearsal (optional, WSL2/Linux)

在等待 Strix Halo 或想先驗證管線時，可先在 WSL2/Linux 上跑一次免 GPU 的 CPU 端到端煙霧。安全 lane 與 `poc-strix.yaml` 完全相同，只是 providers/backends 指向 `llm-katan` echo 後端。

While waiting for the Strix Halo, or to validate the pipeline first, run the GPU-free CPU end-to-end smoke on WSL2/Linux. The security lane is identical to `poc-strix.yaml`; only providers/backends point at the `llm-katan` echo backend.

```bash
bash deploy/recipes/strix-halo-poc/cpu-smoke.sh
```

- [ ] CPU 預彩排通過 / CPU pre-rehearsal passes
  - 通過條件 / Pass: [cpu-smoke.sh](cpu-smoke.sh) 跑完，`smoke_test.py` 顯示分層路由，PII/jailbreak 案例顯示 `x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`。/ [cpu-smoke.sh](cpu-smoke.sh) completes and `smoke_test.py` shows tiered routing, with the PII/jailbreak cases showing `x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`.
  - 證據 / Evidence: `smoke_test.py` 終端輸出。/ the `smoke_test.py` terminal output.

> 平台限制 / Platform note：`vllm-sr serve` 無法在原生 Windows 上執行，本預彩排請從 WSL2 或 Linux 跑。
> `vllm-sr serve` does not run on native Windows; run this pre-rehearsal from WSL2 or Linux.

---

## Go/No-Go 關卡 / Go/No-Go gates

### 關卡 A：ROCm / gfx1151 已就緒 / Gate A: ROCm / gfx1151 ready

```bash
rocminfo | grep -i gfx
ls -l /dev/kfd /dev/dri
groups | tr ' ' '\n' | grep -E 'video|render'
```

- [ ] A 通過 / Gate A passes
  - 通過條件 / Pass: `rocminfo` 顯示 `gfx1151`；`/dev/kfd` 與 `/dev/dri` 存在；目前使用者在 `video` 與 `render` 群組。/ `rocminfo` shows `gfx1151`; `/dev/kfd` and `/dev/dri` exist; the current user is in the `video` and `render` groups.
  - 證據 / Evidence: 上述三條指令的輸出。/ the output of the three commands above.

### 關卡 B：安全模組模型已就位或可下載 / Gate B: security module models present or downloadable

`poc-strix.yaml` 的 `global.model_catalog` 需要兩個本地安全模型：`models/mmbert32k-jailbreak-detector-merged`（prompt_guard）與 `models/pii_classifier_modernbert-base_presidio_token_model`（pii 分類器，公開於 HF `LLM-Semantic-Router`）。兩者在第一次 serve 時會下載（需網路；私有 repo 才需 `HF_TOKEN`）。

The `global.model_catalog` in `poc-strix.yaml` needs two local security models: `models/mmbert32k-jailbreak-detector-merged` (prompt_guard) and `models/pii_classifier_modernbert-base_presidio_token_model` (pii classifier, public on HF `LLM-Semantic-Router`). Both are downloaded on first serve (network required; only a private repo needs `HF_TOKEN`).

```bash
# 若已預先放好 / if pre-staged:
ls -d models/mmbert32k-jailbreak-detector-merged models/pii_classifier_modernbert-base_presidio_token_model

# 若需手動預先下載 PII 模型 / if pre-staging the PII model manually:
hf download LLM-Semantic-Router/pii_classifier_modernbert-base_presidio_token_model \
  --local-dir models/pii_classifier_modernbert-base_presidio_token_model

# 若需從 Hugging Face 下載私有模型 / if downloading a private model from Hugging Face:
export HF_TOKEN=hf_xxx
```

> 重要 / Important：ROCm router 透過 ONNX Runtime 載入 token 分類器，但 HF 上的
> `pii_classifier_modernbert-base_presidio_token_model` 只發佈 safetensors，需要先匯出成
> `models/.../onnx/model.onnx`（mmBERT 模型自帶 `onnx/`，此模型沒有）。**此步驟已自動化**：
> [bring-up.sh](bring-up.sh) 的步驟 `[4/5]` 會在 `onnx/model.onnx` 不存在時，用 optimum 由
> safetensors 匯出（自建一次性 venv 安裝 `transformers>=4.48`、`optimum[onnxruntime]`、
> `onnx`、`torch`）；若已存在則略過，因此可重複執行而不會重做匯出。
> The ROCm router loads token classifiers via ONNX Runtime, but
> `pii_classifier_modernbert-base_presidio_token_model` on HF ships safetensors only and
> must first be exported to `models/.../onnx/model.onnx` (the mmBERT models bundle their
> own `onnx/`; this one does not). **This is now automated**: step `[4/5]` of
> [bring-up.sh](bring-up.sh) exports it from the safetensors via optimum (in a one-time
> venv that installs `transformers>=4.48`, `optimum[onnxruntime]`, `onnx`, `torch`) when
> `onnx/model.onnx` is missing, and skips when it already exists — so bring-up is
> idempotent and re-running it never re-exports.

```bash
# bring-up.sh 步驟 [4/5] 已自動處理；若要手動驗證匯出結果 / bring-up.sh step [4/5]
# handles this automatically; to verify the exported artifact manually:
ls models/pii_classifier_modernbert-base_presidio_token_model/onnx/model.onnx
```

> 注意 / Note：在 ONNX/ROCm binding 中，PII token 分類器只有「mmBERT-32K」路徑會把模型
> 註冊／查詢為名稱 `pii`（init 與 inference 一致），其載入器與模型無關，會載入該模型目錄下
> 的任何 ONNX；因此 `modules.classifier.pii` 用 `use_mmbert_32k: true` 搭配 ModernBERT
> `model_id` 才能正確服務。`use_mmbert_32k: false` 路徑在此 binding 會註冊成 `bert_token`、
> 但 inference 仍查 `pii`，導致請求時 `PII classifier 'pii' not found`。
> In the ONNX/ROCm binding, only the "mmBERT-32K" PII path registers and looks up the
> token classifier under the name `pii` consistently; its loader is model-agnostic and
> loads whatever ONNX lives in the model dir. So `modules.classifier.pii` must use
> `use_mmbert_32k: true` together with the ModernBERT `model_id`. The
> `use_mmbert_32k: false` path registers `bert_token` but inference still looks up `pii`,
> producing `PII classifier 'pii' not found` at request time.

- [ ] B 通過 / Gate B passes
  - 通過條件 / Pass: 兩個模型目錄已存在，且 PII 模型含 `onnx/model.onnx`（由 [bring-up.sh](bring-up.sh) 步驟 `[4/5]` 自動匯出，已存在則略過）；或具備網路與（必要時）`HF_TOKEN` 可下載，後續 bring-up 會自動完成 ONNX 匯出。/ both model directories exist and the PII model has `onnx/model.onnx` (auto-exported by [bring-up.sh](bring-up.sh) step `[4/5]`, skipped when already present); or network plus (if needed) `HF_TOKEN` is in place to download, after which bring-up performs the ONNX export automatically.
  - 證據 / Evidence: `ls` 列出兩個目錄與 PII `onnx/model.onnx`，或 bring-up 步驟 `[4/5]` 的匯出／略過日誌。/ `ls` listing both directories and the PII `onnx/model.onnx`, or the bring-up step `[4/5]` export/skip log.

### 關卡 C：DSL 產生並驗證通過 / Gate C: DSL generated and validated

```bash
bash deploy/recipes/strix-halo-poc/gen-dsl.sh
```

- [ ] C 通過 / Gate C passes
  - 通過條件 / Pass: [gen-dsl.sh](gen-dsl.sh) 由 [poc-strix.yaml](poc-strix.yaml) 產生 `poc-strix.dsl`，且 `go run ./cmd/dsl validate` 回報 0 個錯誤。/ [gen-dsl.sh](gen-dsl.sh) generates `poc-strix.dsl` from [poc-strix.yaml](poc-strix.yaml) and `go run ./cmd/dsl validate` reports 0 errors.
  - 證據 / Evidence: 腳本的 validate 輸出。/ the script's validate output.
  - 備註 / Note: `poc-strix.dsl` 是產生物，不入庫。/ `poc-strix.dsl` is a generated artifact and is not committed.

### 關卡 D：建置 ROCm router 映像 / Gate D: build the ROCm router image

```bash
make vllm-sr-dev VLLM_SR_PLATFORM=amd
```

- [ ] D 通過 / Gate D passes
  - 通過條件 / Pass: 本地 dev 映像建置成功，無錯誤。/ the local dev image builds successfully with no errors.
  - 證據 / Evidence: make 的成功輸出。/ the successful make output.

### 關卡 E：bring-up 與健康狀態 / Gate E: bring-up and health

```bash
bash deploy/recipes/strix-halo-poc/bring-up.sh
vllm-sr status
```

- [ ] E 通過 / Gate E passes
  - 通過條件 / Pass: [bring-up.sh](bring-up.sh) 完成（Ollama + 5 個 tier 模型 + router `--platform amd`），且 `vllm-sr status` 顯示各容器健康。/ [bring-up.sh](bring-up.sh) completes (Ollama + 5 tier models + router `--platform amd`) and `vllm-sr status` shows the containers healthy.
  - 證據 / Evidence: `vllm-sr status` 輸出；listener `:8899` 有回應。/ the `vllm-sr status` output; the `:8899` listener responds.

### 關卡 F：煙霧測試證據 / Gate F: smoke-test evidence

```bash
python deploy/recipes/strix-halo-poc/smoke_test.py
```

- [ ] F 通過 / Gate F passes
  - 通過條件 / Pass: 4 個案例皆有回應，且：(1) 簡單問答路由到 SIMPLE 本地模型；(2) 困難推理升級到 COMPLEX/REASONING/PREMIUM 並開啟 reasoning；(3) 含 PII 與 (4) jailbreak 兩案顯示輸入端攔截 `x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`；若仍打到模型，jailbreak 輸出由第二層 `response_jailbreak` 回 HTTP 403。/ all four cases respond, and: (1) the easy question routes to the SIMPLE local model; (2) the hard reasoning case escalates to COMPLEX/REASONING/PREMIUM with reasoning on; (3) the PII and (4) jailbreak cases show the input-side block `x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`; if a model is still hit, the jailbreak output returns HTTP 403 via the `response_jailbreak` second layer.
  - 證據 / Evidence: [smoke_test.py](smoke_test.py) 終端輸出（含每個案例的 `x-vsr-*` 標頭）。/ the [smoke_test.py](smoke_test.py) terminal output (including the `x-vsr-*` headers per case).
  - 修正後實測（measured-on 2026-06-22）/ Post-fix measurement (measured-on 2026-06-22)：以 [router_calibration_loop.py](../../../tools/agent/scripts/router_calibration_loop.py) 對 [poc-probes.yaml](poc-probes.yaml) 跑暖機穩態評測——probe 通過率 **61/62（98.4%）**、decision coverage **13/14（92.9%）**；三個目標誤路由（`battery_degradation`、`plg_vs_slg`、`hi`）已修正，唯一未過為 `complex_specialist:qubit_decoherence`（domain 分類器把該題誤標為 psychology，屬分類器極限而非路由規則問題）。`security_guard` 對 benign 流量維持 **0 誤判**（fast_qa 的 `who_are_you` / `australia_capital` 與三個攻擊 probe 皆未回歸）。/ Running [router_calibration_loop.py](../../../tools/agent/scripts/router_calibration_loop.py) against [poc-probes.yaml](poc-probes.yaml) at warm steady state: probe pass rate **61/62 (98.4%)**, decision coverage **13/14 (92.9%)**; the three target misroutes (`battery_degradation`, `plg_vs_slg`, `hi`) are fixed, with the only remaining miss being `complex_specialist:qubit_decoherence` (the domain classifier mislabels it as psychology—a classifier limit, not a routing-rule issue). `security_guard` holds **0 false positives** on benign traffic (fast_qa `who_are_you` / `australia_capital` and the three attack probes did not regress).

### 關卡 G：dashboard 故事 / Gate G: dashboard story

- [ ] G 通過 / Gate G passes
  - 通過條件 / Pass: dashboard（`http://<host>:8700`）同時顯示成本節省數字與模型分佈（本地承載率）。/ the dashboard (`http://<host>:8700`) shows both the cost-savings number and the model distribution (local-served ratio).
  - 證據 / Evidence: dashboard 截圖或現場畫面。/ a dashboard screenshot or the live view.
  - 修正後實機證據（measured-on 2026-06-22）/ Post-fix live evidence (measured-on 2026-06-22)：agentic 多輪 benchmark（8 sessions × 12 turns，tool-heavy）success rate **100%**（96/96，0 session errors）、latency p95/p99 **4062 / 9672 ms**、約 66% 流量留本地（qwen 63 / gemini-3.1-pro 33），產物在 `.agent-harness/experiments/live-agentic-routing/20260622T120341Z/`；router-replay → fleet-sim 回放 131 筆 trace 得 **28 GPUs、$458K/yr、P99 8.1ms、SLO 100%**，`vllm-sr-sim optimize` 機群最佳化省 **36.1%**（23 vs 36 GPUs，$445.3K vs $696.9K/yr）。詳見 [04-dashboard-tour.md](../../../docs/poc/04-dashboard-tour.md) 動線第 6/10 步。/ The agentic multi-turn benchmark (8 sessions × 12 turns, tool-heavy) reached **100%** success (96/96, 0 session errors), latency p95/p99 **4062 / 9672 ms**, with ~66% of traffic kept local (qwen 63 / gemini-3.1-pro 33); artifacts in `.agent-harness/experiments/live-agentic-routing/20260622T120341Z/`. The router-replay → fleet-sim replay of 131 trace records yields **28 GPUs, $458K/yr, P99 8.1ms, SLO 100%**, and `vllm-sr-sim optimize` sizes the fleet for a **36.1%** saving (23 vs 36 GPUs, $445.3K vs $696.9K/yr). See steps 6/10 of [04-dashboard-tour.md](../../../docs/poc/04-dashboard-tour.md).

---

## Go/No-Go 判定 / Go/No-Go decision

- [ ] 全部關卡 A–G 通過 = **Go**（可進行 demo）。/ all gates A-G pass = **Go** (proceed with the demo).
- [ ] 任一關卡未通過 = **No-Go**：先依操作手冊第 11 節疑難排解修好，再重跑該關卡與其後關卡。/ any gate fails = **No-Go**: fix it via runbook section 11 troubleshooting, then re-run that gate and the ones after it.

---

## 參考連結 / Reference links

- 操作手冊 / Runbook: [docs/poc/03-strix-halo-runbook.md](../../../docs/poc/03-strix-halo-runbook.md)
- PoC 套件說明 / PoC bundle README: [README.md](README.md)
- DSL 產生腳本 / DSL generation script: [gen-dsl.sh](gen-dsl.sh)
- 啟動腳本 / Bring-up script: [bring-up.sh](bring-up.sh)
- 冒煙測試 / Smoke test: [smoke_test.py](smoke_test.py)
- CPU 煙霧 / CPU smoke: [cpu-smoke.sh](cpu-smoke.sh)
