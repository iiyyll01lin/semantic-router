# 主從拓樸與路由器放哪 / Client-Server Topology and Where the Router Lives

> 一句話開場：很多人會問「router 該放在 client 還是 server？」——這問題其實問錯了。router（ExtProc）+ Envoy **合起來就是 LLM Gateway 那一層**，它們必須**同機共置**（走本地 gRPC）；模型只是 `backend_refs[].endpoint` 上的後端。所以真正要決定的不是「client 還是 server」，而是「閘道放在哪條流量路徑上最省往返」——而答案跟模型放哪**無關**，跟流量在地性有關。
> One-line opener: people ask "should the router sit on the client or the server?"—but that framing is wrong. The router (ExtProc) plus Envoy **together are the LLM Gateway tier**, and they must be **co-located** (local gRPC); models are just backends behind `backend_refs[].endpoint`. So the real decision is not "client vs server" but "on which traffic path does placing the gateway minimize round-trips"—and the answer has **nothing** to do with where the models live and everything to do with traffic locality.

本文件接續既有報告系列（[01-tech-study.md](01-tech-study.md)、[02-poc-plan.md](02-poc-plan.md)、[03-strix-halo-runbook.md](03-strix-halo-runbook.md)、[04-dashboard-tour.md](04-dashboard-tour.md)、[05-amd-strategy-alignment.md](05-amd-strategy-alignment.md)、[06-multi-node-and-operator.md](06-multi-node-and-operator.md)）。[06-multi-node-and-operator.md](06-multi-node-and-operator.md) 講的是「同一份路由設定怎麼**垂直疊副本**」；本文講的是它的另一個維度——「在 client/server 兩端之間，閘道**水平放哪**才不會多繞一圈」，並給出 2 台 Strix Halo PoC 的具體推薦拓樸。

This document continues the existing report series ([01-tech-study.md](01-tech-study.md), [02-poc-plan.md](02-poc-plan.md), [03-strix-halo-runbook.md](03-strix-halo-runbook.md), [04-dashboard-tour.md](04-dashboard-tour.md), [05-amd-strategy-alignment.md](05-amd-strategy-alignment.md), [06-multi-node-and-operator.md](06-multi-node-and-operator.md)). Where [06-multi-node-and-operator.md](06-multi-node-and-operator.md) covers how that same routing config **stacks replicas vertically**, this document covers the orthogonal dimension—**where to place the gateway horizontally** between the client and server ends so traffic does not take an extra round-trip—and gives the concrete recommended topology for the 2-box Strix Halo PoC.

---

## 1. 重新定義「client vs server」：router + Envoy 就是閘道層 / Reframing "client vs server": Router plus Envoy Are the Gateway Tier

第一個要打掉的誤解：router 不是「黏在某個模型旁邊的東西」。如 [01-tech-study.md](01-tech-study.md) 第 2 節所述，router **不自己挑上游 endpoint**——它只決定「該用哪個 model」、改寫請求、回傳 mutations，真正把流量負載平衡到實際 endpoint 的是 **Envoy**（見 [processor_req_body_routing.go](../../src/semantic-router/pkg/extproc/processor_req_body_routing.go) 的 `createRoutingResponse`，它只產出 body 與 model/provider header 的 mutations，不開後端連線）。

The first misconception to kill: the router is not "something glued next to a model." As section 2 of [01-tech-study.md](01-tech-study.md) explains, the router **does not pick an upstream endpoint itself**—it only decides which model to use, rewrites the request, and returns mutations; **Envoy** is what load-balances traffic to the actual endpoint (see `createRoutingResponse` in [processor_req_body_routing.go](../../src/semantic-router/pkg/extproc/processor_req_body_routing.go), which emits only body and model/provider-header mutations and opens no backend connections).

因此 router（ExtProc）與 Envoy 是**同一個閘道層的兩半**：Envoy 在資料面攔流量，router 在控制面做語意決策，兩者透過 ExtProc gRPC 緊密耦合，必須**共置在同一台機器**（本地 gRPC，毫秒內、零網路成本）。這正是 [05-amd-strategy-alignment.md](05-amd-strategy-alignment.md) 把「Envoy + ExtProc router」整體對映成 AMD 簡報 `LLM Gateway` 的原因——閘道是一個**層**，不是一個放在某端的盒子。

So the router (ExtProc) and Envoy are **two halves of one gateway tier**: Envoy intercepts traffic on the data plane, the router makes semantic decisions on the control plane, and the two are tightly coupled over ExtProc gRPC and must be **co-located on the same box** (local gRPC, sub-millisecond, zero network cost). This is exactly why [05-amd-strategy-alignment.md](05-amd-strategy-alignment.md) maps "Envoy + the ExtProc router" as a whole onto the AMD deck's `LLM Gateway`—the gateway is a **tier**, not a box pinned to one end.

模型在哪？模型是**後端**，以 `providers.models[].backend_refs[].endpoint` 註冊（見 [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml)，例如 `endpoint: ollama:11434`）。一個後端可以在閘道本機，也可以在網路另一端的一台 `host:port`。關鍵結論：

Where are the models? Models are **backends**, registered as `providers.models[].backend_refs[].endpoint` (see [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml), e.g. `endpoint: ollama:11434`). A backend can be on the gateway's own box or a `host:port` across the network. The key conclusion:

> 閘道的放置位置由**流量在地性**決定，不是由**模型位置**決定。把閘道放在「請求進入、且大部分流量會就地被服務」的那一端，網路往返最少。
> Gateway placement follows **traffic locality**, not **model location**. Put the gateway where requests enter and where most traffic is served locally, and network round-trips are minimized.

---

## 2. 兩平面模型：控制面共置、資料面才需要優化 / The Two-plane Model: Co-locate the Control Plane, Optimize the Data Plane

把閘道內外的連線拆成兩個平面，放置決策就會變得非常清楚。

Split the connections in and around the gateway into two planes, and the placement decision becomes obvious.

| 平面 / Plane | 連線 / Link | 載荷 / Payload | 大小與位置 / Size and location |
| --- | --- | --- | --- |
| 控制面 / Control plane | Envoy ↔ router（ExtProc gRPC）/ Envoy to router (ExtProc gRPC) | 路由決策、header/body mutations / routing decisions, header/body mutations | 極小、每請求多次往返；**永遠在閘道本機** / tiny, multiple round-trips per request; **always local to the gateway** |
| 資料面 / Data plane | Envoy → 後端 / Envoy to backend | 完整 prompt 與 completion（可能很大）/ full prompt and completion (can be large) | 大、攜帶實際 token；**放置就是在優化它** / large, carries the actual tokens; **placement is what optimizes this** |

控制面（Envoy↔router）每個請求要來回好幾次（header 階段、body 階段、response 階段），但載荷只是決策與 mutation，極小——前提是它**永遠是本地 gRPC**。一旦把 router 和 Envoy 分到兩台機器，這些高頻往返就會被網路放大，這也是為什麼第 1 節說它們必須共置。

The control plane (Envoy to router) makes several round-trips per request (header phase, body phase, response phase), but the payload is just decisions and mutations—tiny, **provided it is always local gRPC**. Split the router and Envoy onto two boxes and those high-frequency round-trips get amplified by the network, which is why section 1 insists they be co-located.

資料面（Envoy→後端）才是攜帶實際 prompt/completion token 的那條線，也是唯一隨內容大小變動的成本。因此「閘道放哪」這個決策的全部意義，就是**讓資料面的網路跳數最少**：常規流量若能在閘道本機就被服務，那條最粗的線就根本不出機器。

The data plane (Envoy to backend) is the line that carries the actual prompt/completion tokens and the only cost that scales with content size. So the entire point of "where to place the gateway" is to **minimize the data plane's network hops**: if routine traffic can be served on the gateway's own box, the thickest line never leaves the machine.

---

## 3. 雙跳分析：一個反模式、兩個無雙跳設計 / Double-hop Analysis: One Anti-pattern, Two Double-hop-free Designs

有了兩平面模型，就能把所有拓樸的優劣化約成一個問題：**常規請求要跨網路幾次？**

With the two-plane model, the quality of any topology reduces to one question: **how many network crossings does a routine request take?**

### 3.1 反模式：閘道在 server、模型卻又住在它要回頭路由的 client / The anti-pattern: gateway on the server while models also live on the client it routes back to

唯一真正該避免的拓樸：把閘道放在 server，但**被閘道路由回去的目標模型卻住在 client 端**。請求從 client 出發 → 跨網路到 server 的閘道 → 閘道決定要用 client 上的小模型 → 再跨網路回 client 服務 → 結果再回到 client。這是 **client→gateway→client 的雙重來回**：一個本來該在原地解決的常規請求，平白付了兩趟網路。

The one topology to truly avoid: put the gateway on the server, but **the target models that the gateway routes back to live on the client side**. A request leaves the client, crosses the network to the gateway on the server, the gateway decides to use a small model that lives on the client, then crosses the network back to the client to be served, and the result returns to the client again. This is a **client-to-gateway-to-client double round-trip**: a routine request that should have been solved in place pays for two network trips for nothing.

### 3.2 設計 A（推薦）：邊緣閘道 / Design A (recommended): edge-gateway

閘道放在 client/edge 端，小模型與閘道**共置在本機**；大模型放在 server，frontier 上雲。常規請求（占大多數）由本機小模型服務——**0 個網路跳數**；只有被升級的困難請求才跨網路去 server 或雲端。這是「Ryzen AI Max+ 邊緣 + Instinct 資料中心」故事的忠實落地。

The gateway sits on the client/edge end, with the small models **co-located on the same box**; big models live on the server and frontier goes to cloud. Routine requests (the majority) are served by the local small model—**zero network hops**; only escalated hard requests cross the network to the server or cloud. This is a faithful realization of the "Ryzen AI Max+ edge plus Instinct datacenter" story.

### 3.3 設計 B：集中式 server 閘道 / Design B: centralized server-gateway

閘道放在 server，而 client **完全不放任何模型**（純粹是請求來源 / origin）。每個常規請求都是 client→server 閘道→server 本機後端，**每請求 1 跳**。因為 client 端沒有模型可被「路由回去」，所以**不會**有 3.1 的雙重來回。當你想要集中治理、所有模型都在資料中心、client 只是瘦端點時，這是正確選擇。

The gateway sits on the server, and the client **hosts no models at all** (it is a pure request origin). Every routine request is client to server-gateway to a server-local backend—**one hop per request**. Because the client has no models to be "routed back to," there is **no** double round-trip from section 3.1. This is the right choice when you want centralized governance, all models in the datacenter, and the client as a thin endpoint.

| 拓樸 / Topology | 常規請求網路跳數 / Routine-request hops | 何時採用 / When to use |
| --- | --- | --- |
| 反模式（server 閘道 + client 端模型）/ Anti-pattern (server-gateway + client-side models) | 2（雙重來回）/ 2 (double round-trip) | 永不 / Never |
| 設計 A：邊緣閘道 / Design A: edge-gateway | 0（本機服務）/ 0 (served locally) | client/edge 端就有算力、想把常規 token 留在本地 / the client/edge has compute and you want routine tokens local |
| 設計 B：集中式 server 閘道 / Design B: centralized server-gateway | 1（每請求 1 跳）/ 1 (one hop per request) | client 是瘦端點、所有模型集中在資料中心 / the client is a thin endpoint and all models live in the datacenter |

---

## 4. 2 台 Strix Halo PoC 的推薦：邊緣閘道設計 / Recommendation for the 2-Strix-Halo PoC: the Edge-gateway Design

對手上這套 2 台 Strix Halo 的 PoC，採用**設計 A（邊緣閘道）**：

For the 2-box Strix Halo PoC, adopt **Design A (edge-gateway)**:

- **Halo-A = client/edge（閘道）** — 跑 router（`vllm-sr serve`）+ Envoy，並**共置小模型**（如 `llama3.2:3b`、`qwen2.5:7b`）於本機 Ollama。常規 token 永遠不出機器。
  **Halo-A = client/edge (gateway)** — runs the router (`vllm-sr serve`) plus Envoy and **co-locates the small models** (e.g. `llama3.2:3b`, `qwen2.5:7b`) in a local Ollama. Routine tokens never leave the box.
- **Halo-B = 扮演 Instinct 資料中心** — 只跑一個純 Ollama endpoint（`0.0.0.0:11434`）服務**大模型**（如 `qwen2.5:14b`、`qwen3:14b`、`qwen2.5:32b`），**完全沒有 router**。它就是設計 A 裡那個「被升級請求才會打到」的後端。
  **Halo-B = playing the Instinct datacenter** — runs only a plain Ollama endpoint (`0.0.0.0:11434`) serving the **big models** (e.g. `qwen2.5:14b`, `qwen3:14b`, `qwen2.5:32b`) with **no router at all**. It is simply the backend that escalated requests hit in Design A.

在 [poc-client-edge.yaml](../../deploy/recipes/strix-halo-2box/poc-client-edge.yaml) 裡，這對映成把 `backend_refs[].endpoint` 拆兩半：邊緣層模型指向本機 `ollama:11434`，資料中心層模型指向 `${HALO_B_IP}:11434`；listener 綁 `0.0.0.0:8899` 讓 app 能從網路打進閘道。這份設定與單機版 [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml) 共用同一套 `routing.decisions`，差別只在後端位址。

In [poc-client-edge.yaml](../../deploy/recipes/strix-halo-2box/poc-client-edge.yaml) this maps to splitting `backend_refs[].endpoint` into two halves: edge-tier models point at the local `ollama:11434`, datacenter-tier models point at `${HALO_B_IP}:11434`; the listener binds `0.0.0.0:8899` so the app can reach the gateway over the network. This config reuses the very same `routing.decisions` as the single-box [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml); only the backend addresses differ.

frontier/premium 那一條（`anthropic/claude-opus-4.6`)現在接到**真實的 Anthropic 公有 API**（`api_format: anthropic` + `base_url: https://api.anthropic.com`),不再是本地 `llm-katan` mock。Envoy 會自動生成共用的 `anthropic_api_cluster`（`api.anthropic.com:443` + TLS),routing 完全沿用——`premium_legal` decision 仍把 legal/high-risk 導向同一個 model 名稱。這形成「本地小模型(Halo-A)+ 本地大模型(Halo-B)+ 雲端 frontier」的混合部署;唯一新增的需求是 Halo-A 要有 `ANTHROPIC_API_KEY` 與對外 443 egress（見第 5 節）。

The frontier/premium branch (`anthropic/claude-opus-4.6`) now points at the **real Anthropic public API** (`api_format: anthropic` + `base_url: https://api.anthropic.com`), no longer the local `llm-katan` mock. Envoy auto-generates the shared `anthropic_api_cluster` (`api.anthropic.com:443` + TLS), and routing is reused verbatim—the `premium_legal` decision still steers legal/high-risk traffic to the same model name. This yields a hybrid deployment of "local small models (Halo-A) + local big models (Halo-B) + cloud frontier"; the only new requirement is that Halo-A has `ANTHROPIC_API_KEY` and outbound 443 egress (see section 5).

對映回 AMD 簡報（見 [05-amd-strategy-alignment.md](05-amd-strategy-alignment.md)）：Halo-A 的閘道**就是** `LLM Gateway` 與 `Intelligent Token Routing`——`Local Tokens → 本地小模型`（Halo-A 本機，0 跳）、`Premium Tokens → Frontier`（上雲），而升級到 Halo-B 大模型對映 `Local Tokens → MI350P AMD Servers`。整張分流圖原封不動地落在這兩台盒子上。

Mapping back to the AMD deck (see [05-amd-strategy-alignment.md](05-amd-strategy-alignment.md)): the gateway on Halo-A **is** the `LLM Gateway` and `Intelligent Token Routing`—`Local Tokens → local small models` (on Halo-A, 0 hops), `Premium Tokens → Frontier` (to cloud), while escalation to Halo-B's big models maps to `Local Tokens → MI350P AMD Servers`. The entire routing diagram lands unchanged on these two boxes.

### 推薦邊緣閘道拓樸圖 / Recommended edge-gateway topology

```mermaid
flowchart TD
    App["OpenAI-compatible app"] -->|"http to 0.0.0.0:8899"| Edge

    subgraph Edge ["Halo-A: client / edge (gateway tier)"]
        Envoy["Envoy proxy (listener 0.0.0.0:8899)"]
        Router["vllm-sr router (ExtProc)"]
        SmallModels["Local Ollama 11434: small models (llama3.2:3b, qwen2.5:7b)"]
        Envoy -->|"ExtProc gRPC (local, control plane)"| Router
        Router -->|"model + mutations"| Envoy
        Envoy -->|"routine (0 network hops)"| SmallModels
    end

    subgraph Datacenter ["Halo-B: plays Instinct datacenter (no router)"]
        BigModels["Plain Ollama 0.0.0.0:11434: big models (qwen2.5:14b, qwen3:14b, qwen2.5:32b)"]
    end

    Cloud["Anthropic public API (api.anthropic.com:443, claude-opus-4.6)"]

    Envoy -->|"escalated hard request to HALO_B_IP:11434"| BigModels
    Envoy -->|"premium / frontier (HTTPS + ANTHROPIC_API_KEY)"| Cloud
```

---

## 5. 誠實邊界與網路事實 / Honest Caveats and Networking Facts

延續整個系列的誠實切分（[02-poc-plan.md](02-poc-plan.md) 第 12 節、[05-amd-strategy-alignment.md](05-amd-strategy-alignment.md) 第 4 節、[06-multi-node-and-operator.md](06-multi-node-and-operator.md) 第 3 節），這裡明確標出本 PoC **不**證明什麼。

Continuing the series' honest split (section 12 of [02-poc-plan.md](02-poc-plan.md), section 4 of [05-amd-strategy-alignment.md](05-amd-strategy-alignment.md), section 3 of [06-multi-node-and-operator.md](06-multi-node-and-operator.md)), here is what this PoC explicitly does **not** prove.

- **Halo-B 不是真的 Instinct / Halo-B is not a real Instinct** — 兩台都是 gfx1151 APU（Ryzen AI Max+ 等級的 Strix Halo），Halo-B 只是**扮演**資料中心角色。因此本 PoC**不**提出任何效能、吞吐或 TCO 主張；它純粹是**拓樸／路由／成本記帳**的驗證。
  Both boxes are gfx1151 APUs (Strix Halo at the Ryzen AI Max+ class); Halo-B merely **plays** the datacenter role. So this PoC makes **no** performance, throughput, or TCO claims; it is purely a **topology / routing / cost-accounting** validation.
- **真實 Instinct 效能／TCO 用 fleet-sim 外推 / Real Instinct perf and TCO come from fleet-sim** — 跨盒子的聚合吞吐與機群成本仍走 [02-poc-plan.md](02-poc-plan.md) 第 12 節的「先量測再模擬」：單機量測 router 開銷與每模型 profile，再用 router-replay → fleet-sim 外推 N 節點機群，並永遠標註哪些是量測、哪些是外推。本文證明的是**路由會正確跨盒子發生**，不是它在真 Instinct 上有多快。
  Cross-box aggregate throughput and fleet cost still go through the "measure-then-simulate" flow of section 12 in [02-poc-plan.md](02-poc-plan.md): measure router overhead and per-model profiles on one box, then extrapolate an N-node fleet via router-replay → fleet-sim, always labeling measured versus extrapolated. This document proves that **routing happens correctly across the boxes**, not how fast it runs on a real Instinct.

網路事實（會讓設定真的能跨盒子跑起來的細節）/ Networking facts (the details that make cross-box routing actually work):

- **`endpoint` 必須是可路由的 IP/host，不能是 docker-network 的 DNS 名 `ollama` / `endpoint` must be a routable IP/host, not the docker-network DNS name `ollama`** — `ollama` 這個名字只在**同一台機器**的 docker 網路內可解析。要指向 Halo-B，必須寫成 `endpoint: <HALO_B_IP>:11434`（一個跨機器可路由的位址）。
  The name `ollama` only resolves inside the docker network **on the same box**. To point at Halo-B, write `endpoint: <HALO_B_IP>:11434` (an address routable across machines).
- **Listener 綁 `0.0.0.0:8899` / Listener binds `0.0.0.0:8899`** — 閘道要讓網路另一端的 app 連得進來，listener 位址必須是 `0.0.0.0`（見 [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml) 的 `listeners[].address: 0.0.0.0`），不能只綁 loopback。
  For the gateway to accept connections from an app on the other end of the network, the listener address must be `0.0.0.0` (see `listeners[].address: 0.0.0.0` in [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml)), not loopback only.
- **要開的埠 / Ports to open** — Halo-A 的閘道入口 `8899`（OpenAI 相容入站）、Halo-B 的 Ollama `11434`。請確認 Halo-A 容器內的 Envoy 真的能連到 `<HALO_B_IP>:11434`（主機路由／防火牆）。
  Port `8899` on Halo-A (the gateway's OpenAI-compatible inbound) and port `11434` on Halo-B (Ollama). Verify that Envoy inside the Halo-A container can actually reach `<HALO_B_IP>:11434` (host routing and firewall).
- **Halo-A 需對外 HTTPS egress 到 `api.anthropic.com:443` + 金鑰 / Halo-A needs outbound HTTPS egress to `api.anthropic.com:443` plus a key** — frontier/premium 那一層現在打真實 Anthropic 公有 API,所以 Halo-A 必須能對外連到 `api.anthropic.com:443`（防火牆／容器允許出向 HTTPS),並在 serve 前設好 `export ANTHROPIC_API_KEY=sk-ant-...`(`vllm-sr serve` 會自動帶進容器)。這只影響 Halo-A,完全不碰 Halo-B;缺金鑰時本地層仍可運作,只有 premium 請求會失敗。
  The frontier/premium tier now calls the real Anthropic public API, so Halo-A must be able to reach `api.anthropic.com:443` outbound (firewall/container allows egress HTTPS), with `export ANTHROPIC_API_KEY=sk-ant-...` set before serving (`vllm-sr serve` auto-injects it into the container). This affects Halo-A only, never Halo-B; without the key the local tiers still work and only premium requests fail.
- **金鑰只在 host env / router 注入,log 預設遮蔽,trace 不含 auth / The key lives only in host env and router injection; logs are redacted by default and traces carry no auth** — `ANTHROPIC_API_KEY` 只存在於 Halo-A 的 host 環境變數,由 router 在送往 Anthropic 時以 header mutation 注入 `x-api-key`(不寫進 Envoy 設定檔)。debug 層的請求/回應 dump 走深拷貝遮蔽(`x-api-key`/`authorization`/...→ `[REDACTED]`,見 [response_log_redaction.go](../../src/semantic-router/pkg/extproc/response_log_redaction.go)),分散式 tracing 只注入 W3C trace context、不帶任何憑證。
  `ANTHROPIC_API_KEY` exists only as a host environment variable on Halo-A; the router injects it as the `x-api-key` header via header mutation when forwarding to Anthropic (never written into the Envoy config). Debug-level request/response dumps are masked through a deep copy (`x-api-key`/`authorization`/... → `[REDACTED]`, see [response_log_redaction.go](../../src/semantic-router/pkg/extproc/response_log_redaction.go)), and distributed tracing injects only the W3C trace context, carrying no credentials.

---

## 6. 執行紀錄 / Run record (measured-on 2026-06-23)

> 一句話框架：本節把前面五節的拓樸主張**實測落地**——在兩台 gfx1151 Strix Halo APU 上以一行指令部起設計 A（邊緣閘道），量測跨盒子路由是否正確發生、agentic 流量的邊緣／資料中心分流，以及最關鍵的「跨盒子那一跳到底貴不貴」。所有數字明確標註**實測（measured）**或**推導（derived）**。
> One-line framing: this section **grounds the previous five sections' topology claims in measurement**—on two gfx1151 Strix Halo APUs, deploy Design A (edge-gateway) with a single command, then measure whether cross-box routing actually happens, the agentic traffic's edge/datacenter split, and the key question "how expensive is that extra cross-box hop." Every number is labeled **measured** or **derived**.

### 6.1 一行部署 + 冒煙測試 / One-command deploy plus smoke test

從 Halo-A 跑 [deploy-2box.sh](../../deploy/recipes/strix-halo-2box/deploy-2box.sh) 一行就把整套設計 A 拉起來。**實測**：router 在 14 秒內就緒，啟動日誌出現 `required_models_already_present total_models:6` 與 `pii_mapping_loaded count:35`——後者正是 PII 掛載修復（fix 提交 `ba3cd09f`）真的生效的證據。

Running [deploy-2box.sh](../../deploy/recipes/strix-halo-2box/deploy-2box.sh) once from Halo-A brings the whole Design A up. **Measured**: the router was ready in 14 s, and the startup log showed `required_models_already_present total_models:6` and `pii_mapping_loaded count:35`—the latter is the proof that the PII-mount fix (fix commit `ba3cd09f`) is actually working.

[smoke_test.py](../../deploy/recipes/strix-halo-2box/smoke_test.py) **實測**驗證跨盒子路由與安全攔截（其中 `qwen2.5:14b` 這個 response body 模型名就是「請求真的跨到 Halo-B」的鐵證）/ **Measured** cross-box routing and security blocking (the response-body model name `qwen2.5:14b` is the hard proof the request really crossed to Halo-B):

| 輸入 / Input | x-vsr-selected-model | 回應 body 模型 / Response body model | 決策 / Decision | 信心 / Confidence | 服務於 / Served on |
| --- | --- | --- | --- | --- | --- |
| 簡單事實 / Easy factual | `qwen/qwen3.5-rocm` | `llama3.2:3b` | `fast_qa` | 0.905 | Halo-A EDGE（0 跳 / 0 hops） |
| 困難推理 / Hard reasoning | `google/gemini-3.1-pro` | `qwen2.5:14b` | `reasoning_deep` | 0.902 | Halo-B DATACENTER（1 跳 / 1 hop） |
| PII 輸入 / PII input | — | — | `security_guard` | — | `fast_response` 攔截（`x-vsr-fast-response: true`）/ blocked by `fast_response` |
| Jailbreak 輸入 / Jailbreak input | — | — | `security_guard` | — | `fast_response` 攔截（`x-vsr-fast-response: true`）/ blocked by `fast_response` |

### 6.2 即時 agentic 壓測 / Live agentic benchmark

以 [agentic_routing_live_benchmark.py](../../bench/agentic_routing_live_benchmark.py) 打閘道 `:8899/v1`（`--model auto --scenario balanced --sessions 8 --turns 8 --concurrency 2`）。**實測**：64 個請求、success_rate 1.0（全部 200）、0 個 validation failure、x-vsr headers 全部有效（missing=0）——這證明流量真的打進 router，而不是 echo mock。

Using [agentic_routing_live_benchmark.py](../../bench/agentic_routing_live_benchmark.py) against the gateway `:8899/v1` (`--model auto --scenario balanced --sessions 8 --turns 8 --concurrency 2`). **Measured**: 64 requests, success_rate 1.0 (all 200), 0 validation failures, x-vsr headers all valid (missing=0)—this confirms traffic hit the router, not the echo mock.

| 指標 / Metric | 值 / Value（實測 / measured） |
| --- | --- |
| 請求數 / Requests | 64（success_rate 1.0） |
| 延遲 ms / Latency ms | mean 3989.55、p50 3818.42、p95 5331.02、p99 5404.17 |
| 吞吐 / Throughput | ~0.5 rps（wall 128 s） |
| 模型分流 / Model distribution | `google/gemini-3.1-pro` x48（資料中心 / datacenter, Halo-B）、`qwen/qwen3.5-rocm` x16（邊緣 / edge, Halo-A） |
| 邊緣／資料中心比 / Edge vs datacenter（**推導 / derived**） | Edge 16/64 = 25% ／ Datacenter 48/64 = 75% |
| Frontier-mock 路由與 422 / Frontier-mock routes and 422s | 0 ／ 0 |

注意：這些數秒級延遲是**被路由到的模型推論時間**，**不是**網路成本（網路那一跳的真實成本見 6.3）。

Note: these multi-second latencies are the **routed-model inference time**, **not** network cost (the real cost of the network hop is in 6.3).

### 6.3 網路跳數（雙跳）成本 / Network-hop (double-hop) cost

這是直接回答「server-side routing 會不會因為雙跳而很怪」的關鍵量測。把「純網路往返」與「聊天往返」分開量，才不會被模型大小污染結論。

This is the key measurement answering "is server-side routing weird because of double hops." Measuring "pure network round-trip" separately from "chat round-trip" keeps model size from contaminating the conclusion.

| 量測 / Measurement | 本機 Halo-A（0 跳）/ local (0 hops) | 遠端 Halo-B（1 跳）/ remote (1 hop) | 差值 / Delta | 註解 / Note |
| --- | --- | --- | --- | --- |
| 純網路往返（trivial `GET /api/tags`）/ Pure network round-trip | 0.397 ms（中位 / median） | 0.572 ms（中位 / median） | **+0.175 ms**（實測 / measured） | 多 1 跳，亞毫秒、可忽略 / one extra hop, sub-ms, negligible |
| 聊天往返（不同模型大小）/ Chat round-trip (different model sizes) | `llama3.2:3b` 170.42 ms | `qwen2.5:14b` 195.81 ms | +25.39 ms（實測 / measured） | 由**模型大小**驅動，**非**網路 / model-size driven, **not** network |

**結論（實測支撐的判斷）**：邊緣閘道設計讓常規流量維持 0 跳；即使升級請求跨到 Halo-B，多出的網路那一跳也只有 ~0.2 ms——所以本拓樸選擇被量化驗證，先前對「雙跳」的疑慮在這條直連 2-box LAN 上是**可量化地小**。誠實警語：此值會在 WAN 上放大，跨地域部署時這條延遲要重新量測。

**Conclusion (measurement-backed judgment)**: the edge-gateway design keeps routine traffic at 0 hops, and even when an escalated request crosses to Halo-B the extra network hop is only ~0.2 ms—so this topology choice is quantitatively validated, and the earlier double-hop concern is **quantifiably small** on this direct 2-box LAN. Honest caveat: this grows over a WAN, so for cross-region deployments this latency must be re-measured.

### 6.4 可觀測性 / Observability

**實測** `:9190` 指標快照（成本與 token 依模型歸戶，與 6.2 分流一致：流量集中在資料中心層 `google/gemini-3.1-pro`）/ **Measured** `:9190` metrics snapshot (cost and tokens attributed per model, consistent with the 6.2 split—traffic concentrated on the datacenter-tier `google/gemini-3.1-pro`):

```text
llm_model_cost_total{currency="USD",model="google/gemini-3.1-pro"} 0.007293120000000013
llm_model_cost_total{currency="USD",model="qwen/qwen3.5-rocm"} 0
llm_model_tokens_total{model="google/gemini-3.1-pro"} 7154
llm_model_tokens_total{model="qwen/qwen3.5-rocm"} 4869
```

儀表板截圖（`:8700` 的 Status／Monitoring／Tracing）本輪**未**擷取：dashboard 需登入，而本次部署未配置 admin 憑證。要看成本／分流圖與 Jaeger 上 `classify → decide → upstream` 的 span，請走這些路由（`/status`、`/monitoring`、`/tracing`）登入後檢視，作為 demo 的觀測入口。

Dashboard screenshots (Status/Monitoring/Tracing at `:8700`) were **not** captured this round: the dashboard requires login and no admin credentials were provisioned at deploy time. To view the cost/distribution charts and the Jaeger `classify → decide → upstream` span, log in and visit those routes (`/status`, `/monitoring`, `/tracing`) as the demo's observability entry point.

### 6.5 誠實邊界 / Honest boundary

延續第 5 節：本節是兩台 gfx1151 APU 上的**拓樸／路由／成本**證據，**不是** Instinct 效能數字。真實機群效能與 TCO 仍由「先量測再模擬」的 fleet-sim 外推補上（見第 5 節與 [02-poc-plan.md](02-poc-plan.md) 第 12 節）。

Continuing section 5: this section is **topology/routing/cost** evidence on two gfx1151 APUs, **not** Instinct performance numbers. Real fleet performance and TCO still come from the "measure-then-simulate" fleet-sim extrapolation (see section 5 and section 12 of [02-poc-plan.md](02-poc-plan.md)).

### 6.6 第二輪：儀表板管理員、多候選、fleet-sim / Second round: dashboard admin, multi-candidate, fleet-sim (measured-on 2026-06-23)

> 一句話框架：第一輪（6.1–6.5）留下三個缺口——儀表板無法登入、選擇演算法未實證、fleet-sim 尚未接真實 replay。本輪在同一對 gfx1151 盒子上補強這些缺口，並**誠實**標出哪些成立、哪些仍未證實。
> One-line framing: the first round (6.1–6.5) left three gaps—the dashboard could not be logged into, the selection algorithm was unproven, and fleet-sim was not yet fed by real replay. This round closes those on the same pair of gfx1151 boxes and **honestly** marks what holds versus what remains unproven.

#### 6.6.1 儀表板管理員已佈建 / Dashboard admin now provisioned

本輪透過新的 `DASHBOARD_ADMIN_*` 環境變數直通（加進 `vllm-sr serve` 的 passthrough allowlist），由 [client-bring-up.sh](../../deploy/recipes/strix-halo-2box/client-bring-up.sh) 在 serve 前佈建一組 demo 管理員 **admin@demo.local**（密碼隱藏，預設 `vllmsr-demo`，可用 `DASHBOARD_ADMIN_PASSWORD` 覆寫）。dashboard 的 `EnsureBootstrapAdmin` 具冪等性，對已 bootstrap 的資料庫重跑也安全，**不需清空 volume**。**實測**：登入後 `/status`、`/monitoring`、`/tracing` 三個觀測頁面現在皆可進入——第一輪「無法登入」的缺口已解除。

This round forwards the new `DASHBOARD_ADMIN_*` env vars (added to the `vllm-sr serve` passthrough allowlist), so [client-bring-up.sh](../../deploy/recipes/strix-halo-2box/client-bring-up.sh) provisions a demo admin **admin@demo.local** before serving (password hidden; default `vllmsr-demo`, override via `DASHBOARD_ADMIN_PASSWORD`). The dashboard's `EnsureBootstrapAdmin` is idempotent and safe to re-run against an already-bootstrapped database, so **no volume wipe is needed**. **Measured**: after login, the `/status`, `/monitoring`, and `/tracing` observability pages are now all reachable—the first round's "cannot log in" gap is resolved.

#### 6.6.2 多候選選擇 — 已知落差（未證實）/ Multi-candidate selection — KNOWN GAP (unproven)

本輪給 `reasoning_deep` decision 加了第二個候選（`google/gemini-2.5-flash-lite`，與既有的 `google/gemini-3.1-pro` 並列）並掛上 `algorithm: type: multi_factor`。**實測（成立的部分）**：啟動日誌出現 `Registered algorithm: multi_factor (tier=supported, dependencies=none)`，且 hard prompt 仍正確路由——困難推理 → `reasoning_deep` → `google/gemini-3.1-pro`（Halo-B 資料中心）；簡單事實 → `fast_qa` → `qwen/qwen3.5-rocm`（Halo-A 邊緣）。

This round gave the `reasoning_deep` decision a second candidate (`google/gemini-2.5-flash-lite` alongside the existing `google/gemini-3.1-pro`) plus an `algorithm: type: multi_factor` block. **Measured (what holds)**: the startup log shows `Registered algorithm: multi_factor (tier=supported, dependencies=none)`, and the hard prompt still routes correctly—hard reasoning → `reasoning_deep` → `google/gemini-3.1-pro` (Halo-B datacenter); easy factual → `fast_qa` → `qwen/qwen3.5-rocm` (Halo-A edge).

**但執行期遙測並未證實 multi_factor 真的執行了候選比較（誠實落差）/ But runtime telemetry did NOT confirm multi_factor actually executed a candidate comparison (honest gap)**：即使在 2–3 個命中 `reasoning_deep` 的 hard 請求後（`llm_decision_match_total{decision_name="reasoning_deep"}=2`），選擇計數器 `llm_model_selection_total{method="multi_factor"}` 仍維持 **0**，也沒有任何選擇方法的 response header 被送出；請求最終解析到第一個候選，且 method 為空（**不是** `single`）。結論：多候選設定與 multi_factor 註冊都到位，路由也正確抵達 `reasoning_deep`，但**沒有任何執行期證據顯示 multi_factor 跑完一次候選比較**。此項記為**已知落差／尚未證實**，不是成功；後續調查見 tech-debt [TD049](../agent/tech-debt/td-049-multi-candidate-selection-not-engaged.md)。

Even after 2–3 hard requests that matched `reasoning_deep` (`llm_decision_match_total{decision_name="reasoning_deep"}=2`), the selection counter `llm_model_selection_total{method="multi_factor"}` stayed **0**, and no selection-method response header was emitted; the request resolved to the first candidate with an empty method (**not** `single`). Conclusion: the multi-candidate config and the `multi_factor` registration are both in place and routing correctly reaches `reasoning_deep`, but there is **no runtime evidence that multi_factor ran a candidate comparison to completion**. This is recorded as a **KNOWN GAP / unproven**, not a success; follow-up investigation is tracked in tech-debt [TD049](../agent/tech-debt/td-049-multi-candidate-selection-not-engaged.md).

#### 6.6.3 Fleet-sim TCO closer（管線示範，非 Instinct 校準）/ Fleet-sim TCO closer (pipeline demo, NOT Instinct-calibrated)

新增的 [export-replay-trace.sh](../../deploy/recipes/strix-halo-2box/export-replay-trace.sh) 把閘道唯讀的 `GET :8899/v1/router_replay` 分頁匯出、重塑成 fleet-sim 的 `semantic_router` JSONL（把 `completion_tokens` 改名為 `generated_tokens`、RFC3339 轉 epoch、濾掉 null token 列），共 **396 列**可用 trace。接著跑 fleet-sim：

The new [export-replay-trace.sh](../../deploy/recipes/strix-halo-2box/export-replay-trace.sh) pages the gateway's read-only `GET :8899/v1/router_replay`, reshapes it into fleet-sim's `semantic_router` JSONL (renames `completion_tokens` → `generated_tokens`, RFC3339 → epoch, drops null-token rows), yielding **396 usable trace rows**. Then fleet-sim is run:

```bash
BASE_URL=http://localhost:8899 OUT=poc-trace.jsonl bash export-replay-trace.sh
pip install -e ../../../src/fleet-sim
python3 ../../../src/fleet-sim/examples/semantic_router_trace_replay.py poc-trace.jsonl selected_model
```

**實測（管線輸出，預設 NVIDIA profile）/ Measured (pipeline output, default NVIDIA profile)**：

| 指標 / Metric | 值 / Value |
| --- | --- |
| 重放請求數 / Requests replayed | 396 |
| 機群 / Fleet | 28 GPUs = 20× A100-80GB + 8× A10G |
| 年成本 / Cost | ~$458K/yr |
| P99 TTFT | 8.1 ms |
| SLO | 100.0% |

**誠實警語（必載）/ Honest caveat (required)**：fleet-sim 的 GPU pool 是**硬編碼的 NVIDIA**（`a100`/`a10g`），所以上面的 `$/yr` 與 GPU 數量是**以預設 profile 跑出的管線示範，並非 Instinct/MI350P 校準的 TCO**。延續第 5 節與第 6.5 節的 gfx1151-非-Instinct 切分；校準 MI350P profile 記為後續 tech-debt [TD048](../agent/tech-debt/td-048-router-replay-fleet-sim-exporter-and-uncalibrated-gpu-profile.md)。

fleet-sim's GPU pools are **hardcoded NVIDIA** (`a100`/`a10g`), so the `$/yr` and GPU counts above are a **pipeline demonstration with default profiles, NOT an Instinct/MI350P-calibrated TCO**. This continues the gfx1151-not-Instinct split of sections 5 and 6.5; calibrating an MI350P profile is tracked as follow-up tech-debt [TD048](../agent/tech-debt/td-048-router-replay-fleet-sim-exporter-and-uncalibrated-gpu-profile.md).

#### 6.6.4 WAN 延遲對比 — 待使用者執行 / WAN-latency contrast — PENDING (to be run by user)

第 6.3 節已量到 LAN 上跨盒子那一跳僅 ~0.2 ms；要讓邊緣閘道的優勢在 WAN 下被量化，新增了 [wan-latency-experiment.sh](../../deploy/recipes/strix-halo-2box/wan-latency-experiment.sh)（在 Halo-B 以 `tc netem` 注入 0/20/50 ms 延遲後重量網路跳，含 cleanup trap）。本輪**未執行**：它需要對 Halo-B 的互動式 SSH + sudo（`tc netem`），而本盒子尚未設好金鑰式 SSH（非互動 preflight 以 `Permission denied (publickey,password)` 失敗）。**標記為待執行**——請使用者先 `ssh-copy-id test001@10.96.28.126`（並允許 `tc` 免密 sudo），再執行 `HALO_B_IP=10.96.28.126 HALO_B_SSH=test001@10.96.28.126 bash deploy/recipes/strix-halo-2box/wan-latency-experiment.sh`。

Section 6.3 already measured the cross-box hop at only ~0.2 ms on the LAN; to quantify the edge-gateway advantage under a WAN, [wan-latency-experiment.sh](../../deploy/recipes/strix-halo-2box/wan-latency-experiment.sh) was added (it injects 0/20/50 ms via `tc netem` on Halo-B and re-measures the hop, with a cleanup trap). It was **not run** this round: it needs interactive SSH + sudo (`tc netem`) on Halo-B, which is not key-based yet (the non-interactive preflight failed with `Permission denied (publickey,password)`). **Marked PENDING / to be run by the user**—set up `ssh-copy-id test001@10.96.28.126` (with passwordless sudo for `tc`), then run `HALO_B_IP=10.96.28.126 HALO_B_SSH=test001@10.96.28.126 bash deploy/recipes/strix-halo-2box/wan-latency-experiment.sh`.

#### 6.6.5 截圖與證據 / Screenshots and evidence

本輪的儀表板截圖（登入後的 `/status`、`/monitoring`、`/tracing`）由另一個工作分支**另行擷取至本機** `.agent-harness/experiments/2box-topology/screenshots/`，**不**進版控（屬本機證據，非倉庫資產）。本輪其餘證據（bring-up 日誌、header dump、export 與 fleet-sim 輸出）見 `.agent-harness/experiments/2box-topology/phase2/`。

This round's dashboard screenshots (post-login `/status`, `/monitoring`, `/tracing`) are captured **separately to the local** `.agent-harness/experiments/2box-topology/screenshots/` by a parallel effort and are **not** committed (local evidence, not a repo asset). The rest of this round's evidence (bring-up log, header dumps, export and fleet-sim output) lives under `.agent-harness/experiments/2box-topology/phase2/`.

#### 6.6.6 選擇演算法落差的根因 + 如何真的跑起 session_aware / Root cause of the selection gap, and how to actually engage session_aware

> 一句話框架：6.6.2 留下的「選擇演算法沒被執行」是**設定假象**，不是 bug——選擇方法有兩道短路會讓設定看起來生效、實際卻走 static。本節把根因講清楚，並給出讓 `session_aware` 真的執行的最小設定與驗證方法。
> One-line framing: the "selection algorithm never ran" gap from 6.6.2 is a **config illusion**, not a bug—two short-circuits make a configured method look active while routing actually falls back to static. This section pins the root cause and gives the minimal config plus verification that makes `session_aware` actually run.

根因有兩道短路（皆已對源碼確認）/ The root cause is two short-circuits (both confirmed against source):

- **單一候選會在選擇前就短路 / A single-candidate decision short-circuits before selection** — 當一個 decision 只有一個 `modelRefs`，`selectModelFromCandidates`（[req_filter_classification.go](../../src/semantic-router/pkg/extproc/req_filter_classification.go) ~L86-90）直接回傳該候選、method 標為 `single`，**根本不進選擇演算法**：`session_aware` 不會跑、不會掛上 `SessionPolicy`、`x-vsr-session-phase` 永遠是空的。
  When a decision has only one `modelRefs`, `selectModelFromCandidates` ([req_filter_classification.go](../../src/semantic-router/pkg/extproc/req_filter_classification.go) ~L86-90) returns that candidate with method `single` and **never enters the selection algorithm**: `session_aware` does not run, no `SessionPolicy` is attached, and `x-vsr-session-phase` stays empty.
- **全域 `model_selection.method` 對每決策路由被忽略 / The global `model_selection.method` is ignored for per-decision routing** — 就算有多個候選，`getSelectionMethod`（[req_filter_classification.go](../../src/semantic-router/pkg/extproc/req_filter_classification.go) ~L448-455）**只**用每決策的 `algorithm.Type` 去查 `selectionMethodByAlgorithmType`（[req_filter_classification_runtime.go](../../src/semantic-router/pkg/extproc/req_filter_classification_runtime.go)），其餘一律 fallback 到 `MethodStatic`，**完全不讀** `global.router.model_selection.method`。所以只設全域 method 是**靜默的死設定**——無錯誤、無警告（直到 Part 1 啟動警告落地），路由其實走 static。
  Even with multiple candidates, `getSelectionMethod` ([req_filter_classification.go](../../src/semantic-router/pkg/extproc/req_filter_classification.go) ~L448-455) maps **only** the per-decision `algorithm.Type` via `selectionMethodByAlgorithmType` ([req_filter_classification_runtime.go](../../src/semantic-router/pkg/extproc/req_filter_classification_runtime.go)) and otherwise falls back to `MethodStatic`; it **never reads** `global.router.model_selection.method`. So setting only the global method is **silent dead config**—no error, no warning (until the Part 1 startup warning lands), and routing quietly runs static. 這個死設定足跡記為 tech-debt [TD050](../agent/tech-debt/td-050-global-model-selection-method-ignored.md)，並根因化 6.6.2 觀察到的 [TD049](../agent/tech-debt/td-049-multi-candidate-selection-not-engaged.md)。This dead-config footgun is tracked as tech-debt [TD050](../agent/tech-debt/td-050-global-model-selection-method-ignored.md) and root-causes the [TD049](../agent/tech-debt/td-049-multi-candidate-selection-not-engaged.md) observation from 6.6.2.

要真的讓 `session_aware` 執行，**每個**多候選 decision 需同時滿足兩點：(1) 重疊的 `modelRefs`（>=2 個候選），(2) 自己的 `algorithm: {type: session_aware, session_aware: {...}}` 區塊。實驗設定見 [experiments/session-aware-multicandidate.yaml](../../deploy/recipes/strix-halo-2box/experiments/session-aware-multicandidate.yaml)（由平行作業維護，請以路徑參考、勿改）。

To actually engage `session_aware`, **each** multi-candidate decision must satisfy both: (1) overlapping `modelRefs` (>=2 candidates), and (2) its own `algorithm: {type: session_aware, session_aware: {...}}` block. The experiment config is [experiments/session-aware-multicandidate.yaml](../../deploy/recipes/strix-halo-2box/experiments/session-aware-multicandidate.yaml) (maintained by a parallel effort—reference it by path, do not edit it).

驗證方法 / How to verify:

- **讀 `x-vsr-session-phase` 回應 header / Read the `x-vsr-session-phase` response header** — 每個回應應帶非空的階段值（`user_turn` 或 `tool_loop`）；空值代表又走了上面的短路。
  Each response should carry a non-empty phase (`user_turn` or `tool_loop`); an empty value means a short-circuit above was hit again.
- **在 `:9190` 指標確認 method=session_aware / Confirm method=session_aware in the `:9190` metrics** — `llm_model_selection_total{method="session_aware"}` 應隨命中該 decision 的請求而 >0（對照 6.6.2 中 multi_factor 維持 0 的失敗樣態）。
  `llm_model_selection_total{method="session_aware"}` should climb above 0 as requests hit that decision (contrast with the multi_factor-stayed-0 failure mode in 6.6.2).

**實測（成立的部分）/ Measured (what holds)**：補上重疊候選 + 每決策 `algorithm` 區塊後，選擇方法變成 `session_aware`，`x-vsr-session-phase` 在 64/64 請求上皆有值（`user_turn` / `tool_loop`），session-policy 違規計數從 16/8 降到 0。**誠實警語 / Honest caveat**：該輪流量塌縮到**單一**服務模型，所以「0 違規」本身**不**證明鎖真的擋下了一次實際換模——只證明 method 確實被執行、phase header 確實被填上。延續第 5、6.5 節的誠實切分：這是**選擇路徑被正確啟動**的證據，不是 session-lock 在真實換模壓力下的證明。

**Measured (what holds)**: after adding the overlapping candidates plus the per-decision `algorithm` block, the selection method became `session_aware`, `x-vsr-session-phase` populated on 64/64 requests (`user_turn` / `tool_loop`), and session-policy violation counters dropped from 16/8 to 0. **Honest caveat**: that run collapsed to a **single** served model, so the 0 violations alone do **not** prove a lock prevented a real model switch—only that the method executed and the phase header was filled. Continuing the honest split of sections 5 and 6.5: this is evidence that **the selection path was correctly engaged**, not proof that the session lock holds under real switch pressure.

### 6.7 拓樸吞吐比較：single-box vs edge-2box / Topology throughput comparison (measured-on 2026-06-29)

> 一句話框架：前面證明路由跨盒子正確、跨盒子那一跳便宜；本節回答「換成 2 盒子到底**值不值**」——用 [topology-bench.sh](../../deploy/recipes/strix-halo-2box/topology-bench.sh) 對**同一份負載**量兩種拓樸,把純網路跳數和 agentic 吞吐/尾延遲分開,結論明確:2-box 用 ~0.2ms 的網路代價換到 +25% 吞吐、尾延遲砍半。
> One-line framing: earlier sections proved routing crosses boxes correctly and the hop is cheap; this section answers "is 2-box actually **worth it**"—[topology-bench.sh](../../deploy/recipes/strix-halo-2box/topology-bench.sh) drives the **same fixed load** through both topologies, separating pure network hop from agentic throughput/tail latency. Verdict: 2-box buys +25% throughput and roughly half the tail latency for a ~0.2 ms network cost.

方法 / Method：同一負載(`tool-heavy` sessions=6 turns=8 concurrency=2 → 48 requests),先各自部署(single-box `poc-strix.yaml` 全本機;edge-2box `poc-client-edge.yaml` 小模型 Halo-A、大模型 Halo-B),再各跑一次量測,最後 `--report` 彙整。**實測 / measured**:

| 拓樸 / Topology | rps | p50 ms | p95 ms | p99 ms | edge/dc % | 0-hop ms | 1-hop ms | hop Δ | success |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| edge-2box | 0.643 | 3127.4 | 3661.9 | 7477.8 | 62/38 | 0.316 | 0.491 | **0.175** | 100% |
| single-box | 0.514 | 3262.0 | 7373.4 | 14185.1 | 62/38 | 0.442 | — | — | 100% |

判讀(**推導 / derived**,正負號相對 2-box 基準）/ Reading (derived, relative to the 2-box baseline):

- **吞吐 +25%** / **throughput +25%**：edge-2box 0.643 vs single-box 0.514 rps;分散後不再爭用。/ no contention once spread across boxes.
- **p50 幾乎相同**(3127 vs 3262)/ p50 ~equal：單一不擁擠請求兩者無差。/ an uncontended single request is the same.
- **尾延遲砍半** / **tail halved**:p95 3662 vs 7373(+101%)、p99 7478 vs 14185。single-box 把小+大模型擠在同一顆 APU,並發時互搶 → 尾延遲炸開;2-box 分散 → 收斂。This is the headline: shared-APU contention is what blows up the single-box tail, not the network.
- **跨盒子那一跳 +0.175ms**(可忽略,對齊 6.3 的 ~0.2ms)/ the cross-box hop is +0.175 ms (negligible, matches 6.3); 省下的競爭遠大於多的一跳 / the contention saved dwarfs the extra hop.
- 分流 62/38、distribution `qwen/qwen3.5-rocm`=30(edge)/`google/gemini-3.1-pro`=18(dc) 兩者相同 → 公平比較 / identical, so the comparison is apples-to-apples.

**誠實邊界 / Honest caveat**:兩台都是 gfx1151 APU,差異反映 **topology/競爭/網路**,非硬體等級(延續 5、6.5);48 req / concurrency 2 樣本小,p99 會抖,要更硬的數字加大 `--sessions/--turns/--concurrency` 重跑。/ Both boxes are gfx1151 APUs, so this is topology/contention/network, not a hardware-tier claim; 48 req at concurrency 2 is a small sample (noisy p99)—scale the load for a firmer number.

---

## 參考連結 / Reference links

- 技術定位（router 不挑 endpoint、Envoy 才挑）/ Tech positioning (router does not pick endpoints, Envoy does): [01-tech-study.md](01-tech-study.md) 第 2–3 節 / sections 2–3
- 先量測再模擬／fleet-sim TCO / Measure-then-simulate, fleet-sim TCO: [02-poc-plan.md](02-poc-plan.md) 第 12 節 / section 12
- AMD 對齊（Intelligent Token Routing / LLM Gateway）與誠實邊界 / AMD alignment and honest boundaries: [05-amd-strategy-alignment.md](05-amd-strategy-alignment.md)
- 多節點與 operator 擴展 / Multi-node and operator scale-out: [06-multi-node-and-operator.md](06-multi-node-and-operator.md)
- 路由回應只產 mutations / Routing response emits only mutations: [processor_req_body_routing.go](../../src/semantic-router/pkg/extproc/processor_req_body_routing.go)（`createRoutingResponse`）
- 單機參考設定（後端綁定範例）/ Single-box reference config (backend-binding example): [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml)
- 2 盒子 PoC recipe（由平行作業建立）/ 2-box PoC recipe (created by a parallel effort): [server-bring-up.sh](../../deploy/recipes/strix-halo-2box/server-bring-up.sh)、[poc-client-edge.yaml](../../deploy/recipes/strix-halo-2box/poc-client-edge.yaml)、[client-bring-up.sh](../../deploy/recipes/strix-halo-2box/client-bring-up.sh)、[smoke_test.py](../../deploy/recipes/strix-halo-2box/smoke_test.py)、[README.md](../../deploy/recipes/strix-halo-2box/README.md)
- 文件網站 / Docs site: [vllm-semantic-router.com](https://vllm-semantic-router.com)
