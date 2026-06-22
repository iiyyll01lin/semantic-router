# POC 與 AMD 企業 AI 策略對齊 / POC × AMD Enterprise AI Strategy Alignment

> 一句話開場：這份單機 Strix Halo PoC（[01-tech-study.md](01-tech-study.md)–[04-dashboard-tour.md](04-dashboard-tour.md)）正是 AMD CIO「Enterprise AI at AMD / TSMC」簡報裡所說的 `Intelligent Token Routing` 與 `LLM Gateway` 的可運行落地版本——把簡報的策略圖變成你今天就能在 dashboard 上點開的元件。
> One-line opener: this single-box Strix Halo PoC ([01-tech-study.md](01-tech-study.md)–[04-dashboard-tour.md](04-dashboard-tour.md)) is a runnable realization of exactly what the AMD CIO "Enterprise AI at AMD / TSMC" deck calls `Intelligent Token Routing` and the `LLM Gateway`—turning the deck's strategy diagrams into components you can click open in the dashboard today.

本文件接續既有報告系列（[01-tech-study.md](01-tech-study.md)、[02-poc-plan.md](02-poc-plan.md)、[03-strix-halo-runbook.md](03-strix-halo-runbook.md)、[04-dashboard-tour.md](04-dashboard-tour.md)），把 AMD 自家簡報的關鍵 slide 對映到本 PoC 的具體元件、設定檔與 dashboard 頁面，並誠實標出 router **不**涵蓋的範圍。它的用途是：在 kickoff 時用客戶自己的策略語言，證明這套 router 不是另一個供應商提案，而是 AMD 已經畫出來的那張圖。

This document continues the existing report series ([01-tech-study.md](01-tech-study.md), [02-poc-plan.md](02-poc-plan.md), [03-strix-halo-runbook.md](03-strix-halo-runbook.md), [04-dashboard-tour.md](04-dashboard-tour.md)). It maps the key slides of AMD's own deck onto this PoC's concrete components, config files, and dashboard pages, and honestly marks what the router does **not** cover. Its purpose: at kickoff, use the customer's own strategy language to show that this router is not another vendor pitch but the very diagram AMD already drew.

---

## 1. 為什麼這是「AMD 自己的論點」/ Why This Is "AMD's Own Thesis"

AMD CIO 簡報的 Slide 35–37（Tokenomics、Current/Future State）給出一個明確的結論：**「Agentic AI 讓 token 需求倍增——唯有混合策略能把上升的成本變成可控的方程式」**。它把企業同時定位成 token 的 `Generators & Consumers`，並畫出一個 `Intelligent Token Routing` 的分流：`Premium Tokens → Frontier Model`、`Local Tokens → MI350P AMD Servers / Local LLMs`。

Slides 35–37 of the AMD CIO deck (Tokenomics, Current/Future State) state a clear conclusion: **"Agentic AI multiplies token demand—only a hybrid strategy turns rising costs into a manageable equation."** They cast the enterprise as both `Generators & Consumers` of tokens, and draw an `Intelligent Token Routing` split: `Premium Tokens → Frontier Model` and `Local Tokens → MI350P AMD Servers / Local LLMs`.

這正是 vLLM Semantic Router 的逐字定義：一個訊號驅動的路由器，把常規 token 留在本地 AMD 硬體、只把困難 token 升級到 frontier 雲端模型（見 [01-tech-study.md](01-tech-study.md) 第 1–2 節）。因此這份 PoC 不需要說服客戶相信一個新主張——它把客戶自家 CIO 已經背書的主張，變成一個能量測、能 demo 的系統。

This is, word for word, the definition of vLLM Semantic Router: a signal-driven router that keeps routine tokens on local AMD hardware and escalates only hard tokens to frontier cloud models (see sections 1–2 of [01-tech-study.md](01-tech-study.md)). So this PoC does not need to convince the customer of a new claim—it turns a claim their own CIO has already endorsed into a measurable, demoable system.

---

## 2. Slide → POC 元件對照表 / Slide → POC Component Mapping

下表把每張關鍵 slide 對映到 PoC 的承載元件、可指認的設定／程式檔，以及 demo 時要點開的 dashboard 頁面（頁面路徑見 [04-dashboard-tour.md](04-dashboard-tour.md)）。

The table below maps each key slide to the PoC component that carries it, the identifiable config/source file, and the dashboard page to open during the demo (page routes are in [04-dashboard-tour.md](04-dashboard-tour.md)).

| AMD 簡報 slide / AMD deck slide | PoC 承載元件 / PoC component | 設定或程式檔 / Config or source file | Dashboard 頁面 / Dashboard page |
| --- | --- | --- | --- |
| Slide 35–36 `Intelligent Token Routing` / Tokenomics（Premium→Frontier、Local→MI350P/Local LLM）| 語意分層路由本體：5 個 tier、14 條決策、依難度／領域升級 / The semantic tiered router itself: 5 tiers, 14 decisions, escalate by difficulty/domain | [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml)（`providers.models` pricing 與 `routing.decisions`）、[processor_req_body_routing.go](../../src/semantic-router/pkg/extproc/processor_req_body_routing.go) | Config — Models / Decisions（`/config`）、Brain / Topology（`/topology`）、Playground（`/playground`）|
| Slide 36 future-state tokenomics（MI350P 機群經濟學）/ future-state tokenomics (MI350P fleet economics) | router-replay → fleet-sim 的「先量測再模擬」TCO 收尾 / measure-then-simulate TCO closer | [router_replay_cost.go](../../src/semantic-router/pkg/extproc/router_replay_cost.go)、[src/fleet-sim/run_sim.py](../../src/fleet-sim/run_sim.py)（見 [02-poc-plan.md](02-poc-plan.md) 第 12 節）| Insight（`/insights`）、Fleet Sim — Overview / Runs（`/fleet-sim`）|
| Slide 34 `AMD OpenClaw` + `LLM Gateway`（企業／外部雲以閘道分隔）/ OpenClaw + LLM Gateway | Envoy + ExtProc router 即「LLM Gateway」；多代理操作台對映 OpenClaw / Envoy + the ExtProc router *is* the LLM Gateway; the multi-agent console maps to OpenClaw | [server.go](../../src/semantic-router/pkg/extproc/server.go)、[OpenClawPage.tsx](../../dashboard/frontend/src/pages/OpenClawPage.tsx) | ClawOS（`/clawos`）、Playground（`/playground`）|
| Slide 28 `Agent Gateway`（MCP、Tool Registry、Auth、Policy Enforcement）/ Agent Gateway | MCP servers & tools 設定 + 高優先序 `security_guard` 政策 lane + RBAC / MCP servers & tools config + high-priority `security_guard` policy lane + RBAC | [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml)（`mcp`、`security_guard`）、[SecurityPolicyPage.tsx](../../dashboard/frontend/src/pages/SecurityPolicyPage.tsx) | Config — MCP Servers & Tools（`/config`）、Security Policy（`/security`）|
| Slide 18 reference stack `Agent LLM (Domain-Specific) + Critic LLM (Frontier)` | 分層升級 + reasoning gating：本地 domain 模型處理常規、frontier 模型作為「critic / 深推理」/ tiered escalation + reasoning gating: local domain model for routine, frontier model as critic/deep-reasoning | [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml)（`formal_math_proof`、`reasoning_deep` 等決策）| Config — Decisions（`/config`）、Playground（`/playground`）|
| Slide 23–27 `Orion` / `Sentinel`（安全診斷、self-healing、confidence-driven path selection、SOC 偵測）| 安全訊號 + 信心驅動路由 + 兩層 jailbreak 防護 / security signals + confidence-driven routing + two-layer jailbreak defense | [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml)（`jailbreak_attempt`、`contains_pii`、`response_jailbreak`）| Playground（`/playground`）、Config — Signals（`/config`）|
| Slide 12 maturity ladder `Assist→Suggest→Automate→Autonomous`；Slide 33–34 Digital Workers（自有身分／權限）| agentic 多輪 session 路由 demo + 使用者／角色身分 / agentic multi-turn session-routing demo + user/role identity | [bench/agentic_routing_live_benchmark.py](../../bench/agentic_routing_live_benchmark.py)、[UsersPage.tsx](../../dashboard/frontend/src/pages/UsersPage.tsx) | ClawOS（`/clawos`）、Users（`/users`）、Playground（`/playground`）|
| Slide 13 layers `Plumbing AI infra` / `Harvesting insights`（Security & People）| 路由基礎設施（plumbing）+ 可觀測性（harvesting）/ routing plumbing + observability harvesting | [poc-strix.yaml](../../deploy/recipes/strix-halo-poc/poc-strix.yaml)（`global.services.observability`）| Status（`/status`）、Monitoring（`/monitoring`）、Tracing（`/tracing`）、Insight（`/insights`）|

---

## 3. 三條對齊主線 / Three Alignment Threads

- **Token 經濟 / Token economics（Slide 35–37）** — router 的分層路由直接是簡報的 `Intelligent Token Routing`；它能用設定檔 `pricing` 對比「全走最貴模型」基準算出省下多少（[02-poc-plan.md](02-poc-plan.md) 第 1 節），再用 router-replay → fleet-sim 在部署機群**之前**證明 MI350P 機群的 TCO（[02-poc-plan.md](02-poc-plan.md) 第 12 節）。
  The router's tiered routing *is* the deck's `Intelligent Token Routing`; it computes savings from config `pricing` against an all-most-expensive baseline (section 1 of [02-poc-plan.md](02-poc-plan.md)), then proves MI350P fleet TCO *before* deploying the fleet via router-replay → fleet-sim (section 12 of [02-poc-plan.md](02-poc-plan.md)).
- **安全治理 / Security governance（Slide 23–28）** — `pii` 與 `jailbreak` 訊號把風險請求導向高優先序的 `security_guard`，由 `fast_response` 即時拒絕、`response_jailbreak` 在輸出端回 HTTP 403；這對映簡報的 Orion/Sentinel 安全層與 Agent Gateway 的 `Policy Enforcement`（[04-dashboard-tour.md](04-dashboard-tour.md) Playground 段落）。
  The `pii` and `jailbreak` signals steer risky requests to the high-priority `security_guard`, refused immediately by `fast_response` with `response_jailbreak` returning HTTP 403 on output; this maps to the deck's Orion/Sentinel security layer and the Agent Gateway `Policy Enforcement` (Playground section of [04-dashboard-tour.md](04-dashboard-tour.md)).
- **Agentic 成熟度 / Agentic maturity（Slide 12、18、33–34）** — 多輪 session 路由 demo（[bench/agentic_routing_live_benchmark.py](../../bench/agentic_routing_live_benchmark.py)）展示 session 內的 selected-model 連續性與 tool-loop 治理，把簡報的 `Agent LLM + Critic LLM` 與 Digital Worker 主軸落到可量測的流量上；dashboard 的 ClawOS（`/clawos`）對映 Slide 34 的 OpenClaw。
  The multi-turn session-routing demo ([bench/agentic_routing_live_benchmark.py](../../bench/agentic_routing_live_benchmark.py)) shows in-session selected-model continuity and tool-loop governance, grounding the deck's `Agent LLM + Critic LLM` and Digital Worker themes in measurable traffic; the dashboard's ClawOS (`/clawos`) maps to OpenClaw on Slide 34.

---

## 4. 誠實邊界：router 不涵蓋的部分 / Honest Boundaries: What the Router Does NOT Cover

對齊不等於宣稱「router 等於整個 AMD 企業 AI 願景」。為了讓報告可信，明確標出 router **不**負責的層：

Alignment does not mean claiming the router equals the whole AMD enterprise AI vision. To keep the report credible, here is what the router explicitly does **not** own:

- **OPTIMA 資料平台層 / OPTIMA data platform layer（Slide 14–18、29）** — 資料湖倉（lakehouse）、Apache Iceberg、Nessie、knowledge graph、ETL／資料管線。這些是**資料平台**的職責，不是 router 的職責。Router 只在「Knowledge Base 特性」上**部分**接觸這一層（[04-dashboard-tour.md](04-dashboard-tour.md) 的 Knowledge Base 群組與 Knowledge Map），它能引用既有的 base 做語意檢索，但**不**負責資料的攝取、版本控管、治理或 graph 建模。
  Data lakehouse, Apache Iceberg, Nessie, knowledge graphs, and ETL/data pipelines. These are the **data platform's** responsibility, not the router's. The router only **partly** touches this layer via its Knowledge Base features (the Knowledge Base group and Knowledge Map in [04-dashboard-tour.md](04-dashboard-tour.md)): it can reference existing bases for semantic retrieval, but it does **not** own data ingestion, versioning, governance, or graph modeling.
- **完整的 agent 編排 / Full agent orchestration** — router 做的是 LLM Gateway 層的「選模型 + 安全 + session 連續性」，而不是 Slide 28 完整 `Agent Session Mgmt` / `Context Relay` 的有狀態工作流引擎。ClawOS 頁面呈現的是多代理的**操作視圖**，底層編排仍是另一個系統的職責。
  The router does the LLM Gateway-layer "pick model + security + session continuity," not the full stateful workflow engine behind Slide 28's `Agent Session Mgmt` / `Context Relay`. The ClawOS page presents an **operational view** of multiple agents; the underlying orchestration remains another system's responsibility.
- **真實機群效能 / Real fleet performance** — fleet-sim 給的是**模擬**的 TCO 與容量，不是 Instinct 機群的實測吞吐／延遲；跨節點數字是外推，必須標示（[02-poc-plan.md](02-poc-plan.md) 第 12 節「模擬的邊界」）。
  fleet-sim gives **simulated** TCO and capacity, not measured Instinct-fleet throughput/latency; cross-node numbers are extrapolation and must be labeled (the "honest boundaries" of section 12 in [02-poc-plan.md](02-poc-plan.md)).

把這些邊界說清楚，反而讓「router = Intelligent Token Routing / LLM Gateway」的對齊更站得住腳：它精準地對到 AMD 那張圖的中介層，而不是宣稱包辦上下游。

Stating these boundaries actually strengthens the "router = Intelligent Token Routing / LLM Gateway" alignment: it maps precisely to the gateway tier of AMD's diagram rather than claiming to own everything above and below it.

---

## 5. 在 demo 裡怎麼用這份對齊 / Using This Alignment in the Demo

依 [04-dashboard-tour.md](04-dashboard-tour.md) 的 POC Demo 動線走時，可在以下節點直接引用本文件的對照：

When walking the POC Demo Flow in [04-dashboard-tour.md](04-dashboard-tour.md), cite this mapping at these moments:

- 開 **Config — Models / Decisions** 時：「這就是 Slide 35 的 `Intelligent Token Routing`——`Local Tokens → 本地模型`、`Premium Tokens → Frontier`。」/ At **Config — Models / Decisions**: "This is Slide 35's `Intelligent Token Routing`—`Local Tokens → local models`, `Premium Tokens → Frontier`."
- 開 **Playground** 的 PII/jailbreak 兩筆時：「這是 Slide 23–28 的 Orion/Sentinel 安全層與 Agent Gateway 的 Policy Enforcement。」/ At the **Playground** PII/jailbreak requests: "This is the Orion/Sentinel security layer of Slides 23–28 and the Agent Gateway Policy Enforcement."
- 開 **ClawOS** 與 agentic 多輪步驟時：「這是 Slide 34 的 OpenClaw 與 Slide 12 的 `Automate→Autonomous` 成熟度。」/ At **ClawOS** and the agentic multi-turn step: "This is Slide 34's OpenClaw and the `Automate→Autonomous` maturity of Slide 12."
- 開 **Insight → Fleet Sim** 收尾時：「這是 Slide 36 的 future-state tokenomics——我們在部署 MI350P 機群前先證明它的 TCO。」/ At the **Insight → Fleet Sim** closer: "This is Slide 36's future-state tokenomics—we prove the MI350P fleet's TCO before deploying it."
