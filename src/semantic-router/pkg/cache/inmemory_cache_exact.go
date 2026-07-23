//go:build !windows && cgo

package cache

import (
	"sync/atomic"
	"time"

	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/observability/metrics"
)

// FindExact returns a cached response for an EXACT (byte-for-byte) match of the
// scoped query, without generating an embedding. It is the fast pre-routing
// path: an identical repeat prompt is served in tens of microseconds, skipping
// the mmBERT embedding + classifier fan-out entirely (the ~0.7 s signal.evaluation
// stage). Semantic (paraphrase) hits still go through the per-decision
// FindSimilarWithThreshold path after routing.
//
// Correctness: only entries that already carry a stored response and that pass
// the same hard user-scope + expiry gate as the similarity search are eligible
// (entryEligible). Because the write path never stores personalized (RAG/memory)
// responses, an exact hit can only ever replay a non-personalized answer for the
// identical prompt from the same user scope.
func (c *InMemoryCache) FindExact(model string, query string) ([]byte, bool, error) {
	start := time.Now()

	if !c.enabled || query == "" {
		return nil, false, nil
	}

	scopeNamespace := CacheScopeNamespaceOf(query)

	c.mu.RLock()
	now := time.Now()
	var response []byte
	found := false
	// Newest-first: return the freshest exact match if duplicates exist.
	for i := len(c.entries) - 1; i >= 0; i-- {
		entry := c.entries[i]
		if entry.Query != query {
			continue
		}
		ok, _ := c.entryEligible(entry, scopeNamespace, now)
		if !ok {
			continue
		}
		response = entry.ResponseBody
		found = true
		break
	}
	c.mu.RUnlock()

	if found {
		c.StoreSimilarity(1.0)
		atomic.AddInt64(&c.hitCount, 1)
		metrics.RecordCacheOperation("memory", "find_exact", "hit", time.Since(start).Seconds())
		return response, true, nil
	}

	atomic.AddInt64(&c.missCount, 1)
	metrics.RecordCacheOperation("memory", "find_exact", "miss", time.Since(start).Seconds())
	return nil, false, nil
}
