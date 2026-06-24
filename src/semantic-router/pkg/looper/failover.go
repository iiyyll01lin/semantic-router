/*
Copyright 2025 vLLM Semantic Router.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package looper

import (
	"context"
	"fmt"

	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/config"
	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/observability/logging"
)

// FailoverLooper implements cross-model / cross-provider failover.
//
// It dispatches the request against each ModelRef in the authored order and
// returns the first successful (2xx) response. When a candidate call fails
// with a non-2xx status (e.g. 401/429/5xx) or a transport error, the looper
// re-dispatches to the NEXT candidate. Each candidate is a fully independent
// dispatch: the looper round-trips back through extproc, so the target model
// name, request body `model` field, auth header, and provider profile are all
// resolved per candidate. This is the genuine cross-provider failover that
// Envoy retry policies cannot express (Envoy can only retry within a single
// cluster/auth/body).
//
// Ordering is always the authored modelRefs order; FailoverLooper never
// reorders candidates the way the confidence looper sorts by param_size.
type FailoverLooper struct {
	*BaseLooper
}

// NewFailoverLooper creates a new FailoverLooper instance.
func NewFailoverLooper(cfg *config.LooperConfig) *FailoverLooper {
	return &FailoverLooper{
		BaseLooper: NewBaseLooper(cfg),
	}
}

// Execute dispatches candidates in order until one succeeds.
//
// on_error policy (algorithm.failover.on_error):
//   - "skip" (default): on a failed candidate, try the next modelRef.
//   - "fail": surface the first candidate's error immediately and stop.
//
// When every candidate fails (or on_error=fail trips on the first failure),
// Execute returns an error so extproc can surface an upstream failure instead
// of a fabricated success.
func (l *FailoverLooper) Execute(ctx context.Context, req *Request) (*Response, error) {
	if len(req.ModelRefs) == 0 {
		return nil, fmt.Errorf("no models configured")
	}

	// Set decision name in client for header transmission.
	l.client.SetDecisionName(req.DecisionName)

	onError := failoverOnError(req)

	logging.ComponentEvent("looper", "execution_started", map[string]interface{}{
		"looper":           "failover",
		"decision":         req.DecisionName,
		"candidate_models": len(req.ModelRefs),
		"streaming":        req.IsStreaming,
		"on_error":         onError,
	})

	var modelsUsed []string
	iteration := 0

	for _, modelRef := range req.ModelRefs {
		iteration++
		modelName := modelRef.Model
		if modelRef.LoRAName != "" {
			modelName = modelRef.LoRAName
		}

		accessKey := ""
		if req.ModelParams != nil {
			if params, ok := req.ModelParams[modelRef.Model]; ok {
				accessKey = params.AccessKey
			}
		}

		logging.ComponentDebugEvent("looper", "model_dispatch_started", map[string]interface{}{
			"looper":    "failover",
			"decision":  req.DecisionName,
			"model_ref": modelName,
			"iteration": iteration,
		})

		// FailoverLooper does not need logprobs (no confidence scoring).
		resp, err := l.client.CallModel(ctx, req.OriginalRequest, modelName, req.IsStreaming, iteration, nil, accessKey)
		if err != nil {
			modelsUsed = append(modelsUsed, modelName)
			logging.ComponentWarnEvent("looper", "model_dispatch_failed", map[string]interface{}{
				"looper":    "failover",
				"decision":  req.DecisionName,
				"model_ref": modelName,
				"iteration": iteration,
				"error":     err.Error(),
			})
			if onError == "fail" {
				return nil, fmt.Errorf("model %s failed: %w", modelName, err)
			}
			// Default ("skip"): fall through to the next candidate.
			continue
		}

		modelsUsed = append(modelsUsed, modelName)
		logging.ComponentEvent("looper", "execution_completed", map[string]interface{}{
			"looper":         "failover",
			"decision":       req.DecisionName,
			"models_used":    modelsUsed,
			"iterations":     iteration,
			"selected_model": modelName,
			"reason":         "first_success",
		})
		return l.formatFailoverResponse(resp, modelsUsed, iteration, req.IsStreaming)
	}

	return nil, fmt.Errorf("all models failed")
}

// failoverOnError resolves the configured on_error policy, defaulting to
// "skip" so the algorithm actually fails over to later candidates.
func failoverOnError(req *Request) string {
	if req.Algorithm != nil && req.Algorithm.Failover != nil && req.Algorithm.Failover.OnError != "" {
		return req.Algorithm.Failover.OnError
	}
	return "skip"
}

// formatFailoverResponse renders the single winning response. Unlike the
// aggregating loopers, failover returns exactly the chosen candidate's content
// and usage; only that one call counts toward token accounting.
func (l *FailoverLooper) formatFailoverResponse(resp *ModelResponse, modelsUsed []string, iterations int, streaming bool) (*Response, error) {
	agg := &AggregatedResponse{
		Models:          modelsUsed,
		Responses:       []*ModelResponse{resp},
		CombinedContent: resp.Content,
		FinalModel:      resp.Model,
		AverageLogprob:  resp.AverageLogprob,
		HasToolCalls:    resp.HasToolCalls,
	}

	var (
		out *Response
		err error
	)
	if streaming {
		out, err = l.formatStreamingResponse(agg, modelsUsed, iterations)
	} else {
		out, err = l.formatJSONResponse(agg, modelsUsed, iterations)
	}
	if err != nil {
		return nil, err
	}
	out.AlgorithmType = "failover"
	return out, nil
}
