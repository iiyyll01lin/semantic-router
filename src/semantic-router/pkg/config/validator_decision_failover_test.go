package config

import "testing"

func TestValidateDecisionAlgorithmConfig_Failover(t *testing.T) {
	tests := []struct {
		name      string
		algorithm *AlgorithmConfig
		wantErr   bool
	}{
		{
			name:      "failover without block defaults are valid",
			algorithm: &AlgorithmConfig{Type: "failover"},
			wantErr:   false,
		},
		{
			name: "failover on_error skip is valid",
			algorithm: &AlgorithmConfig{
				Type:     "failover",
				Failover: &FailoverAlgorithmConfig{OnError: "skip"},
			},
			wantErr: false,
		},
		{
			name: "failover on_error fail is valid",
			algorithm: &AlgorithmConfig{
				Type:     "failover",
				Failover: &FailoverAlgorithmConfig{OnError: "fail"},
			},
			wantErr: false,
		},
		{
			name: "failover on_error unknown is rejected",
			algorithm: &AlgorithmConfig{
				Type:     "failover",
				Failover: &FailoverAlgorithmConfig{OnError: "retry"},
			},
			wantErr: true,
		},
		{
			name: "failover type with mismatched confidence block is rejected",
			algorithm: &AlgorithmConfig{
				Type:       "failover",
				Confidence: &ConfidenceAlgorithmConfig{},
			},
			wantErr: true,
		},
		{
			name: "non-failover type with failover block is rejected",
			algorithm: &AlgorithmConfig{
				Type:     "confidence",
				Failover: &FailoverAlgorithmConfig{OnError: "skip"},
			},
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateDecisionAlgorithmConfig("test-decision", nil, tt.algorithm)
			if (err != nil) != tt.wantErr {
				t.Fatalf("validateDecisionAlgorithmConfig() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

func TestIsSupportedDecisionAlgorithmType_Failover(t *testing.T) {
	if !IsSupportedDecisionAlgorithmType("failover") {
		t.Fatal("failover should be a supported decision algorithm type")
	}
	if tier := GetAlgorithmTier("failover"); tier != "supported" {
		t.Fatalf("failover tier = %q, want supported", tier)
	}
}
