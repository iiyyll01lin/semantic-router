package extproc

import (
	"time"

	ext_proc "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"go.opentelemetry.io/otel/attribute"

	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/cache"
	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/config"
	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/observability/logging"
	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/observability/metrics"
	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/observability/tracing"
	"github.com/vllm-project/semantic-router/src/semantic-router/pkg/utils/http"
)

// decisionWillPersonalize checks whether the matched decision is configured
// with plugins (RAG, memory) that inject user-specific context. When true,
// we skip the entire cache path — both reads and writes — because:
//   - reads would serve a generic cached answer instead of the personalized one
//   - writes would cache a personalized answer that could leak to other users
//
// This avoids orphaned pending cache entries and unnecessary embedding work.
func decisionWillPersonalize(ctx *RequestContext, cfg *config.RouterConfig) bool {
	d := ctx.VSRSelectedDecision
	if d == nil {
		return false
	}
	if ragCfg := d.GetRAGConfig(); ragCfg != nil && ragCfg.Enabled {
		return true
	}
	// Per-decision memory plugin takes priority over global setting.
	if memCfg := d.GetMemoryConfig(); memCfg != nil {
		return memCfg.Enabled
	}
	if cfg != nil && cfg.Memory.Enabled {
		return true
	}
	return false
}

// handleCaching handles cache lookup and storage with category-specific settings
func (r *OpenAIRouter) handleCaching(ctx *RequestContext, categoryName string) (*ext_proc.ProcessingResponse, bool) {
	// Skip entire cache path for decisions that will inject user-specific context.
	// Both reads (would serve stale generic answers) and writes (would leak
	// personalized data) are wrong when RAG or memory is enabled.
	if decisionWillPersonalize(ctx, r.Config) {
		logging.Debugf("[Cache] Skipping cache for decision '%s': RAG or memory enabled", categoryName)
		return nil, false
	}

	if ctx.LooperRequest {
		return r.handleLooperCacheSkip(ctx, categoryName)
	}

	requestModel, requestQuery, err := cache.ExtractQueryFromOpenAIRequest(ctx.OriginalRequestBody)
	if err != nil {
		logging.Errorf("Error extracting query from request: %v", err)
		return nil, false
	}

	ctx.RequestModel = requestModel
	ctx.RequestQuery = requestQuery
	ctx.CacheQuery = cache.ScopeQueryToUser(requestQuery, cacheScopeUserID(ctx))

	cacheEnabled := r.semanticCacheEnabledForScope(categoryName)

	if response, shouldReturn := r.performCacheLookup(ctx, categoryName, requestModel, cacheEnabled); shouldReturn {
		return response, true
	}

	r.storePendingCacheRequest(ctx, categoryName, requestModel, cacheEnabled)

	return nil, false
}

// tryExactCacheShortCircuit performs a pre-routing EXACT-match cache lookup.
// On an identical repeat prompt it returns a cache-hit response immediately,
// skipping signal evaluation (mmBERT embedding + classifier fan-out) and routing
// entirely — the ~0.7 s router tax collapses to tens of ms. Returns nil on
// miss / disabled / unsupported backend so the normal pipeline continues.
//
// This runs BEFORE the routing decision is known, which is safe for EXACT
// matches specifically:
//   - an identical prompt yields the identical routing decision, hence the
//     identical cache scope and cache-enablement, so serving the stored answer
//     is consistent with the per-decision policy that stored it;
//   - the write path (handleCaching) never stores personalized (RAG/memory)
//     responses, so an exact hit can only ever replay a non-personalized answer
//     for the same prompt and user scope.
//
// Semantic/paraphrase hits are intentionally left on the per-decision path
// (handleCaching, after routing) so near-matches cannot cross decision
// boundaries — preserving routing correctness.
func (r *OpenAIRouter) tryExactCacheShortCircuit(ctx *RequestContext) *ext_proc.ProcessingResponse {
	if r.Cache == nil || !r.Cache.IsEnabled() {
		return nil
	}
	matcher, ok := r.Cache.(cache.ExactMatcher)
	if !ok {
		return nil
	}
	if ctx.LooperRequest {
		return nil
	}

	requestModel, requestQuery, err := cache.ExtractQueryFromOpenAIRequest(ctx.OriginalRequestBody)
	if err != nil || requestQuery == "" {
		return nil
	}
	cacheQuery := cache.ScopeQueryToUser(requestQuery, cacheScopeUserID(ctx))

	spanCtx, span := tracing.StartPluginSpan(ctx.TraceContext, "exact-cache", "")
	startTime := time.Now()
	cachedResponse, found, cacheErr := matcher.FindExact(requestModel, cacheQuery)
	lookupTime := time.Since(startTime).Milliseconds()

	if cacheErr != nil {
		logging.Errorf("Error in exact cache lookup: %v", cacheErr)
		tracing.RecordError(span, cacheErr)
		tracing.EndPluginSpan(span, "error", lookupTime, "lookup_failed")
		ctx.TraceContext = spanCtx
		return nil
	}
	if !found {
		tracing.EndPluginSpan(span, "success", lookupTime, "cache_miss")
		ctx.TraceContext = spanCtx
		return nil
	}

	// Exact hit: replay the cached response, skipping embed + classify + routing.
	ctx.RequestModel = requestModel
	ctx.RequestQuery = requestQuery
	ctx.CacheQuery = cacheQuery
	ctx.VSRCacheHit = true
	ctx.VSRCacheSimilarity = 1.0

	metrics.RecordCachePluginHit("", "exact-cache")
	tracing.SetSpanAttributes(span,
		attribute.String(tracing.AttrCacheKey, requestQuery),
		attribute.Bool(tracing.AttrCacheHit, true),
		attribute.Int64(tracing.AttrCacheLookupTimeMs, lookupTime),
		attribute.Float64("cache.similarity", 1.0))
	tracing.EndPluginSpan(span, "success", lookupTime, "cache_hit")

	r.startRouterReplay(ctx, requestModel, requestModel, "")
	logging.LogEvent("exact_cache_hit", map[string]interface{}{
		"request_id": ctx.RequestID,
		"model":      requestModel,
		"query":      requestQuery,
	})
	response := http.CreateCacheHitResponse(cachedResponse, ctx.ExpectStreamingResponse, "", ctx.VSRSelectedDecisionName, ctx.VSRMatchedKeywords, ctx.VSRCacheSimilarity)
	r.updateRouterReplayStatus(ctx, 200, ctx.ExpectStreamingResponse)
	r.attachRouterReplayResponse(ctx, cachedResponse, true)
	ctx.TraceContext = spanCtx
	return response
}

// handleLooperCacheSkip extracts the query for a looper request (skipping read)
// and registers a pending cache write if caching is enabled.
func (r *OpenAIRouter) handleLooperCacheSkip(ctx *RequestContext, categoryName string) (*ext_proc.ProcessingResponse, bool) {
	logging.Debugf("[Cache] Skipping cache read for looper internal request")

	requestModel, requestQuery, err := cache.ExtractQueryFromOpenAIRequest(ctx.OriginalRequestBody)
	if err != nil {
		logging.Errorf("Error extracting query from request: %v", err)
		return nil, false
	}
	ctx.RequestModel = requestModel
	ctx.RequestQuery = requestQuery
	ctx.CacheQuery = cache.ScopeQueryToUser(requestQuery, cacheScopeUserID(ctx))

	cacheEnabled := r.semanticCacheEnabledForScope(categoryName)
	r.storePendingCacheRequest(ctx, categoryName, requestModel, cacheEnabled)
	return nil, false
}

// storePendingCacheRequest adds a pending cache request if caching is enabled for this decision.
func (r *OpenAIRouter) storePendingCacheRequest(ctx *RequestContext, categoryName, requestModel string, cacheEnabled bool) {
	cacheQuery := cacheQueryForContext(ctx)
	if cacheQuery == "" || !r.Cache.IsEnabled() || !cacheEnabled {
		return
	}
	ttlSeconds := r.Config.GetCacheTTLSecondsForDecision(categoryName)
	if err := r.Cache.AddPendingRequest(ctx.RequestID, requestModel, cacheQuery, ctx.OriginalRequestBody, ttlSeconds); err != nil {
		logging.Errorf("Error adding pending request to cache: %v", err)
	}
}

// performCacheLookup searches for a cached response matching the request query.
// Returns the cached response and true on cache hit, or nil and false on miss/error/skip.
func (r *OpenAIRouter) performCacheLookup(
	ctx *RequestContext, categoryName, requestModel string, cacheEnabled bool,
) (*ext_proc.ProcessingResponse, bool) {
	cacheQuery := cacheQueryForContext(ctx)
	if cacheQuery == "" || !r.Cache.IsEnabled() || !cacheEnabled {
		return nil, false
	}

	threshold := r.Config.GetCacheSimilarityThreshold()
	if categoryName != "" {
		threshold = r.Config.GetCacheSimilarityThresholdForDecision(categoryName)
	}

	logging.Infof("handleCaching: Performing cache lookup - model=%s, query='%s', threshold=%.2f",
		requestModel, ctx.RequestQuery, threshold)

	spanCtx, span := tracing.StartPluginSpan(ctx.TraceContext, "semantic-cache", categoryName)

	startTime := time.Now()
	cachedResponse, found, cacheErr := r.Cache.FindSimilarWithThreshold(requestModel, cacheQuery, threshold)
	lookupTime := time.Since(startTime).Milliseconds()

	logging.Infof("FindSimilarWithThreshold returned: found=%v, error=%v, lookupTime=%dms", found, cacheErr, lookupTime)

	tracing.SetSpanAttributes(span,
		attribute.String(tracing.AttrCacheKey, ctx.RequestQuery),
		attribute.Bool(tracing.AttrCacheHit, found),
		attribute.Int64(tracing.AttrCacheLookupTimeMs, lookupTime),
		attribute.String(tracing.AttrCategoryName, categoryName),
		attribute.Float64("cache.threshold", float64(threshold)))

	if cacheErr != nil {
		logging.Errorf("Error searching cache: %v", cacheErr)
		tracing.RecordError(span, cacheErr)
		tracing.EndPluginSpan(span, "error", lookupTime, "lookup_failed")
	} else if found {
		ctx.VSRCacheHit = true
		ctx.VSRCacheSimilarity = r.Cache.LastSimilarity()

		if categoryName != "" {
			ctx.VSRSelectedDecisionName = categoryName
		}

		metrics.RecordCachePluginHit(categoryName, "semantic-cache")
		tracing.EndPluginSpan(span, "success", lookupTime, "cache_hit")

		r.startRouterReplay(ctx, requestModel, requestModel, categoryName)
		logging.LogEvent("cache_hit", map[string]interface{}{
			"request_id": ctx.RequestID,
			"model":      requestModel,
			"query":      ctx.RequestQuery,
			"category":   categoryName,
			"threshold":  threshold,
		})
		response := http.CreateCacheHitResponse(cachedResponse, ctx.ExpectStreamingResponse, categoryName, ctx.VSRSelectedDecisionName, ctx.VSRMatchedKeywords, ctx.VSRCacheSimilarity)
		r.updateRouterReplayStatus(ctx, 200, ctx.ExpectStreamingResponse)
		r.attachRouterReplayResponse(ctx, cachedResponse, true)
		ctx.TraceContext = spanCtx
		return response, true
	} else {
		ctx.VSRCacheSimilarity = r.Cache.LastSimilarity()
		metrics.RecordCachePluginMiss(categoryName, "semantic-cache")
		tracing.EndPluginSpan(span, "success", lookupTime, "cache_miss")
	}
	ctx.TraceContext = spanCtx

	return nil, false
}

func cacheQueryForContext(ctx *RequestContext) string {
	if ctx == nil {
		return ""
	}
	if ctx.CacheQuery != "" {
		return ctx.CacheQuery
	}
	return ctx.RequestQuery
}
