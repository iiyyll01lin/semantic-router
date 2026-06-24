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
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/openai/openai-go"

	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/config"
)

// newFailoverBackend returns an httptest server that returns a per-model HTTP
// status. A model mapped to http.StatusOK gets a valid chat completion;
// anything else returns that status code with an error body (mimicking
// 401/429/5xx upstream failures). It records the ordered sequence of models it
// was asked to serve so tests can assert dispatch order and count.
func newFailoverBackend(t *testing.T, statusByModel map[string]int) (*httptest.Server, *[]string) {
	t.Helper()
	var (
		mu          sync.Mutex
		modelsSeen  []string
		seenPointer = &modelsSeen
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		bodyBytes, _ := io.ReadAll(r.Body)
		var reqMap map[string]interface{}
		_ = json.Unmarshal(bodyBytes, &reqMap)
		model, _ := reqMap["model"].(string)

		mu.Lock()
		modelsSeen = append(modelsSeen, model)
		*seenPointer = modelsSeen
		mu.Unlock()

		status, ok := statusByModel[model]
		if !ok {
			status = http.StatusOK
		}
		if status != http.StatusOK {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"error":{"message":"upstream failure"}}`))
			return
		}

		resp := map[string]interface{}{
			"id":      "chatcmpl-stub",
			"object":  "chat.completion",
			"created": 0,
			"model":   model,
			"choices": []map[string]interface{}{
				{
					"index":         0,
					"message":       map[string]interface{}{"role": "assistant", "content": "answer from " + model},
					"finish_reason": "stop",
				},
			},
			"usage": map[string]interface{}{
				"prompt_tokens":     5,
				"completion_tokens": 7,
				"total_tokens":      12,
			},
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}))
	return server, seenPointer
}

func failoverRequest(onError string, models ...config.ModelRef) *Request {
	params := openai.ChatCompletionNewParams{
		Model:    "auto",
		Messages: []openai.ChatCompletionMessageParamUnion{openai.UserMessage("hello")},
	}
	algo := &config.AlgorithmConfig{Type: "failover"}
	if onError != "" {
		algo.Failover = &config.FailoverAlgorithmConfig{OnError: onError}
	}
	return &Request{
		OriginalRequest: &params,
		ModelRefs:       models,
		Algorithm:       algo,
		DecisionName:    "premium_failover_route",
	}
}

// TestFailoverLooper_PrimaryFailsNextUsed verifies the core behavior: when the
// premium primary model returns a non-2xx status, the request is re-dispatched
// to the next candidate, whose successful response is returned.
func TestFailoverLooper_PrimaryFailsNextUsed(t *testing.T) {
	server, seen := newFailoverBackend(t, map[string]int{
		"premium-cloud": http.StatusUnauthorized, // 401, e.g. missing API key
		"local-dc":      http.StatusOK,
	})
	defer server.Close()

	l := NewFailoverLooper(&config.LooperConfig{Endpoint: server.URL})
	req := failoverRequest("skip",
		config.ModelRef{Model: "premium-cloud"},
		config.ModelRef{Model: "local-dc"},
	)

	out, err := l.Execute(context.Background(), req)
	if err != nil {
		t.Fatalf("Execute failed: %v", err)
	}

	if out.Model != "local-dc" {
		t.Errorf("expected fallback model local-dc, got %q", out.Model)
	}
	if out.AlgorithmType != "failover" {
		t.Errorf("expected AlgorithmType=failover, got %q", out.AlgorithmType)
	}
	if got := *seen; len(got) != 2 || got[0] != "premium-cloud" || got[1] != "local-dc" {
		t.Errorf("expected ordered dispatch [premium-cloud local-dc], got %v", got)
	}
	// Only the successful call should count toward usage accounting.
	want := TokenUsage{PromptTokens: 5, CompletionTokens: 7, TotalTokens: 12}
	if out.Usage != want {
		t.Errorf("Usage = %+v, want %+v (only the winning call)", out.Usage, want)
	}
}

// TestFailoverLooper_FirstSucceedsNoFallback verifies that a healthy primary
// short-circuits: the fallback is never dispatched.
func TestFailoverLooper_FirstSucceedsNoFallback(t *testing.T) {
	server, seen := newFailoverBackend(t, map[string]int{
		"premium-cloud": http.StatusOK,
		"local-dc":      http.StatusOK,
	})
	defer server.Close()

	l := NewFailoverLooper(&config.LooperConfig{Endpoint: server.URL})
	req := failoverRequest("skip",
		config.ModelRef{Model: "premium-cloud"},
		config.ModelRef{Model: "local-dc"},
	)

	out, err := l.Execute(context.Background(), req)
	if err != nil {
		t.Fatalf("Execute failed: %v", err)
	}
	if out.Model != "premium-cloud" {
		t.Errorf("expected primary model premium-cloud, got %q", out.Model)
	}
	if got := *seen; len(got) != 1 || got[0] != "premium-cloud" {
		t.Errorf("expected exactly one dispatch [premium-cloud], got %v", got)
	}
}

// TestFailoverLooper_OnErrorFailSurfacesError verifies that on_error=fail
// surfaces the first failure immediately without attempting later candidates.
func TestFailoverLooper_OnErrorFailSurfacesError(t *testing.T) {
	server, seen := newFailoverBackend(t, map[string]int{
		"premium-cloud": http.StatusInternalServerError, // 500
		"local-dc":      http.StatusOK,
	})
	defer server.Close()

	l := NewFailoverLooper(&config.LooperConfig{Endpoint: server.URL})
	req := failoverRequest("fail",
		config.ModelRef{Model: "premium-cloud"},
		config.ModelRef{Model: "local-dc"},
	)

	_, err := l.Execute(context.Background(), req)
	if err == nil {
		t.Fatal("expected error when on_error=fail and primary fails, got nil")
	}
	if got := *seen; len(got) != 1 || got[0] != "premium-cloud" {
		t.Errorf("expected only the primary to be dispatched, got %v", got)
	}
}

// TestFailoverLooper_AllFail verifies that exhausting every candidate returns
// an error rather than a fabricated success.
func TestFailoverLooper_AllFail(t *testing.T) {
	server, seen := newFailoverBackend(t, map[string]int{
		"premium-cloud": http.StatusServiceUnavailable, // 503
		"local-dc":      http.StatusTooManyRequests,    // 429
	})
	defer server.Close()

	l := NewFailoverLooper(&config.LooperConfig{Endpoint: server.URL})
	req := failoverRequest("skip",
		config.ModelRef{Model: "premium-cloud"},
		config.ModelRef{Model: "local-dc"},
	)

	_, err := l.Execute(context.Background(), req)
	if err == nil {
		t.Fatal("expected error when all candidates fail, got nil")
	}
	if got := *seen; len(got) != 2 {
		t.Errorf("expected every candidate to be attempted, got %v", got)
	}
}

// TestFailoverLooper_DefaultsToSkip verifies that omitting the failover block
// defaults on_error to skip (failover proceeds to the next candidate).
func TestFailoverLooper_DefaultsToSkip(t *testing.T) {
	server, seen := newFailoverBackend(t, map[string]int{
		"premium-cloud": http.StatusBadGateway, // 502
		"local-dc":      http.StatusOK,
	})
	defer server.Close()

	l := NewFailoverLooper(&config.LooperConfig{Endpoint: server.URL})
	// No failover block at all → default behavior must be skip.
	req := failoverRequest("",
		config.ModelRef{Model: "premium-cloud"},
		config.ModelRef{Model: "local-dc"},
	)

	out, err := l.Execute(context.Background(), req)
	if err != nil {
		t.Fatalf("Execute failed: %v", err)
	}
	if out.Model != "local-dc" {
		t.Errorf("expected fallback model local-dc with default skip, got %q", out.Model)
	}
	if got := *seen; len(got) != 2 {
		t.Errorf("expected fallback to be attempted by default, got %v", got)
	}
}

// TestFailoverLooper_NoModels verifies an empty candidate list is an error.
func TestFailoverLooper_NoModels(t *testing.T) {
	l := NewFailoverLooper(&config.LooperConfig{Endpoint: "http://unused"})
	req := failoverRequest("skip")
	if _, err := l.Execute(context.Background(), req); err == nil {
		t.Fatal("expected error with no models configured, got nil")
	}
}
