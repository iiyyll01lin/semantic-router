package config

import (
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

var _ = Describe("globalModelSelectionMethodIgnored", func() {
	It("warns when a non-static global method has no per-decision algorithm block", func() {
		cfg := &RouterConfig{}
		cfg.ModelSelection.Method = "session_aware"
		cfg.Decisions = []Decision{
			{Name: "no-algo"},
			{Name: "empty-algo", Algorithm: &AlgorithmConfig{Type: ""}},
		}
		Expect(globalModelSelectionMethodIgnored(cfg)).To(BeTrue())
	})

	It("does not warn when at least one decision carries an algorithm block", func() {
		cfg := &RouterConfig{}
		cfg.ModelSelection.Method = "session_aware"
		cfg.Decisions = []Decision{
			{Name: "no-algo"},
			{Name: "has-algo", Algorithm: &AlgorithmConfig{Type: "session_aware"}},
		}
		Expect(globalModelSelectionMethodIgnored(cfg)).To(BeFalse())
	})

	It("does not warn when the global method is static", func() {
		cfg := &RouterConfig{}
		cfg.ModelSelection.Method = "static"
		cfg.Decisions = []Decision{{Name: "no-algo"}}
		Expect(globalModelSelectionMethodIgnored(cfg)).To(BeFalse())
	})

	It("does not warn when the global method is empty", func() {
		cfg := &RouterConfig{}
		cfg.Decisions = []Decision{{Name: "no-algo"}}
		Expect(globalModelSelectionMethodIgnored(cfg)).To(BeFalse())
	})
})
