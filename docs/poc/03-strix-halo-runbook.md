# 單機 Strix Halo PoC 操作手冊 / Single-box Strix Halo PoC Runbook

> 可照抄的雙語操作手冊：在一台 Ubuntu Strix Halo（Ryzen AI Max+ 395，gfx1151）上端到端跑起完整軟體 PoC。
> A copy-pasteable bilingual runbook to bring up the full software PoC end to end on a single Ubuntu Strix Halo box (Ryzen AI Max+ 395, gfx1151).

本文件是 [02-poc-plan.md](02-poc-plan.md) 第 11 節「單機 / Strix Halo 軟體 PoC 變體」的執行步驟，採用該節選定的**方法 C**（同機跑多個本地後端、每個 tier 用真正不同的模型、完全離線）。技術原理見 [01-tech-study.md](01-tech-study.md)。

This document is the execution counterpart to section 11 ("Single-box Strix Halo Software PoC Variant") of [02-poc-plan.md](02-poc-plan.md), using that section's selected **approach C** (multiple local backends on one box, a genuinely different model per tier, fully offline). For the underlying technology, see [01-tech-study.md](01-tech-study.md).

主要 serving stack 為 **Ollama（ROCm）**：一個 server 同時掛多個模型（每個 tier 一個），對外提供 OpenAI 相容 `/v1` API，並自動管理共用的統一記憶體。另提供 **AMD Lemonade Server** 與 **llama.cpp ROCm** 兩種替代方案。

The primary serving stack is **Ollama (ROCm)**: one server hosts multiple models (one per tier), exposes an OpenAI-compatible `/v1` API, and auto-manages the shared unified memory. Two alternatives are also covered: **AMD Lemonade Server** and **llama.cpp ROCm**.

---

## 0. 啟動順序總覽 / Bring-up Order Overview

下圖為建議的端到端啟動順序。每一步完成後再進入下一步。

The diagram below is the recommended end-to-end bring-up order. Finish each step before moving to the next.

```mermaid
flowchart TD
    Prereq["1. Prerequisites: Ubuntu + ROCm gfx1151 + Docker GPU passthrough"] --> Serve["2. Install serving stack + pull tier models (Ollama)"]
    Serve --> Backends["3. Start local backends on vllm-sr-network (approach C)"]
    Backends --> Router["4. Build or install the router"]
    Router --> Config["5. Author the PoC config (poc-strix.yaml + .dsl)"]
    Config --> Security["6. Enable the security lane (PII + jailbreak)"]
    Security --> Up["7. Serve + verify (vllm-sr serve --platform amd)"]
    Up --> Validate["8. Validate + calibrate (dsl validate + calibration loop)"]
    Validate --> Demo["9. Measure + demo (cost, distribution, security)"]
    Demo --> Accept["10. Acceptance checklist"]
    Accept --> Trouble["11. Troubleshooting (as needed)"]
```

接線模型 / Wiring model：router 只做語意決策並改寫請求，由 **Envoy** 負載平衡到實際後端。所有容器（router、Envoy、dashboard、各本地後端）都掛在同一個 Docker network `vllm-sr-network` 上，後端以「容器名稱:埠號」被引用（例如 `ollama:11434`）。

Wiring model: the router only makes semantic decisions and rewrites requests; **Envoy** load-balances to the real backend. All containers (router, Envoy, dashboard, and each local backend) sit on the same Docker network `vllm-sr-network`, and backends are referenced by `container-name:port` (e.g. `ollama:11434`).

### 已驗證的接線事實 / Verified wiring facts

下列事實已從 CLI 原始碼確認，據此撰寫本手冊的設定與啟動步驟。

The following facts were confirmed from the CLI source and drive this runbook's config and startup steps.

| 項目 / Item | 確認結果 / Confirmed value | 來源 / Source |
| --- | --- | --- |
| 預設 Docker network 名稱 / default Docker network name | `vllm-sr-network`（預設 stack；自訂 stack 名稱時為 `<stack>-vllm-sr-network`）/ `vllm-sr-network` (default stack; `<stack>-vllm-sr-network` for a custom stack name) | [runtime_stack.py](../../src/vllm-sr/cli/runtime_stack.py) |
| 後端可達性 / backend reachability | 同網路上的具名容器（`ollama:11434`），或主機閘道 `host.docker.internal:<port>`（router/Envoy 容器會自動加上 `--add-host=host.docker.internal:host-gateway`）/ named container on the shared network (`ollama:11434`), or the host gateway `host.docker.internal:<port>` (router/Envoy containers get `--add-host=host.docker.internal:host-gateway`) | [docker_run_command.py](../../src/vllm-sr/cli/docker_run_command.py), [docker_start.py](../../src/vllm-sr/cli/docker_start.py) |
| router 送往本地 endpoint 的 OpenAI 路徑 / OpenAI path the router emits to a local endpoint | 預設 `/v1/chat/completions`，可用 `backend_refs[].chat_path` 覆寫 / defaults to `/v1/chat/completions`, overridable via `backend_refs[].chat_path` | [chat_client.py](../../src/vllm-sr/cli/chat_client.py), [models.py](../../src/vllm-sr/cli/models.py) |
| AMD GPU passthrough | 掛載 `/dev/kfd` + `/dev/dri`、加上 `--group-add video`（由 `VLLM_SR_AMD_GPU_PASSTHROUGH` 控制）/ mounts `/dev/kfd` + `/dev/dri` with `--group-add video` (gated by `VLLM_SR_AMD_GPU_PASSTHROUGH`) | [docker_run_command.py](../../src/vllm-sr/cli/docker_run_command.py) |

因為 Ollama 的 OpenAI 相容 endpoint 就在 `/v1`，預設 `chat_path`（`/v1/chat/completions`）即可直接對上，無需覆寫。本手冊的假設是：把 Ollama 當成名為 `ollama` 的容器跑在 `vllm-sr-network` 上，因此 `endpoint: ollama:11434`。若你改用 host gateway 模式（serving stack 直接跑在主機而非容器），請把 `endpoint` 改成 `host.docker.internal:11434`。

Because Ollama's OpenAI-compatible endpoint lives at `/v1`, the default `chat_path` (`/v1/chat/completions`) lines up directly with no override needed. This runbook assumes Ollama runs as a container named `ollama` on `vllm-sr-network`, hence `endpoint: ollama:11434`. If instead you run the serving stack directly on the host (not as a container), change `endpoint` to `host.docker.internal:11434`.

---

## 1. 前置需求 / Prerequisites

| 需求 / Requirement | 說明 / Note |
| --- | --- |
| 作業系統 / OS | Ubuntu（x86_64）。ROCm router 映像僅支援 x86_64（見 [Dockerfile.rocm](../../src/vllm-sr/Dockerfile.rocm)），Strix Halo 為 x86 CPU 故同機即可跑 router / Ubuntu (x86_64). The ROCm router image is x86_64 only (see [Dockerfile.rocm](../../src/vllm-sr/Dockerfile.rocm)); Strix Halo is x86 CPU so the router runs on the same box |
| GPU / iGPU | RDNA 3.5 iGPU gfx1151；安裝對 gfx1151 友善的 ROCm 驅動與 runtime / RDNA 3.5 iGPU gfx1151; install a ROCm driver and runtime that support gfx1151 |
| 記憶體 / Memory | 最高 128GB 統一記憶體，由所有並行容器共用，需規劃每個後端的切片 / up to 128GB unified memory shared across all concurrent containers; plan each backend's slice |
| Docker | 可使用 GPU passthrough：`/dev/kfd`、`/dev/dri`，使用者在 `video`/`render` 群組 / Docker with GPU passthrough: `/dev/kfd`, `/dev/dri`, and the user in the `video`/`render` groups |
| 磁碟 / Disk | 模型快取空間（每個 tier 模型數 GB 到數十 GB）/ disk for the model cache (several GB to tens of GB per tier model) |

確認 GPU 與群組 / Verify the GPU and groups：

```bash
rocminfo | grep -i gfx
ls -l /dev/kfd /dev/dri
groups | tr ' ' '\n' | grep -E 'video|render'
```

統一記憶體（GTT）備註 / Unified-memory (GTT) note：Strix Halo 把系統 RAM 當成 GPU 可用的統一記憶體；多個後端同時載入時，請依各模型量化後大小規劃總量，避免超出可用 GTT。

Unified-memory (GTT) note: Strix Halo exposes system RAM as GPU-usable unified memory; when several backends load at once, budget the total against each model's quantized size to avoid exceeding available GTT.

---

## 2. 安裝 serving stack 並下載各 tier 模型 / Install the Serving Stack and Pull Tier Models

主要路徑使用 Ollama（ROCm）。先安裝，再下載每個 tier 的模型。

The primary path uses Ollama (ROCm). Install it first, then pull one model per tier.

### 2.1 安裝 Ollama / Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Ollama 官方網站 / official site: https://ollama.com 。在支援 ROCm 的主機上，Ollama 會自動偵測 AMD GPU；本手冊改以容器方式啟動（見第 3 節），以便和 router 共用同一個 Docker network。

Ollama official site: https://ollama.com . On a ROCm-capable host Ollama auto-detects the AMD GPU; this runbook runs it as a container (see section 3) so it shares the Docker network with the router.

### 2.2 提案的各 tier 預設模型（可替換）/ Proposed default models per tier (swappable)

下列模型為合理起點，請依品質需求與記憶體預算替換。`Ollama 標籤` 為下載與在設定中引用所用的名稱。

The models below are reasonable starting points; swap them to match quality needs and the memory budget. The `Ollama tag` is the name used to pull and to reference in config.

| Tier | 角色 / Role | 提案模型 / Proposed model | Ollama 標籤 / Ollama tag |
| --- | --- | --- | --- |
| SIMPLE | 常規流量、預設模型 / routine traffic, default model | 小型 instruct / small instruct | `llama3.2:3b`（或 `qwen2.5:3b`）|
| MEDIUM | 低成本驗證／解釋 / low-cost verified/explainer | 中型 instruct / mid instruct | `qwen2.5:7b` |
| COMPLEX | 系統設計、硬 STEM、健康 / systems design, hard STEM, health | 中大型 instruct / mid-large instruct | `qwen2.5:14b` |
| REASONING | 形式化推理、證明 / formal reasoning, proofs | 具推理能力模型 / reasoning-capable model | `qwen3:14b`（或重用 COMPLEX 並開啟 reasoning）|
| PREMIUM | 法遵、高風險分析 / legal, high-risk analysis | 本地最大可放入的模型，或選配真實雲端 / largest local model that fits, or optionally real cloud | `qwen2.5:32b` |

下載模型 / Pull the models：

```bash
for tag in llama3.2:3b qwen2.5:7b qwen2.5:14b qwen3:14b qwen2.5:32b; do
  ollama pull "$tag"
done
```

記憶體取捨 / Memory trade-off：所有 tier 都載入時會共用 128GB 統一記憶體。若吃緊，請(1) 減少 tier 數量、(2) 改用較小或量化更激進的標籤、或(3) 讓 Ollama 依需求載入／卸載（見 `OLLAMA_KEEP_ALIVE` 與 `OLLAMA_MAX_LOADED_MODELS`）。

Memory trade-off: loading every tier shares the 128GB unified memory. If it is tight, (1) reduce the number of tiers, (2) use smaller or more aggressively quantized tags, or (3) let Ollama load/unload on demand (see `OLLAMA_KEEP_ALIVE` and `OLLAMA_MAX_LOADED_MODELS`).

---

## 3. 啟動本地後端（方法 C）/ Start Local Backends (Approach C)

### 3.1 主要：Ollama 容器 / Primary: the Ollama container

先建立共用 Docker network（名稱須與 router 預設一致），再以 ROCm passthrough 啟動 Ollama 容器，命名為 `ollama`。

Create the shared Docker network first (the name must match the router default), then start the Ollama container with ROCm passthrough, named `ollama`.

```bash
sudo docker network create vllm-sr-network 2>/dev/null || true

sudo docker run -d \
  --name ollama \
  --network=vllm-sr-network \
  --restart unless-stopped \
  -p 11434:11434 \
  -v ollama:/root/.ollama \
  --device=/dev/kfd \
  --device=/dev/dri \
  --group-add=video \
  --cap-add=SYS_PTRACE \
  --security-opt seccomp=unconfined \
  -e HSA_OVERRIDE_GFX_VERSION=11.5.1 \
  ollama/ollama:rocm
```

備註 / Notes：

- `HSA_OVERRIDE_GFX_VERSION` 對 gfx1151 有時需要設定；若 Ollama 已正確偵測 GPU 可省略。實際值依你的 ROCm 版本而定。
  `HSA_OVERRIDE_GFX_VERSION` is sometimes needed for gfx1151; omit it if Ollama already detects the GPU correctly. The exact value depends on your ROCm version.
- 容器使用 named volume `ollama` 保存已下載模型。若你在第 2.2 節已於主機下載，請改掛主機路徑或在容器內重新 `ollama pull`。
  The container uses a named volume `ollama` to persist pulled models. If you pulled on the host in 2.2, mount the host path instead or re-run `ollama pull` inside the container.

在容器內下載（若使用 named volume）/ Pull inside the container (if using the named volume)：

```bash
for tag in llama3.2:3b qwen2.5:7b qwen2.5:14b qwen3:14b qwen2.5:32b; do
  sudo docker exec ollama ollama pull "$tag"
done
```

驗證每個模型都能在 OpenAI 相容 endpoint 回應 / Verify each model answers on the OpenAI-compatible endpoint：

```bash
curl -s http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
        "model": "llama3.2:3b",
        "messages": [{"role": "user", "content": "ping"}]
      }' | head
```

方法 C 的關鍵 / The key to approach C：一個 Ollama server 同時提供多個模型，但每個 tier 用**真正不同**的模型名稱（`llama3.2:3b`、`qwen2.5:14b`…），因此分層路由展示的是真實的模型差異，而非單一模型的別名。

The key to approach C: one Ollama server serves multiple models, but each tier uses a genuinely different model name (`llama3.2:3b`, `qwen2.5:14b`, ...), so the tiered routing demonstrates real model differentiation rather than aliases over a single model.

### 3.2 替代方案 A：AMD Lemonade Server / Alternative A: AMD Lemonade Server

Lemonade 是 AMD 官方掛牌的本地推理 server，對客戶故事特別有說服力，並提供 OpenAI 相容 API。

Lemonade is the AMD-branded local inference server, which is especially compelling for the customer story, and it exposes an OpenAI-compatible API.

- 官方網站 / official site: https://lemonade-server.ai
- 啟動後同樣以「容器名稱:埠號」或 `host.docker.internal:<port>` 在設定中引用其 OpenAI 相容 endpoint；其餘設定與 Ollama 路徑相同。
  Once running, reference its OpenAI-compatible endpoint the same way (by `container-name:port` or `host.docker.internal:<port>`) in config; everything else matches the Ollama path.
- 與 Ollama 相同，多個模型由同一 server 提供，各 tier 指向不同模型名稱即可滿足方法 C。
  As with Ollama, one server hosts multiple models, and pointing each tier at a different model name satisfies approach C.

### 3.3 替代方案 B：llama.cpp ROCm（每 tier 嚴格切 VRAM）/ Alternative B: llama.cpp ROCm (strict per-tier VRAM slices)

若需要**每個 tier 嚴格切一塊 VRAM**，可為每個 tier 跑一個獨立的 `llama-server`，各自綁不同埠號。

If you need a strict VRAM slice per tier, run one `llama-server` per tier, each bound to a different port.

- 官方專案 / official project: https://github.com/ggml-org/llama.cpp （以 `GGML_HIP`/ROCm 建置）/ (build with `GGML_HIP`/ROCm)
- 每個 tier 一個容器、一個埠號（例如 SIMPLE `:8001`、MEDIUM `:8002`、COMPLEX `:8003`…），全部掛在 `vllm-sr-network` 上。
  One container and one port per tier (e.g. SIMPLE `:8001`, MEDIUM `:8002`, COMPLEX `:8003`, ...), all on `vllm-sr-network`.
- 在設定中，各 tier 的 `backend_refs.endpoint` 指向各自的「容器名稱:埠號」（而非像 Ollama 那樣共用同一個 endpoint）。
  In config, each tier's `backend_refs.endpoint` points at its own `container-name:port` (rather than sharing one endpoint as with Ollama).

範例（單一 tier 容器）/ Example (a single tier container)：

```bash
sudo docker run -d \
  --name llamacpp-simple \
  --network=vllm-sr-network \
  -p 8001:8001 \
  -v "$HOME/models:/models" \
  --device=/dev/kfd --device=/dev/dri --group-add=video \
  --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
  ghcr.io/ggml-org/llama.cpp:server-rocm \
  -m /models/simple.gguf --host 0.0.0.0 --port 8001 --n-gpu-layers 99
```

三種 serving stack 比較 / Comparison of the three serving stacks：

| Serving stack | 多模型方式 / Multi-model approach | 每 tier 記憶體控制 / Per-tier memory control | 在設定中的 endpoint / Endpoint in config |
| --- | --- | --- | --- |
| Ollama（主要）/ Ollama (primary) | 一個 server 多模型 / one server, many models | 由 Ollama 自動管理 / Ollama auto-manages | 全 tier 共用 `ollama:11434`，模型名稱不同 / all tiers share `ollama:11434`, different model names |
| AMD Lemonade | 一個 server 多模型 / one server, many models | 由 Lemonade 管理 / managed by Lemonade | 共用單一 endpoint，模型名稱不同 / one shared endpoint, different model names |
| llama.cpp ROCm | 每 tier 一個 server / one server per tier | 每容器嚴格切 VRAM / strict per-container VRAM slice | 每 tier 不同埠號 / different port per tier |

> Ollama、Lemonade、llama.cpp 為通用 AMD 生態工具，非本 repo 的檔案。
> Ollama, Lemonade, and llama.cpp are general AMD ecosystem tools, not files in this repo.

---

## 4. 取得並建置 router / Build or Install the Router

兩種方式擇一 / Pick one of two ways：

```bash
# 方式 A：自建 ROCm 版本 / Option A: build the ROCm variant locally
make vllm-sr-dev VLLM_SR_PLATFORM=amd

# 方式 B：安裝官方版本 / Option B: install the official build
curl -fsSL https://vllm-semantic-router.com/install.sh | bash
```

CLI 進入點為 [main.py](../../src/vllm-sr/cli/main.py)，serve 實作為 [runtime.py](../../src/vllm-sr/cli/commands/runtime.py)。預設拓樸為 split（router + Envoy + dashboard 各自獨立容器，見 [consts.py](../../src/vllm-sr/cli/consts.py)）。

The CLI entry is [main.py](../../src/vllm-sr/cli/main.py) and the serve implementation is [runtime.py](../../src/vllm-sr/cli/commands/runtime.py). The default topology is split (router + Envoy + dashboard as separate containers, see [consts.py](../../src/vllm-sr/cli/consts.py)).

---

## 5. 撰寫 PoC config / Author the PoC Config

本 repo 已提供改寫好的設定：[deploy/recipes/strix-halo-poc/poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml)（由參考設定 [balance.yaml](../../deploy/recipes/balance.yaml)（v0.3）改寫）。最小改動原則：**保留** 既有的 5 個模型 `name`（13 條決策仍引用它們）與每 tier `pricing`（這樣 dashboard 的成本節省才會顯示），只把每個模型的 `backend_refs.endpoint` 與 `provider_model_id` 改成本地 Ollama endpoint 與該 tier 的真實模型名稱。

The adapted config already lives in this repo: [deploy/recipes/strix-halo-poc/poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml) (adapted from the reference profile [balance.yaml](../../deploy/recipes/balance.yaml), v0.3). Minimal-change principle: keep the existing 5 model `name`s (the 13 decisions still reference them) and the per-tier `pricing` (so the dashboard still shows cost savings), and only change each model's `backend_refs.endpoint` and `provider_model_id` to the local Ollama endpoint and that tier's real model name.

設定 schema 見 [models.py](../../src/vllm-sr/cli/models.py)（`BackendRef.endpoint` 用於本地 OpenAI 相容後端；`base_url` + `provider` + `api_key_env` 用於遠端 frontier）；完整對照範例見 [config/config.yaml](../../config/config.yaml)。

The config schema is in [models.py](../../src/vllm-sr/cli/models.py) (`BackendRef.endpoint` for a local OpenAI-compatible backend; `base_url` + `provider` + `api_key_env` for a remote frontier); a full reference example is in [config/config.yaml](../../config/config.yaml).

### 5.1 `providers.models[]` 的 backend_refs 片段 / The `providers.models[]` backend_refs snippet

下列片段示範把 balance.yaml 的 5 個模型重新指向本地 Ollama（方法 C，全 tier 共用 `ollama:11434`，模型名稱不同）。`name` 維持不變，`provider_model_id` 改為 Ollama 標籤，`pricing` 保留誇大值供 Insights demo。

The snippet below repoints balance.yaml's 5 models at local Ollama (approach C, all tiers share `ollama:11434` with different model names). The `name` stays the same, `provider_model_id` becomes the Ollama tag, and `pricing` keeps the exaggerated values for Insights demos.

```yaml
providers:
  defaults:
    default_model: qwen/qwen3.5-rocm   # SIMPLE tier stays the default model
    default_reasoning_effort: low
    reasoning_families:
      qwen3:
        parameter: enable_thinking
        type: chat_template_kwargs
  models:
    - name: qwen/qwen3.5-rocm          # SIMPLE
      provider_model_id: llama3.2:3b   # real local model served by Ollama
      reasoning_family: qwen3
      backend_refs:
        - endpoint: ollama:11434       # named container on vllm-sr-network
          name: ollama_local
          protocol: http
          weight: 1
      pricing:
        currency: USD
        prompt_per_1m: 0
        cached_input_per_1m: 0
        completion_per_1m: 0
    - name: google/gemini-2.5-flash-lite   # MEDIUM
      provider_model_id: qwen2.5:7b
      reasoning_family: qwen3
      backend_refs:
        - endpoint: ollama:11434
          name: ollama_local
          protocol: http
          weight: 1
      pricing:
        currency: USD
        prompt_per_1m: 0.01
        cached_input_per_1m: 0.002
        completion_per_1m: 0.04
    - name: google/gemini-3.1-pro          # COMPLEX
      provider_model_id: qwen2.5:14b
      reasoning_family: qwen3
      backend_refs:
        - endpoint: ollama:11434
          name: ollama_local
          protocol: http
          weight: 1
      pricing:
        currency: USD
        prompt_per_1m: 0.48
        cached_input_per_1m: 0.12
        completion_per_1m: 1.92
    - name: openai/gpt5.4                   # REASONING
      provider_model_id: qwen3:14b
      reasoning_family: qwen3
      backend_refs:
        - endpoint: ollama:11434
          name: ollama_local
          protocol: http
          weight: 1
      pricing:
        currency: USD
        prompt_per_1m: 1.2
        cached_input_per_1m: 0.3
        completion_per_1m: 4.8
    - name: anthropic/claude-opus-4.6       # PREMIUM
      provider_model_id: qwen2.5:32b        # largest local model that fits
      reasoning_family: qwen3
      backend_refs:
        - endpoint: ollama:11434
          name: ollama_local
          protocol: http
          weight: 1
      pricing:
        currency: USD
        prompt_per_1m: 1.8
        cached_input_per_1m: 0.45
        completion_per_1m: 7.2
```

備註 / Notes：

- `provider_model_id` 是真正送給後端 `model` 欄位的值，必須等於 Ollama 標籤（例如 `qwen2.5:14b`）。`name` 是路由內部的邏輯名稱，決策的 `modelRefs[].model` 引用它。
  `provider_model_id` is the value actually sent in the backend `model` field and must equal the Ollama tag (e.g. `qwen2.5:14b`). `name` is the routing-internal logical name that decisions reference via `modelRefs[].model`.
- `endpoint` 用「容器名稱:埠號」`ollama:11434`，因為後端與 router 同在 `vllm-sr-network`。預設 `chat_path` 為 `/v1/chat/completions`，與 Ollama 的 `/v1` 相容，無需覆寫。
  `endpoint` uses `container-name:port` `ollama:11434` because the backend and router share `vllm-sr-network`. The default `chat_path` (`/v1/chat/completions`) is compatible with Ollama's `/v1`, so no override is needed.
- `reasoning_family: qwen3` 透過 `enable_thinking` chat-template 參數控制思考開關（見 [01-tech-study.md](01-tech-study.md) 4.4 節）。若某 tier 改用非 qwen 模型，請相應調整或移除 reasoning family。
  `reasoning_family: qwen3` toggles thinking via the `enable_thinking` chat-template parameter (see section 4.4 of [01-tech-study.md](01-tech-study.md)). If a tier switches to a non-qwen model, adjust or drop the reasoning family accordingly.
- 替代方案 B（llama.cpp）時，把各 tier 的 `endpoint` 改為各自的「容器名稱:埠號」（如 `llamacpp-simple:8001`），`provider_model_id` 改為各 server 所載入的模型名稱。
  For alternative B (llama.cpp), change each tier's `endpoint` to its own `container-name:port` (e.g. `llamacpp-simple:8001`) and set `provider_model_id` to the model each server loaded.
- 選配真實雲端 PREMIUM / optional real-cloud PREMIUM：把該模型的 `backend_refs` 改成 `{ base_url: https://api.openai.com/v1, provider: openai, api_key_env: OPENAI_API_KEY }`，金鑰由環境變數讀取。
  Optional real-cloud PREMIUM: change that model's `backend_refs` to `{ base_url: https://api.openai.com/v1, provider: openai, api_key_env: OPENAI_API_KEY }`; the key is read from an env var.

### 5.2 一條決策範例 / One decision example

`routing.decisions[]` 與 `modelCards` 直接沿用 balance.yaml（不需改動，因為它們引用的是 `name` 而非後端）。下例為 SIMPLE 的 fallback 決策，引用 `qwen/qwen3.5-rocm`（已在 5.1 重新指向 `llama3.2:3b`）。

`routing.decisions[]` and `modelCards` carry over from balance.yaml unchanged (they reference `name`, not the backend). The example below is the SIMPLE fallback decision referencing `qwen/qwen3.5-rocm` (repointed to `llama3.2:3b` in 5.1).

```yaml
routing:
  decisions:
    - name: simple_general
      description: Lowest-cost fallback for everyday traffic and non-specialized requests.
      priority: 170
      tier: 13
      modelRefs:
        - model: qwen/qwen3.5-rocm     # -> provider_model_id llama3.2:3b on Ollama
          use_reasoning: false
      rules:
        operator: OR
        conditions:
          - operator: AND
            conditions:
              - type: context
                name: short_context
              - operator: OR
                conditions:
                  - type: projection
                    name: balance_simple
                  - type: projection
                    name: balance_medium
```

> 上述調整已套用於 [deploy/recipes/strix-halo-poc/poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml)：完整的 13 條決策、signals、projections 與 modelCards 皆從 [balance.yaml](../../deploy/recipes/balance.yaml) 複製，僅依第 5.1 節調整 `providers.models[]`，並依第 6 節加入安全 lane。
> These changes are already applied in [deploy/recipes/strix-halo-poc/poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml): the full 13 decisions, signals, projections, and modelCards are copied from [balance.yaml](../../deploy/recipes/balance.yaml), only `providers.models[]` is changed per section 5.1, and the security lane from section 6 is added.

---

## 6. 啟用安全 lane（設定任務）/ Enable the Security Lane (a Config Task)

維護中的 [balance.yaml](../../deploy/recipes/balance.yaml) 為了專注 balance，已把 jailbreak/PII 從路由表面移除。安全 demo 需明確加回三樣東西。

The maintained [balance.yaml](../../deploy/recipes/balance.yaml) dropped jailbreak/PII from its routing surface to stay balance-focused. The security demo requires explicitly adding back three things.

1. 在 `routing.signals` 加回 `jailbreak` 與 `pii` 訊號 / Re-add `jailbreak` and `pii` signals under `routing.signals`：

```yaml
routing:
  signals:
    jailbreak:
      - name: jailbreak_attempt
        threshold: 0.7
        description: Detect prompt-injection / jailbreak attempts.
    pii:
      - name: contains_pii
        threshold: 0.5
        description: Detect personally identifiable information in the request.
```

2. 新增一條高優先序的安全決策 lane / Add a high-priority security decision lane：

```yaml
routing:
  decisions:
    - name: security_guard
      description: Deny or down-route requests that trip jailbreak or PII signals.
      priority: 300            # higher than every balance lane so it wins first
      tier: 0
      modelRefs:
        - model: qwen/qwen3.5-rocm   # keep risky traffic on the local model
          use_reasoning: false
      rules:
        operator: OR
        conditions:
          - type: jailbreak
            name: jailbreak_attempt
          - type: pii
            name: contains_pii
      plugins:
        - type: response_jailbreak
          configuration:
            enabled: true
            action: block          # block -> HTTP 403 on jailbreak in the response
```

3. 在 `global.model_catalog.modules` 設定 `prompt_guard` 與 PII 分類器模型 / Configure the `prompt_guard` and PII classifier models under `global.model_catalog.modules`：

```yaml
global:
  model_catalog:
    modules:
      prompt_guard:
        use_cpu: true            # keep the iGPU for the LLM backends
        # model path / threshold per the canonical config reference
      pii_classifier:
        use_cpu: true
```

`PromptGuardConfig` 的型別定義見 [model_config_types.go](../../src/semantic-router/pkg/config/model_config_types.go)；完整的 `global.model_catalog.modules` 範例（prompt_guard、pii_classifier 的模型路徑與門檻）請參照 [config/config.yaml](../../config/config.yaml)。把分類器設為 `use_cpu: true`，以把 iGPU 留給 LLM 後端（與第 7 節 `VLLM_SR_AMD_PRESERVE_CPU=1` 一致）。

`PromptGuardConfig` types are defined in [model_config_types.go](../../src/semantic-router/pkg/config/model_config_types.go); for the full `global.model_catalog.modules` example (model paths and thresholds for prompt_guard and pii_classifier), follow [config/config.yaml](../../config/config.yaml). Set the classifiers to `use_cpu: true` to reserve the iGPU for the LLM backends (consistent with `VLLM_SR_AMD_PRESERVE_CPU=1` in section 7).

---

## 7. 啟動與驗證 / Serve and Verify

以 [deploy/recipes/strix-halo-poc/poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml) 啟動 router，平台為 amd，並設 `VLLM_SR_AMD_PRESERVE_CPU=1` 讓內建分類器留在 CPU。第 3 節的後端啟動與下列 serve 步驟已整合在腳本 [bring-up.sh](../../deploy/recipes/strix-halo-poc/bring-up.sh) 中，可直接 `bash deploy/recipes/strix-halo-poc/bring-up.sh` 一次完成。

Serve the router with [deploy/recipes/strix-halo-poc/poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml) on the amd platform, setting `VLLM_SR_AMD_PRESERVE_CPU=1` to keep the built-in classifiers on CPU. The backend bring-up from section 3 and the serve step below are bundled in [bring-up.sh](../../deploy/recipes/strix-halo-poc/bring-up.sh), so you can run `bash deploy/recipes/strix-halo-poc/bring-up.sh` to do it all at once.

```bash
export VLLM_SR_AMD_PRESERVE_CPU=1

vllm-sr serve \
  --config deploy/recipes/strix-halo-poc/poc-strix.yaml \
  --image-pull-policy never \
  --platform amd
```

為何 `VLLM_SR_AMD_PRESERVE_CPU=1` / Why `VLLM_SR_AMD_PRESERVE_CPU=1`：`--platform amd` 預設會把 classifier modules 從 CPU 翻成 GPU；本 PoC 要把 iGPU 全留給 LLM 後端，故設此旗標讓 mmBERT/embedding 分類器留在 CPU（見 [runtime_config_mutation.py](../../src/vllm-sr/cli/commands/runtime_config_mutation.py)）。

Why `VLLM_SR_AMD_PRESERVE_CPU=1`: `--platform amd` by default flips classifier modules from CPU to GPU; this PoC reserves the iGPU entirely for the LLM backends, so the flag keeps the mmBERT/embedding classifiers on CPU (see [runtime_config_mutation.py](../../src/vllm-sr/cli/commands/runtime_config_mutation.py)).

驗證項目 / What to check：

| 檢查 / Check | 方法 / How |
| --- | --- |
| 容器狀態 / container status | `vllm-sr status` |
| Dashboard | 瀏覽 `http://<host>:8700` / browse `http://<host>:8700` |
| Metrics | `curl -s http://<host>:9190/metrics` |
| 端到端請求 / end-to-end request | 對 listener `:8899` 送一個 OpenAI 相容請求（見下）/ send an OpenAI-compatible request to listener `:8899` (below) |

預設埠口 / Default ports：listener `:8899`、api `:8080`、gRPC `:50051`、dashboard `:8700`、metrics `:9190`、Envoy admin `:9901`（見 [consts.py](../../src/vllm-sr/cli/consts.py)）。

Default ports: listener `:8899`, api `:8080`, gRPC `:50051`, dashboard `:8700`, metrics `:9190`, Envoy admin `:9901` (see [consts.py](../../src/vllm-sr/cli/consts.py)).

端到端冒煙測試 / End-to-end smoke test（單一請求如下；4 個示範請求（簡單／推理／PII／jailbreak）可用 [smoke_test.py](../../deploy/recipes/strix-halo-poc/smoke_test.py)：`python deploy/recipes/strix-halo-poc/smoke_test.py`）/ (single request below; for all 4 demo requests (easy / reasoning / PII / jailbreak) use [smoke_test.py](../../deploy/recipes/strix-halo-poc/smoke_test.py): `python deploy/recipes/strix-halo-poc/smoke_test.py`)：

```bash
curl -s http://localhost:8899/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
        "model": "auto",
        "messages": [{"role": "user", "content": "What is the capital of France?"}]
      }' | head
```

---

## 8. 驗證與校準 / Validate and Calibrate

先做本地靜態驗證（無需 GPU／router 即可在任何機器上跑），再對 PoC 的 DSL 做 Go 驗證，最後對運作中的 router 跑校準迴圈。

Run the offline static validation first (no GPU/router needed; runs on any box), then the Go DSL validation for the PoC DSL, and finally the calibration loop against the live router.

```bash
# 0. 離線結構驗證（純 PyYAML）/ offline structural validation (pure PyYAML)
python deploy/recipes/strix-halo-poc/validate_poc_config.py \
  deploy/recipes/strix-halo-poc/poc-strix.yaml

# 1. 由 poc-strix.yaml 產生並驗證 poc-strix.dsl（需要 Go）/ generate and validate
#    poc-strix.dsl from poc-strix.yaml (requires Go)
bash deploy/recipes/strix-halo-poc/gen-dsl.sh
```

`gen-dsl.sh` 會從 `src/semantic-router` 執行 `go run ./cmd/dsl decompile` 由 `poc-strix.yaml` 產生 `poc-strix.dsl`，再執行 `go run ./cmd/dsl validate` 驗證它。`.dsl` 是產生物，不入庫（在有 Go 的 Strix Halo 上即時產生）。DSL 只編碼 `routing.*`，providers/global 仍留在 YAML。DSL CLI 進入點見 [cmd/dsl/main.go](../../src/semantic-router/cmd/dsl/main.go)，腳本見 [gen-dsl.sh](../../deploy/recipes/strix-halo-poc/gen-dsl.sh)。

`gen-dsl.sh` runs `go run ./cmd/dsl decompile` from `src/semantic-router` to generate `poc-strix.dsl` from `poc-strix.yaml`, then `go run ./cmd/dsl validate` to validate it. The `.dsl` is a generated artifact and is not committed (it is produced on the Strix Halo where Go exists). The DSL encodes only `routing.*`; providers/global stay in YAML. The DSL CLI entry is [cmd/dsl/main.go](../../src/semantic-router/cmd/dsl/main.go), and the script is [gen-dsl.sh](../../deploy/recipes/strix-halo-poc/gen-dsl.sh).

If you also maintain a PoC-specific `.dsl`, replace the path above with `poc-strix.dsl`. The DSL CLI entry is [cmd/dsl/main.go](../../src/semantic-router/cmd/dsl/main.go).

路由校準迴圈 / Routing calibration loop：

```bash
python3 tools/agent/scripts/router_calibration_loop.py run \
  --router-url http://<host>:8080 \
  --probes deploy/recipes/strix-halo-poc/poc-probes.yaml \
  --yaml deploy/recipes/strix-halo-poc/poc-strix.yaml \
  --dsl deploy/recipes/strix-halo-poc/poc-strix.dsl
```

腳本見 [router_calibration_loop.py](../../tools/agent/scripts/router_calibration_loop.py)；probe 套件 [poc-probes.yaml](../../deploy/recipes/strix-halo-poc/poc-probes.yaml)（balance 的 13 條決策 probe 加上 PoC 的 `security_guard` 安全 probe）。若你改動了決策，請相應調整 probe 期望。

The script is [router_calibration_loop.py](../../tools/agent/scripts/router_calibration_loop.py); the probe suite [poc-probes.yaml](../../deploy/recipes/strix-halo-poc/poc-probes.yaml) is the balance 13-decision probes plus the PoC `security_guard` security probes. If you changed the decisions, adjust the probe expectations accordingly.

---

## 9. 量測與 demo / Measure and Demo

依 [02-poc-plan.md](02-poc-plan.md) 第 8 節的 demo 腳本，送出代表性請求並在 dashboard/Grafana 上展示。

Follow the demo script in section 8 of [02-poc-plan.md](02-poc-plan.md): send representative requests and show them on the dashboard/Grafana.

| 步驟 / Step | 送出 / Send | 預期觀察 / Expected observation |
| --- | --- | --- |
| 1 | 簡單問答 / an easy question | 路由到 SIMPLE 本地模型、成本 ~$0 / routes to the SIMPLE local model at ~$0 |
| 2 | 困難推理問題 / a hard reasoning question | 升級到 COMPLEX/REASONING/PREMIUM、開啟 reasoning / escalates to COMPLEX/REASONING/PREMIUM with reasoning on |
| 3 | 含 PII 的請求 / a request with PII | 命中 `contains_pii` 訊號，路由到 `security_guard`，由 fast_response 即時拒絕：HTTP 200 + `x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`（可另觀察 `x-vsr-matched-pii`）/ matches the `contains_pii` signal, routes to `security_guard`, and fast_response refuses immediately: HTTP 200 + `x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard` (optionally also `x-vsr-matched-pii`) |
| 4 | jailbreak 嘗試 / a jailbreak attempt | 命中 `jailbreak_attempt` 訊號，路由到 `security_guard`，由 fast_response 即時拒絕（HTTP 200 + `x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`，可另觀察 `x-vsr-matched-jailbreak`）；若仍打到模型，第二層 `response_jailbreak` 對被標記的輸出回 HTTP 403 / matches the `jailbreak_attempt` signal, routes to `security_guard`, and fast_response refuses immediately (HTTP 200 + `x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`, optionally also `x-vsr-matched-jailbreak`); if a model is still hit, the `response_jailbreak` second layer returns HTTP 403 on the flagged output |
| 5 | 開啟 Grafana / open Grafana | 成本下降數字、本地承載率、token 用量、TTFT/TPOT、快取命中 / cost-reduction number, local-served ratio, token usage, TTFT/TPOT, cache hit |
| 6（選配 / optional）| 跑 calibration loop / run the calibration loop | 路由準確率報表 / a routing-accuracy report |

> 安全攔截的真實機制 / How security blocking actually works：在此訊號驅動的 router 中，`pii` 與 `jailbreak` 是**訊號**，只負責把請求路由到 `security_guard` 決策，本身不會攔截。輸入端的攔截來自該決策上的 `fast_response` plugin（回 HTTP 200 + 制式拒絕訊息 + `x-vsr-fast-response: true`）；`response_jailbreak` 是第二層，只在 LLM **輸出**被標記時回 HTTP 403。路由路徑中**沒有**內聯 PII 遮罩，遮罩只在 `/api` 分類服務提供。
> How security blocking actually works: in this signal-driven router, `pii` and `jailbreak` are signals that only route a request to the `security_guard` decision; they do not block by themselves. The input-side block comes from the `fast_response` plugin on that decision (HTTP 200 + a canned refusal + `x-vsr-fast-response: true`), and `response_jailbreak` is the second layer that returns HTTP 403 only when the LLM output is flagged. There is no inline PII masking in the routing path; masking is available only via the `/api` classification service.

成本節省的來源 / Where savings come from：即使全部都在本地服務，dashboard 仍以設定檔的 `pricing` 對比「全部走最貴模型」基準計算省錢數字。完全離線示範 frontier 升級時，可用 mock 伺服器 `llm-katan` 取代真實雲端 API（見 [e2e/testing/llm-katan/README.md](../../e2e/testing/llm-katan/README.md)）。

Where savings come from: even when everything is served locally, the dashboard computes savings from the config `pricing` against an all-most-expensive-model baseline. For a fully offline demo of frontier escalation, replace the real cloud API with the mock server `llm-katan` (see [e2e/testing/llm-katan/README.md](../../e2e/testing/llm-katan/README.md)).

---

## 10. 驗收清單 / Acceptance Checklist

對應 [02-poc-plan.md](02-poc-plan.md) 第 1 節的可量測成功標準。

Mapped to the measurable success criteria in section 1 of [02-poc-plan.md](02-poc-plan.md).

| 驗收項目 / Acceptance item | 通過條件 / Pass condition | 證據來源 / Evidence source |
| --- | --- | --- |
| Token 成本下降 / token cost reduction | 相對全 frontier 基準下降 50%–80% / 50%–80% vs an all-frontier baseline | dashboard cost savings + Grafana |
| 本地承載率 / local-served ratio | 60%–80% 由本地 tier 服務 / 60%–80% served by local tiers | model distribution 指標 / metric |
| 路由準確率 / routing accuracy | 標註 probe 集 >= 90% / >= 90% on the labeled probe set | calibration loop + [balance.probes.yaml](../../deploy/recipes/balance.probes.yaml) |
| 安全攔截 / security blocking | PII + jailbreak 可即時示範 / live PII + jailbreak block | 輸入端 fast_response（`x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`）+ 輸出端 `response_jailbreak` 回 HTTP 403 / input-side fast_response (`x-vsr-fast-response: true` + `x-vsr-selected-decision: security_guard`) + output-side `response_jailbreak` HTTP 403 |
| 延遲 / latency | 本地 tier TTFT/TPOT 可接受、路由額外開銷低 / acceptable local-tier TTFT/TPOT, low routing overhead | Grafana P95 面板 |
| 品質維持 / quality retention | 困難請求不低於全 frontier 基準 / hard requests match the all-frontier baseline | 抽樣人評 + probe 期望 / sampled human eval + probe expectations |

驗收完成 / Done when：能在 dashboard 上同時展示「成本下降數字」「路由分佈」「安全攔截」三個畫面，並用標註 probe 集證明路由準確率。完成後，這個單機變體即達成 [02-poc-plan.md](02-poc-plan.md) 第 6 節的 **Phase 0** 目標，可進入多硬體階段。

Done when: the dashboard can simultaneously show the cost-reduction number, the routing distribution, and security blocking, and the labeled probe set proves routing accuracy. At that point this single-box variant satisfies the **Phase 0** goal in section 6 of [02-poc-plan.md](02-poc-plan.md), and you can move on to the multi-hardware phases.

---

## 11. 疑難排解 / Troubleshooting

| 症狀 / Symptom | 可能原因與處理 / Likely cause and fix |
| --- | --- |
| ROCm / gfx1151 未偵測到 / ROCm or gfx1151 not detected | 確認 `rocminfo` 看得到 gfx1151；Ollama 容器可試設 `HSA_OVERRIDE_GFX_VERSION`；確認使用者在 `video`/`render` 群組且 `/dev/kfd`、`/dev/dri` 存在 / confirm `rocminfo` shows gfx1151; try `HSA_OVERRIDE_GFX_VERSION` on the Ollama container; ensure the user is in `video`/`render` and `/dev/kfd`, `/dev/dri` exist |
| 統一記憶體 OOM / unified-memory OOM | 多個後端共用 128GB GTT；減少 tier、用更小或量化更激進的模型，或讓 Ollama 依需求載入／卸載（`OLLAMA_MAX_LOADED_MODELS`、`OLLAMA_KEEP_ALIVE`）/ backends share the 128GB GTT; reduce tiers, use smaller or more quantized models, or let Ollama load/unload on demand |
| router 容器連不到後端 / router container cannot reach the backend | 確認後端容器與 router 同在 `vllm-sr-network`、`endpoint` 用「容器名稱:埠號」（`ollama:11434`）；後端跑在主機時改用 `host.docker.internal:<port>`（router/Envoy 容器已自動加 host-gateway，見 [docker_run_command.py](../../src/vllm-sr/cli/docker_run_command.py)）/ ensure the backend container shares `vllm-sr-network` with the router and `endpoint` uses `container-name:port` (`ollama:11434`); for a host-run backend use `host.docker.internal:<port>` (router/Envoy containers add the host-gateway automatically) |
| OpenAI 路徑不符 / OpenAI path mismatch | 預設 `chat_path` 為 `/v1/chat/completions`；若後端用非標準路徑，於該 `backend_refs` 設 `chat_path` 覆寫（見 [models.py](../../src/vllm-sr/cli/models.py)）/ default `chat_path` is `/v1/chat/completions`; if a backend uses a non-standard path, override `chat_path` on that `backend_refs` (see [models.py](../../src/vllm-sr/cli/models.py)) |
| 分類器搶走 GPU / classifiers compete for the GPU | 設 `VLLM_SR_AMD_PRESERVE_CPU=1` 讓 mmBERT/embedding 分類器留在 CPU（見 [runtime_config_mutation.py](../../src/vllm-sr/cli/commands/runtime_config_mutation.py)）/ set `VLLM_SR_AMD_PRESERVE_CPU=1` to keep the mmBERT/embedding classifiers on CPU |
| `provider_model_id` 後端找不到模型 / backend reports unknown model | `provider_model_id` 必須等於 Ollama 標籤；用 `ollama list` 或 `docker exec ollama ollama list` 確認已下載 / `provider_model_id` must equal the Ollama tag; confirm it is pulled with `ollama list` or `docker exec ollama ollama list` |
| 設定驗證失敗 / config validation fails | 跑 `go run ./cmd/dsl validate` 找出錯誤；確認 `modelRefs[].model` 都對得上 `providers.models[].name` / run `go run ./cmd/dsl validate` to surface errors; ensure every `modelRefs[].model` matches a `providers.models[].name` |

---

## 參考連結 / Reference Links

- PoC 執行計畫 / PoC plan: [02-poc-plan.md](02-poc-plan.md)
- 技術研究 / Technology study: [01-tech-study.md](01-tech-study.md)
- AMD 參考 playbook / AMD reference playbook: [deploy/amd/README.md](../../deploy/amd/README.md)
- 參考路由設定 / reference routing profile: [deploy/recipes/balance.yaml](../../deploy/recipes/balance.yaml)
- Ollama: https://ollama.com
- AMD Lemonade Server: https://lemonade-server.ai
- llama.cpp: https://github.com/ggml-org/llama.cpp
- 文件網站 / docs site: https://vllm-semantic-router.com
