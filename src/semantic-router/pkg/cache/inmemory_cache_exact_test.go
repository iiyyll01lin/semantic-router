//go:build !windows && cgo

package cache

import (
	"testing"
	"time"
)

// newExactCacheForTest builds an enabled in-memory cache whose only expiry gate
// is the per-entry ExpiresAt field (global TTL disabled). FindExact never
// touches embeddings, so entries are appended directly and the tests stay
// deterministic without a loaded embedding model.
func newExactCacheForTest(t *testing.T) *InMemoryCache {
	t.Helper()
	c := NewInMemoryCache(InMemoryCacheOptions{
		Enabled:        true,
		MaxEntries:     100,
		TTLSeconds:     0, // never expire via global TTL; ExpiresAt drives expiry
		EmbeddingModel: "mmbert",
	})
	t.Cleanup(func() { _ = c.Close() })
	return c
}

// TestFindExactReplaysStoredResponse verifies the fast path: a byte-for-byte
// repeat of a previously answered prompt replays the stored response, reports a
// hit, and records the perfect (1.0) similarity that an exact match implies.
func TestFindExactReplaysStoredResponse(t *testing.T) {
	c := newExactCacheForTest(t)

	const query = "explain mitosis versus meiosis in eukaryotic cells in great detail"
	response := []byte(`{"choices":[{"message":{"content":"exact-hit"}}]}`)
	c.entries = append(c.entries, CacheEntry{
		RequestID:    "exact-1",
		Query:        query,
		ResponseBody: response,
		Timestamp:    time.Now(),
		LastAccessAt: time.Now(),
	})

	got, found, err := c.FindExact("test-model", query)
	if err != nil {
		t.Fatalf("FindExact returned error: %v", err)
	}
	if !found {
		t.Fatal("expected an exact hit for the stored query")
	}
	if string(got) != string(response) {
		t.Fatalf("exact hit returned wrong body: got %q want %q", got, response)
	}
	if sim := c.LastSimilarity(); sim != 1.0 {
		t.Fatalf("exact hit must record similarity 1.0, got %v", sim)
	}
}

// TestFindExactMissOnUnseenQuery verifies that a query which was never stored
// returns a clean miss rather than a fuzzy neighbor (FindExact does no semantic
// matching).
func TestFindExactMissOnUnseenQuery(t *testing.T) {
	c := newExactCacheForTest(t)

	c.entries = append(c.entries, CacheEntry{
		RequestID:    "exact-1",
		Query:        "what is the boiling point of water at sea level",
		ResponseBody: []byte(`{"choices":[{"message":{"content":"100C"}}]}`),
		Timestamp:    time.Now(),
		LastAccessAt: time.Now(),
	})

	got, found, err := c.FindExact("test-model", "what is the boiling point of water on everest")
	if err != nil {
		t.Fatalf("FindExact returned error: %v", err)
	}
	if found {
		t.Fatalf("expected a miss for an unseen query, got body %q", got)
	}
	if got != nil {
		t.Fatalf("miss must return a nil body, got %q", got)
	}
}

// TestFindExactEmptyQueryShortCircuits verifies the empty-query guard returns
// (nil, false, nil) without scanning entries.
func TestFindExactEmptyQueryShortCircuits(t *testing.T) {
	c := newExactCacheForTest(t)

	c.entries = append(c.entries, CacheEntry{
		RequestID:    "exact-1",
		Query:        "",
		ResponseBody: []byte(`{"choices":[{"message":{"content":"should-not-return"}}]}`),
		Timestamp:    time.Now(),
		LastAccessAt: time.Now(),
	})

	got, found, err := c.FindExact("test-model", "")
	if err != nil {
		t.Fatalf("FindExact returned error: %v", err)
	}
	if found || got != nil {
		t.Fatalf("empty query must short-circuit to (nil,false,nil), got (%q,%v)", got, found)
	}
}

// TestFindExactDisabledCacheShortCircuits verifies a disabled cache never
// serves a response even when a matching entry exists.
func TestFindExactDisabledCacheShortCircuits(t *testing.T) {
	c := NewInMemoryCache(InMemoryCacheOptions{
		Enabled:        false,
		MaxEntries:     100,
		EmbeddingModel: "mmbert",
	})
	t.Cleanup(func() { _ = c.Close() })

	const query = "explain the CAP theorem"
	c.entries = append(c.entries, CacheEntry{
		RequestID:    "exact-1",
		Query:        query,
		ResponseBody: []byte(`{"choices":[{"message":{"content":"CAP"}}]}`),
		Timestamp:    time.Now(),
		LastAccessAt: time.Now(),
	})

	got, found, err := c.FindExact("test-model", query)
	if err != nil {
		t.Fatalf("FindExact returned error: %v", err)
	}
	if found || got != nil {
		t.Fatalf("disabled cache must short-circuit to (nil,false,nil), got (%q,%v)", got, found)
	}
}

// TestFindExactSkipsExpiredEntry verifies the expiry gate: an entry whose
// per-entry ExpiresAt is in the past is not returned, while the identical entry
// with a future ExpiresAt is.
func TestFindExactSkipsExpiredEntry(t *testing.T) {
	c := newExactCacheForTest(t)

	const query = "summarize the theory of general relativity"
	response := []byte(`{"choices":[{"message":{"content":"spacetime"}}]}`)
	c.entries = append(c.entries, CacheEntry{
		RequestID:    "exact-1",
		Query:        query,
		ResponseBody: response,
		Timestamp:    time.Now().Add(-time.Hour),
		LastAccessAt: time.Now().Add(-time.Hour),
		ExpiresAt:    time.Now().Add(-time.Minute), // already expired
	})

	if _, found, err := c.FindExact("test-model", query); err != nil {
		t.Fatalf("FindExact returned error: %v", err)
	} else if found {
		t.Fatal("expired entry must not be returned")
	}

	// Refresh the same entry's expiry into the future: it now becomes eligible.
	c.entries[0].ExpiresAt = time.Now().Add(time.Hour)
	got, found, err := c.FindExact("test-model", query)
	if err != nil {
		t.Fatalf("FindExact returned error: %v", err)
	}
	if !found {
		t.Fatal("entry with a future ExpiresAt must be returned")
	}
	if string(got) != string(response) {
		t.Fatalf("unexpired hit returned wrong body: got %q want %q", got, response)
	}
}

// TestFindExactReturnsNewestDuplicate verifies the newest-first scan: when two
// entries carry the identical query, FindExact replays the freshest (most
// recently appended) response rather than a stale earlier one.
func TestFindExactReturnsNewestDuplicate(t *testing.T) {
	c := newExactCacheForTest(t)

	const query = "list the primary colors"
	stale := []byte(`{"choices":[{"message":{"content":"stale"}}]}`)
	fresh := []byte(`{"choices":[{"message":{"content":"fresh"}}]}`)

	c.entries = append(c.entries,
		CacheEntry{
			RequestID:    "exact-old",
			Query:        query,
			ResponseBody: stale,
			Timestamp:    time.Now().Add(-time.Minute),
			LastAccessAt: time.Now().Add(-time.Minute),
		},
		CacheEntry{
			RequestID:    "exact-new",
			Query:        query,
			ResponseBody: fresh,
			Timestamp:    time.Now(),
			LastAccessAt: time.Now(),
		},
	)

	got, found, err := c.FindExact("test-model", query)
	if err != nil {
		t.Fatalf("FindExact returned error: %v", err)
	}
	if !found {
		t.Fatal("expected a hit when duplicate entries exist")
	}
	if string(got) != string(fresh) {
		t.Fatalf("duplicate lookup must return the freshest body: got %q want %q", got, fresh)
	}
}

// TestFindExactEnforcesUserScope verifies user-scope isolation: an exact entry
// stored under one user's scope is never replayed for another user's identical
// base prompt, while the owning user still gets an exact hit. This mirrors the
// hard scope gate proven for the similarity path in
// TestSearchEnforcesUserScope, but on the pre-routing exact path.
func TestFindExactEnforcesUserScope(t *testing.T) {
	c := newExactCacheForTest(t)

	const base = "explain mitosis versus meiosis in eukaryotic cells in great detail"
	aliceQuery := ScopeQueryToUser(base, "alice")
	bobQuery := ScopeQueryToUser(base, "bob")
	aliceResponse := []byte(`{"choices":[{"message":{"content":"alice-only"}}]}`)

	c.entries = append(c.entries, CacheEntry{
		RequestID:    "alice-1",
		Query:        aliceQuery,
		ResponseBody: aliceResponse,
		Timestamp:    time.Now(),
		LastAccessAt: time.Now(),
	})

	// Bob asks the identical base question (scoped to bob) → must NOT be served
	// alice's answer.
	if got, found, err := c.FindExact("test-model", bobQuery); err != nil {
		t.Fatalf("FindExact returned error: %v", err)
	} else if found {
		t.Fatalf("cross-tenant leak: bob was served alice's entry (%q)", got)
	}

	// The unscoped/anonymous base query must not reach a scoped entry either.
	if _, found, err := c.FindExact("test-model", base); err != nil {
		t.Fatalf("FindExact returned error: %v", err)
	} else if found {
		t.Fatal("unscoped query must not match a scoped entry")
	}

	// Alice repeats her own scoped query → exact hit with her response.
	got, found, err := c.FindExact("test-model", aliceQuery)
	if err != nil {
		t.Fatalf("FindExact returned error: %v", err)
	}
	if !found {
		t.Fatal("alice must get an exact hit for her own scoped query")
	}
	if string(got) != string(aliceResponse) {
		t.Fatalf("alice hit returned wrong body: got %q want %q", got, aliceResponse)
	}
}
