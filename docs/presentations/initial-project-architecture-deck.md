# vLLM Semantic Router Initial Project Deck

Audience: technical decision makers  
Format: PDF  
Language: English titles with Traditional Chinese support text  
Data policy: use only verified numbers from committed docs/readmes

## Slide 1. vLLM Semantic Router Initial Project / 初始專案架構提案

Message: present vLLM Semantic Router as a signal-driven control plane for routing, safety, observability, and edge fleet governance.

Source anchors:

- `website/static/img/banner.png`
- `website/static/img/vllm-sr-logo.social.png`

## Slide 2. Technical Thesis / 技術主張

Message: a router becomes useful when it interprets request signals, composes decisions, and routes to the right execution lane with traceability.

Customer takeaway: this is not a single model demo; it is an operational layer for multi-model systems.

## Slide 3. System Architecture / 系統架構

Message: requests are decomposed into signals, projected into decisions, optionally transformed by plugins, then routed to model targets.

Source anchors:

- `website/static/img/architecture.png`
- `paper/sections/architecture.tex`
- `paper/sections/signal_engine.tex`
- `paper/sections/decision_engine.tex`

## Slide 4. Runtime Data Path / 執行期資料路徑

Message: Envoy receives OpenAI-compatible traffic, calls the semantic router through ExtProc, and forwards the enriched request to backend providers.

Source anchors:

- `paper/sections/extproc_pipeline.tex`
- `bench` harness memory: Envoy `:8801`, ExtProc `:50051`, Prometheus `:9279`, backend `:8000`

## Slide 5. Decision Fabric / 決策織物

Message: declarative decision rules make routing understandable, portable, and auditable across products and deployment environments.

Source anchors:

- `website/static/img/signal-0.png`
- `dashboard/frontend` screenshots in `website/static/img/dashboard`

## Slide 6. Safety and Governance Lanes / 安全與治理路由

Message: PII and jailbreak detections are signals; policy decisions can trigger fast responses, response guards, or protected model lanes.

Source anchors:

- `deploy/recipes/strix-halo-poc/README.md`
- `paper/sections/safety.tex`
- `paper/sections/halugate.tex`

## Slide 7. Observability and Explainability / 可觀測性與可解釋性

Message: every route can expose diagnostic headers and dashboard views so customers can inspect model selection, signals, and savings.

Source anchors:

- `website/static/img/dashboard/config.png`
- `website/static/img/grafana_screenshot.png`
- `paper/sections/observability.tex`

## Slide 8. Strix Halo Single-Box PoC / 單機 PoC

Message: one Strix Halo box runs the full local stack: Ollama ROCm, five tier models, router, safety lane, smoke tests, and offline validation.

Source anchors:

- `docs/poc/03-strix-halo-runbook.md` Mermaid bring-up overview
- `deploy/recipes/strix-halo-poc/README.md`
- `deploy/recipes/strix-halo-poc/poc-strix.yaml`
- `deploy/recipes/strix-halo-poc/bring-up.sh`

## Slide 9. 2-Box Edge Gateway / 雙機邊緣閘道路由

Message: keep routine traffic local at the edge and escalate hard tiers to a second box or external provider without double-hop routing.

Source anchors:

- `docs/poc/07-client-server-topology.md` Mermaid edge-gateway topology
- `deploy/recipes/strix-halo-2box/README.md`
- `deploy/recipes/strix-halo-2box/poc-client-edge.yaml`
- `deploy/recipes/strix-halo-2box/deploy-2box.sh`

## Slide 10. Fleet Control Plane / 艦隊控制平面

Message: a pull-mode central control plane distributes signed config to bare edge gateways with central audit and drift recovery.

Source anchors:

- `deploy/recipes/strix-halo-fleet-2box/README.md`
- `deploy/recipes/strix-halo-fleet-2box/docs/research-pipeline.md` Mermaid fleet pipeline
- `deploy/recipes/strix-halo-fleet-2box/ccp_server.py`
- `deploy/recipes/strix-halo-fleet-2box/fleet_agent.py`

## Slide 11. Convergence Contract / 收斂契約

Message: the Python CCP signs `sha256(config_bytes)`; the Go router reports `/config/hash`; agents apply config in place so fsnotify hot-reloads without restart.

Source anchors:

- `deploy/recipes/strix-halo-fleet-2box/docs/research-pipeline.md`
- `deploy/recipes/strix-halo-fleet-2box/README.md`

## Slide 12. Verified Evidence / 已驗證成果

Message: the fleet design is hardware-verified on two real Strix Halo gateways.

Verified figures:

- Run bundle: `run-20260701-154843`
- Both real routers converged to signed hash `a78aebc5fd5f`
- One central edit converged both routers to `fc739baa...`
- Rollback returned both routers to `a78aebc5fd5f`
- Five converged versions: `v1` to `v5`
- Cross-box convergence span: `0-3s`, bounded by poll interval
- Halo-A router cold-start: `565s`
- Model footprint: about `44 GB` Ollama tiers plus about `0.6 GB` PII model

Source anchors:

- `deploy/recipes/strix-halo-fleet-2box/README.md`
- `deploy/recipes/strix-halo-fleet-2box/docs/research-pipeline.md`

## Slide 13. Benchmark and TCO Context / Benchmark 與 TCO 脈絡

Message: the benchmark stack and fleet-sim provide the path from replay evidence to TCO analysis, while hardware-specific performance claims remain explicitly scoped.

Caveat: fleet-sim TCO is simulation/extrapolation unless rerun on target hardware.

Source anchors:

- `website/static/img/fleet-sim/pareto-frontier.png`
- `bench`
- `src/fleet-sim`
- `deploy/recipes/strix-halo-2box/export-replay-trace.sh`

## Slide 14. Initial Project Scope / 初始專案範圍

Message: propose a controlled customer pilot that starts with one routing profile, one safety lane, one observability loop, and one deployment topology.

Source anchors:

- `docs/poc/08-topology-promotion-and-governance.md` Mermaid promoted edge-gateway topology

Suggested deliverables:

- customer traffic taxonomy and signal map
- routing config and DSL review
- single-box or 2-box deployment rehearsal
- dashboard and replay evidence
- final architecture and operating runbook

## Slide 15. Risks and Mitigations / 風險與緩解

Message: the key risks are operational, not architectural: image/version skew, model staging, network reachability, and finer-grained hot-reload metrics.

Mitigation anchors:

- pin router images or migrate config schema deliberately
- preflight model assets and PII ONNX export
- verify container-to-host reachability for remote tiers
- add write-to-converge timers for sub-second hot-reload measurement

## Slide 16. Roadmap / 下一步

Message: scale the customer pilot into a governed multi-model control plane with better measurement, broader fleet patterns, and customer dataset validation.

Roadmap items:

- sub-second reload/convergence instrumentation
- customer replay and data-quality calibration
- broader fleet and operator alignment
- cost/latency policy tuning
- production readiness review