package extproc

import (
	"strings"
	"testing"

	core "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	ext_proc "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
)

func TestIsSensitiveHeaderKey(t *testing.T) {
	sensitive := []string{
		"Authorization", "authorization", "  Authorization  ",
		"x-api-key", "X-API-Key", "api-key", "proxy-authorization",
		"x-goog-api-key", "x-litellm-api-key", "x-user-openai-key",
		"x-session-token", "client-secret",
	}
	for _, k := range sensitive {
		if !isSensitiveHeaderKey(k) {
			t.Errorf("expected %q to be treated as sensitive", k)
		}
	}
	notSensitive := []string{
		"content-length", "x-selected-model", "x-vsr-destination-endpoint",
		"traceparent", "content-type", ":status",
	}
	for _, k := range notSensitive {
		if isSensitiveHeaderKey(k) {
			t.Errorf("expected %q NOT to be sensitive", k)
		}
	}
}

func requestBodyResponseWithHeaders(headers ...*core.HeaderValueOption) *ext_proc.ProcessingResponse {
	return &ext_proc.ProcessingResponse{
		Response: &ext_proc.ProcessingResponse_RequestBody{
			RequestBody: &ext_proc.BodyResponse{
				Response: &ext_proc.CommonResponse{
					Status:         ext_proc.CommonResponse_CONTINUE,
					HeaderMutation: &ext_proc.HeaderMutation{SetHeaders: headers},
				},
			},
		},
	}
}

func TestRedactProcessingResponseMasksCredentialsAndKeepsOriginal(t *testing.T) {
	const authCanary = "Bearer redact-me-auth-canary-001"
	const anthropicCanary = "redact-me-canary-002"
	resp := requestBodyResponseWithHeaders(
		&core.HeaderValueOption{Header: &core.HeaderValue{Key: "Authorization", RawValue: []byte(authCanary)}},
		&core.HeaderValueOption{Header: &core.HeaderValue{Key: "x-api-key", Value: anthropicCanary}},
		&core.HeaderValueOption{Header: &core.HeaderValue{Key: "x-selected-model", RawValue: []byte("qwen3-122b")}},
	)

	redacted := redactResponseForLog(resp)

	// 1. The dump string must not contain either credential value.
	dump := strings.ToLower(redactedResponseDump(redacted))
	if strings.Contains(dump, "canary-001") || strings.Contains(dump, "canary-002") {
		t.Fatalf("redacted dump still contains a credential: %s", dump)
	}

	rm := responseHeaderMutation(redacted)
	gotAuth := headerValueByKey(rm, "Authorization")
	gotApiKey := headerValueByKey(rm, "x-api-key")
	gotModel := headerValueByKey(rm, "x-selected-model")
	if gotAuth != redactedHeaderValue {
		t.Errorf("Authorization not redacted: %q", gotAuth)
	}
	if gotApiKey != redactedHeaderValue {
		t.Errorf("x-api-key not redacted: %q", gotApiKey)
	}
	if gotModel != "qwen3-122b" {
		t.Errorf("non-sensitive header must be preserved, got %q", gotModel)
	}

	// 2. The ORIGINAL response (sent to Envoy) must still carry the real value.
	origRM := responseHeaderMutation(resp)
	if string(origRM.SetHeaders[0].Header.RawValue) != authCanary {
		t.Fatalf("original response was mutated: %q", origRM.SetHeaders[0].Header.RawValue)
	}
}

// TestRedactProcessingResponseMasksAnthropicAPIKey is a focused regression
// guard for the Anthropic credential header: the router injects the upstream
// key under x-api-key, so a debug dump of the ext_proc mutation must mask it to
// [REDACTED] in both the string Value and RawValue forms (CWE-532), while
// leaving the original mutation sent to Envoy untouched.
func TestRedactProcessingResponseMasksAnthropicAPIKey(t *testing.T) {
	const keyValue = "sk-ant-secret-canary"
	const keyRaw = "sk-ant-raw-canary"
	resp := requestBodyResponseWithHeaders(
		&core.HeaderValueOption{Header: &core.HeaderValue{Key: "x-api-key", Value: keyValue}},
		&core.HeaderValueOption{Header: &core.HeaderValue{Key: "X-API-Key", RawValue: []byte(keyRaw)}},
	)

	redacted := redactResponseForLog(resp)
	rm := responseHeaderMutation(redacted)

	for _, opt := range rm.SetHeaders {
		if opt.Header.Value != "" && opt.Header.Value != redactedHeaderValue {
			t.Errorf("x-api-key Value not masked: %q", opt.Header.Value)
		}
		if len(opt.Header.RawValue) > 0 && string(opt.Header.RawValue) != redactedHeaderValue {
			t.Errorf("x-api-key RawValue not masked: %q", opt.Header.RawValue)
		}
	}

	dump := redactedResponseDump(redacted)
	if strings.Contains(dump, "canary") {
		t.Fatalf("redacted dump still contains the key: %s", dump)
	}

	// The original mutation (sent to Envoy) must still carry the real key.
	origRM := responseHeaderMutation(resp)
	if origRM.SetHeaders[0].Header.Value != keyValue {
		t.Fatalf("original x-api-key was mutated: %q", origRM.SetHeaders[0].Header.Value)
	}
}

func TestRedactProcessingResponseNilSafe(t *testing.T) {
	if redactResponseForLog(nil) != nil {
		t.Fatal("nil response must return nil")
	}
}

// headerValueByKey returns the string form (Value or RawValue) of the first
// set-header matching key, or "" if absent.
func headerValueByKey(hm *ext_proc.HeaderMutation, key string) string {
	if hm == nil {
		return ""
	}
	for _, opt := range hm.SetHeaders {
		if opt == nil || opt.Header == nil || opt.Header.Key != key {
			continue
		}
		if opt.Header.Value != "" {
			return opt.Header.Value
		}
		return string(opt.Header.RawValue)
	}
	return ""
}

func redactedResponseDump(r *ext_proc.ProcessingResponse) string {
	rm := responseHeaderMutation(r)
	var b strings.Builder
	if rm != nil {
		for _, opt := range rm.SetHeaders {
			if opt == nil || opt.Header == nil {
				continue
			}
			b.WriteString(opt.Header.Key)
			b.WriteByte('=')
			b.WriteString(opt.Header.Value)
			b.WriteByte('|')
			b.Write(opt.Header.RawValue)
			b.WriteByte('\n')
		}
	}
	return b.String()
}
