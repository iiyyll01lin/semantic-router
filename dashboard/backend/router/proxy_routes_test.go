package router

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/vllm-project/semantic-router/dashboard/backend/config"
)

func TestConfigureEnvoyProxyStripsBrowserOriginBeforeUpstream(t *testing.T) {
	t.Parallel()

	originSeen := "sentinel"
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		originSeen = r.Header.Get("Origin")
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	defer upstream.Close()

	envoyProxy := configureEnvoyProxy(&config.Config{EnvoyURL: upstream.URL})
	if envoyProxy == nil {
		t.Fatal("configureEnvoyProxy returned nil")
	}

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	req.Header.Set("Origin", "http://10.96.30.46:8700")
	recorder := httptest.NewRecorder()
	envoyProxy.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusOK)
	}
	// Upstream (e.g. ollama) must not receive a browser Origin; otherwise its
	// CORS allow-list rejects the request with an empty-body 403.
	if originSeen != "" {
		t.Fatalf("upstream Origin = %q, want empty", originSeen)
	}
	// The browser must still get a CORS grant derived from X-Forwarded-Origin.
	if got := recorder.Header().Get("Access-Control-Allow-Origin"); got != "http://10.96.30.46:8700" {
		t.Fatalf("Access-Control-Allow-Origin = %q, want %q", got, "http://10.96.30.46:8700")
	}
}

func TestRegisterFleetSimRoutesReturnsBadGatewayWhenDisabled(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	registerFleetSimRoutes(mux, &config.Config{})

	req := httptest.NewRequest(http.MethodGet, "/api/fleet-sim/api/workloads", nil)
	recorder := httptest.NewRecorder()
	mux.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusBadGateway)
	}
}

func TestRegisterFleetSimRoutesProxiesSimulatorPaths(t *testing.T) {
	t.Parallel()

	var proxiedPath string
	var forwardedPrefix string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		proxiedPath = r.URL.Path
		forwardedPrefix = r.Header.Get("X-Forwarded-Prefix")
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	defer server.Close()

	mux := http.NewServeMux()
	registerFleetSimRoutes(mux, &config.Config{FleetSimURL: server.URL})

	req := httptest.NewRequest(http.MethodGet, "/api/fleet-sim/api/workloads", nil)
	recorder := httptest.NewRecorder()
	mux.ServeHTTP(recorder, req)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusOK)
	}
	if proxiedPath != "/api/workloads" {
		t.Fatalf("proxied path = %q, want %q", proxiedPath, "/api/workloads")
	}
	if forwardedPrefix != "/api/fleet-sim" {
		t.Fatalf("forwarded prefix = %q, want %q", forwardedPrefix, "/api/fleet-sim")
	}
}
