package config

import (
	"strings"

	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/observability/logging"
)

// modelSelectionMethodStatic is the selection method that uses the static
// selector. It is also the implicit fallback when a decision has no per-decision
// algorithm, so a global method equal to this value is never silently ignored.
const modelSelectionMethodStatic = "static"

// globalModelSelectionMethodIgnored reports whether a non-static global
// model_selection.method is configured while no decision carries a per-decision
// algorithm block. In that situation the router resolves the selection method
// per decision from decision.algorithm.type (see getSelectionMethod in
// pkg/extproc) and falls back to static; the global method is never consulted
// for routing, so it is silent dead config. Kept as a pure predicate so both the
// warn-triggering and no-warn cases are deterministically unit testable without
// capturing log output.
func globalModelSelectionMethodIgnored(cfg *RouterConfig) bool {
	method := strings.TrimSpace(cfg.ModelSelection.Method)
	if method == "" || method == modelSelectionMethodStatic {
		return false
	}
	for _, d := range cfg.Decisions {
		if d.Algorithm != nil && strings.TrimSpace(d.Algorithm.Type) != "" {
			return false
		}
	}
	return true
}

// warnGlobalModelSelectionMethodIgnored emits a startup warning when the global
// model_selection.method is set to a non-static method but no decision carries a
// per-decision algorithm block. The warning surfaces the silent dead-config gap
// without blocking boot, mirroring warnModelSwitchGateEnforceWithoutCostSignals.
func warnGlobalModelSelectionMethodIgnored(cfg *RouterConfig) {
	if !globalModelSelectionMethodIgnored(cfg) {
		return
	}
	logging.Warnf(
		"model_selection.method=%q is configured globally but no decision carries a per-decision "+
			"algorithm block. Per-decision routing resolves the selection method from "+
			"decision.algorithm.type and falls back to static, so the global model_selection.method "+
			"is never consulted for routing and is silently ignored. Add an algorithm block "+
			"(decision.algorithm.type) to the decisions that should use this method.",
		strings.TrimSpace(cfg.ModelSelection.Method),
	)
}
