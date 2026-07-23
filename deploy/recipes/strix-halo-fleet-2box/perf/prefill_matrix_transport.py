"""Prompt construction, streaming transports, and metric reduction for prefill matrices."""

from __future__ import annotations

import json
import statistics
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from hashlib import sha256
from itertools import pairwise
from typing import Any

SUCCESS_MARKER = "MATRIX_OK"
HTTP_OK = 200
HTTP_REDIRECT_START = 300
MIN_DECODE_EVENTS = 2
PROMPT_SHARED_OVERHEAD_TOKENS = 48
PROMPT_IDENTITY_WORDS = 24
CACHED_TOKEN_PATHS = (
    (
        "usage.prompt_tokens_details.cached_tokens",
        ("prompt_tokens_details", "cached_tokens"),
    ),
    (
        "usage.input_tokens_details.cached_tokens",
        ("input_tokens_details", "cached_tokens"),
    ),
    ("usage.cached_tokens", ("cached_tokens",)),
    ("usage.prompt_cache_hit_tokens", ("prompt_cache_hit_tokens",)),
)
METRIC_PREFIXES = ("vllm:",)


def build_prompt(
    filler_units: int,
    reuse_percent: int,
    cell_id: str,
    cohort: str,
    request_index: int,
    *,
    trial_index: int = 0,
    target_tokens: int | None = None,
    namespace: str = "measure",
) -> str:
    """Build a deterministic prompt from a backend-calibrated filler count.

    ``filler_units`` is deliberately not called a token count. The matrix first
    calibrates it against authoritative backend usage, then uses the same count
    for measured requests. Each cold/warm pair gets a unique fixed-width
    identity. Reuse cohorts share only their requested leading prefix; zero
    reuse includes the cohort in the identity so every prompt stays distinct.
    """
    filler_units = max(1, filler_units)
    reuse_basis = target_tokens if target_tokens is not None else filler_units
    shared_count = min(
        filler_units,
        max(
            0,
            int(reuse_basis * reuse_percent / 100) - PROMPT_SHARED_OVERHEAD_TOKENS,
        ),
    )
    unique_count = filler_units - shared_count
    pair_scope = f"{cell_id}|trial={trial_index}|request={request_index}"
    if not shared_count:
        pair_scope += f"|cohort={cohort}"
    identity = _fixed_width_identity(pair_scope)
    cache_buster = (
        "A"
        if namespace.startswith("calibration")
        else "B" if namespace == "measure" else "C"
    )
    prefix = f"{cache_buster}\n{identity}\n" + ("alpha " * shared_count)
    cohort_word = "alpha" if cohort == "cold" else "bravo"
    variant = f"\nCohort {cohort_word}.\n" if shared_count else ""
    instruction = (
        f"\nIgnore all filler and answer exactly {SUCCESS_MARKER}. Do not explain.\n"
    )
    return prefix + variant + ("alpha " * unique_count) + instruction


def _fixed_width_identity(scope: str) -> str:
    digest = int.from_bytes(sha256(scope.encode()).digest(), "big")
    words = [
        "alpha" if (digest >> bit) & 1 else "bravo"
        for bit in range(PROMPT_IDENTITY_WORDS)
    ]
    return " ".join(words)


def request_once(
    api: str,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
    api_key: str = "",
    num_ctx: int = 0,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if api == "ollama":
        return request_ollama(
            base_url,
            model,
            prompt,
            max_tokens,
            timeout,
            num_ctx=num_ctx,
        )
    return request_openai(
        base_url,
        model,
        prompt,
        max_tokens,
        timeout,
        api_key=api_key,
        extra_body=extra_body,
    )


def request_openai(
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
    api_key: str = "",
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if extra_body:
        payload.update(extra_body)
    encoded = _encode_payload(payload)
    request_facts = _request_facts(
        encoded=encoded,
        prompt=prompt,
        model=model,
        max_tokens=max_tokens,
    )
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=encoded,
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    started_unix = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = parse_openai_stream(
                response,
                started,
                int(response.status),
                deadline_s=timeout,
            )
            response_headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower().startswith("x-vsr-")
            }
            result["response_headers"] = response_headers
            result["semantic_cache_hit"] = _header_truth(
                response_headers.get("x-vsr-cache-hit")
            )
            return _attach_request_facts(result, request_facts, started_unix)
    except urllib.error.HTTPError as exc:
        payload_text = exc.read().decode("utf-8", "replace")
        result = failed_request(exc.code, started, payload_text)
    except (
        json.JSONDecodeError,
        OSError,
        TimeoutError,
        ValueError,
        urllib.error.URLError,
    ) as exc:
        result = failed_request(0, started, f"{type(exc).__name__}: {exc}")
    return _attach_request_facts(result, request_facts, started_unix)


def request_ollama(
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
    num_ctx: int = 0,
) -> dict[str, Any]:
    options: dict[str, Any] = {"temperature": 0, "num_predict": max_tokens}
    if num_ctx > 0:
        options["num_ctx"] = num_ctx
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "think": False,
        "options": options,
    }
    encoded = _encode_payload(payload)
    request_facts = _request_facts(
        encoded=encoded,
        prompt=prompt,
        model=model,
        max_tokens=max_tokens,
    )
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/generate",
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    started_unix = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = parse_ollama_stream(
                response,
                started,
                int(response.status),
                deadline_s=timeout,
            )
            return _attach_request_facts(result, request_facts, started_unix)
    except urllib.error.HTTPError as exc:
        payload_text = exc.read().decode("utf-8", "replace")
        result = failed_request(exc.code, started, payload_text)
    except (
        json.JSONDecodeError,
        OSError,
        TimeoutError,
        ValueError,
        urllib.error.URLError,
    ) as exc:
        result = failed_request(0, started, f"{type(exc).__name__}: {exc}")
    return _attach_request_facts(result, request_facts, started_unix)


def parse_openai_stream(
    lines: Iterable[bytes | str],
    started: float,
    status: int = 200,
    deadline_s: float = 0,
) -> dict[str, Any]:
    (
        content,
        reasoning,
        event_times,
        usage,
        extensions,
        ended,
    ) = _consume_openai_events(lines, started, deadline_s)
    text = "".join(content)
    timings = extensions.get("timings")
    if not isinstance(timings, dict):
        timings = {}
    (
        prompt_tokens,
        completion_tokens,
        prompt_token_source,
        completion_token_source,
    ) = _openai_usage_counts(usage, timings)
    cached_tokens, cached_present, cached_source = cached_token_observation(usage)
    prefill_duration_ms = _first_number(
        timings.get("prompt_ms"),
        _nested_value(extensions, ("metrics", "prefill_duration_ms")),
        _nested_value(extensions, ("usage", "prefill_duration_ms")),
    )
    prefill_tps = _first_number(
        timings.get("prompt_per_second"),
        _nested_value(extensions, ("metrics", "prefill_tokens_per_second")),
    )
    if not prefill_tps and prompt_tokens and prefill_duration_ms:
        prefill_tps = prompt_tokens / (prefill_duration_ms / 1000)
    decode_tps = _first_number(timings.get("predicted_per_second"))
    if not decode_tps:
        decode_tps = _client_decode_tps(completion_tokens, event_times)
    return successful_request(
        status=status,
        started=started,
        ended=ended,
        event_times=event_times,
        text=text,
        reasoning="".join(reasoning),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_token_source=prompt_token_source,
        completion_token_source=completion_token_source,
        cached_tokens=cached_tokens,
        cached_token_field_present=cached_present,
        cached_token_source=cached_source,
        prefill_duration_ms=prefill_duration_ms,
        prefill_tps=prefill_tps,
        decode_tps=decode_tps,
        raw_extensions=extensions,
    )


def _consume_openai_events(
    lines: Iterable[bytes | str],
    started: float,
    deadline_s: float,
) -> tuple[
    list[str],
    list[str],
    list[float],
    dict[str, Any],
    dict[str, Any],
    float,
]:
    content: list[str] = []
    reasoning: list[str] = []
    event_times: list[float] = []
    usage: dict[str, Any] = {}
    extensions: dict[str, Any] = {}
    ended = started
    for event in _sse_events(lines):
        ended = time.perf_counter()
        if deadline_s and ended - started > deadline_s:
            raise TimeoutError(f"request deadline {deadline_s}s exceeded")
        _merge_extensions(extensions, event)
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        for choice in _choices(event):
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            piece = delta.get("content")
            thought = delta.get("reasoning") or delta.get("reasoning_content")
            if isinstance(piece, str) and piece:
                content.append(piece)
                event_times.append(ended)
            if isinstance(thought, str) and thought:
                reasoning.append(thought)
                event_times.append(ended)
    return content, reasoning, event_times, usage, extensions, ended


def _openai_usage_counts(
    usage: dict[str, Any],
    timings: dict[str, Any],
) -> tuple[int | None, int | None, str, str]:
    prompt_tokens = _usage_integer(usage, ("prompt_tokens", "input_tokens"))
    completion_tokens = _usage_integer(usage, ("completion_tokens", "output_tokens"))
    prompt_token_source = (
        "openai.usage.prompt_tokens"
        if "prompt_tokens" in usage
        else "openai.usage.input_tokens" if "input_tokens" in usage else ""
    )
    completion_token_source = (
        "openai.usage.completion_tokens"
        if "completion_tokens" in usage
        else "openai.usage.output_tokens" if "output_tokens" in usage else ""
    )
    if prompt_tokens is None:
        prompt_tokens = _integer(timings.get("prompt_n"))
        if prompt_tokens is not None:
            prompt_token_source = "openai.timings.prompt_n"
    if completion_tokens is None:
        completion_tokens = _integer(timings.get("predicted_n"))
        if completion_tokens is not None:
            completion_token_source = "openai.timings.predicted_n"
    return (
        prompt_tokens,
        completion_tokens,
        prompt_token_source,
        completion_token_source,
    )


def parse_ollama_stream(
    lines: Iterable[bytes | str],
    started: float,
    status: int = 200,
    deadline_s: float = 0,
) -> dict[str, Any]:
    content: list[str] = []
    event_times: list[float] = []
    final: dict[str, Any] = {}
    ended = started
    for raw in lines:
        ended = time.perf_counter()
        if deadline_s and ended - started > deadline_s:
            raise TimeoutError(f"request deadline {deadline_s}s exceeded")
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        piece = event.get("response")
        if isinstance(piece, str) and piece:
            content.append(piece)
            event_times.append(ended)
        if event.get("done"):
            final = event
            break
    prompt_tokens = _integer(final.get("prompt_eval_count"))
    completion_tokens = _integer(final.get("eval_count"))
    prompt_duration_ns = _number(final.get("prompt_eval_duration"))
    eval_duration_ns = _number(final.get("eval_duration"))
    return successful_request(
        status=status,
        started=started,
        ended=ended,
        event_times=event_times,
        text="".join(content),
        reasoning=str(final.get("thinking") or ""),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_token_source=(
            "ollama.prompt_eval_count" if prompt_tokens is not None else ""
        ),
        completion_token_source=(
            "ollama.eval_count" if completion_tokens is not None else ""
        ),
        cached_tokens=None,
        cached_token_field_present=False,
        cached_token_source="",
        prefill_duration_ms=(
            prompt_duration_ns / 1e6 if prompt_duration_ns is not None else None
        ),
        prefill_tps=_nanosecond_rate(prompt_tokens, prompt_duration_ns),
        decode_tps=_nanosecond_rate(completion_tokens, eval_duration_ns),
        raw_extensions={
            "load_duration_ms": _nanoseconds_to_ms(final.get("load_duration")),
            "total_duration_ms": _nanoseconds_to_ms(final.get("total_duration")),
            "done": final.get("done"),
            "done_reason": final.get("done_reason"),
            "prompt_eval_count": final.get("prompt_eval_count"),
            "prompt_eval_duration": final.get("prompt_eval_duration"),
            "eval_count": final.get("eval_count"),
            "eval_duration": final.get("eval_duration"),
        },
    )


def successful_request(
    *,
    status: int,
    started: float,
    ended: float,
    event_times: list[float],
    text: str,
    reasoning: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    prompt_token_source: str,
    completion_token_source: str,
    cached_tokens: int | None,
    cached_token_field_present: bool,
    cached_token_source: str,
    prefill_duration_ms: float | None,
    prefill_tps: float | None,
    decode_tps: float | None,
    raw_extensions: dict[str, Any],
) -> dict[str, Any]:
    intervals = [(right - left) * 1000 for left, right in pairwise(event_times)]
    return {
        "status": status,
        "success": HTTP_OK <= status < HTTP_REDIRECT_START,
        "wall_s": max(0.0, ended - started),
        "ttft_ms": ((event_times[0] - started) * 1000 if event_times else None),
        "stream_chunk_itl_ms_mean": (
            statistics.fmean(intervals) if intervals else None
        ),
        "stream_chunk_itl_ms_p95": percentile(intervals, 95),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "prompt_token_source": prompt_token_source,
        "completion_token_source": completion_token_source,
        "cached_tokens": cached_tokens,
        "cached_token_field_present": cached_token_field_present,
        "cached_token_source": cached_token_source,
        "computed_tokens": (
            prompt_tokens - cached_tokens
            if prompt_tokens is not None
            and cached_tokens is not None
            and cached_tokens <= prompt_tokens
            else None
        ),
        "prefill_duration_ms": prefill_duration_ms,
        "prefill_tps": prefill_tps,
        "decode_tps": decode_tps,
        "marker_correct": SUCCESS_MARKER in text,
        "text_excerpt": text[:200],
        "reasoning_excerpt": reasoning[:200],
        "response_sha256": sha256((text + reasoning).encode()).hexdigest(),
        "raw_extensions": raw_extensions,
        "error": "",
    }


def failed_request(status: int, started: float, error: str) -> dict[str, Any]:
    return {
        "status": status,
        "success": False,
        "wall_s": max(0.0, time.perf_counter() - started),
        "ttft_ms": None,
        "stream_chunk_itl_ms_mean": None,
        "stream_chunk_itl_ms_p95": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "prompt_token_source": "",
        "completion_token_source": "",
        "cached_tokens": None,
        "cached_token_field_present": False,
        "cached_token_source": "",
        "computed_tokens": None,
        "prefill_duration_ms": None,
        "prefill_tps": None,
        "decode_tps": None,
        "marker_correct": False,
        "text_excerpt": "",
        "reasoning_excerpt": "",
        "response_sha256": "",
        "response_headers": {},
        "semantic_cache_hit": None,
        "raw_extensions": {},
        "error": error[:1000],
    }


def summarize_requests(
    requests: list[dict[str, Any]], elapsed_s: float
) -> dict[str, Any]:
    successful = [request for request in requests if request.get("success")]
    prompt_tokens = _numeric_values(successful, "prompt_tokens")
    completion_tokens = sum(
        int(request.get("completion_tokens") or 0) for request in successful
    )
    cached_values = _numeric_values(successful, "cached_tokens")
    computed_values = _numeric_values(successful, "computed_tokens")
    gate_passes = sum(bool(request.get("gate_passed")) for request in requests)
    return {
        "requests": len(requests),
        "successes": len(successful),
        "success_rate": (
            round(len(successful) / len(requests), 4) if requests else 0.0
        ),
        "marker_accuracy": (
            round(
                sum(bool(request.get("marker_correct")) for request in successful)
                / len(successful),
                4,
            )
            if successful
            else None
        ),
        "gate_passes": gate_passes,
        "gate_pass_rate": (round(gate_passes / len(requests), 4) if requests else 0.0),
        "elapsed_s": round(elapsed_s, 4),
        "aggregate_output_tps": (
            round(completion_tokens / elapsed_s, 4) if elapsed_s > 0 else None
        ),
        "prompt_tokens": metric_summary(prompt_tokens),
        "completion_tokens": completion_tokens,
        "cached_tokens": (
            sum(int(value) for value in cached_values) if cached_values else None
        ),
        "computed_tokens": (
            sum(int(value) for value in computed_values) if computed_values else None
        ),
        "cached_prompt_ratio": (
            round(sum(cached_values) / sum(prompt_tokens), 6)
            if cached_values and prompt_tokens and sum(prompt_tokens)
            else None
        ),
        "cached_token_field_rate": (
            round(
                sum(
                    bool(request.get("cached_token_field_present"))
                    for request in successful
                )
                / len(successful),
                4,
            )
            if successful
            else 0.0
        ),
        "prompt_usage_field_rate": (
            round(
                sum(request.get("prompt_tokens") is not None for request in successful)
                / len(successful),
                4,
            )
            if successful
            else 0.0
        ),
        "completion_usage_field_rate": (
            round(
                sum(
                    request.get("completion_tokens") is not None
                    for request in successful
                )
                / len(successful),
                4,
            )
            if successful
            else 0.0
        ),
        "prompt_token_sources": counts(
            str(request.get("prompt_token_source") or "missing")
            for request in successful
        ),
        "semantic_cache_hits": sum(
            request.get("semantic_cache_hit") is True for request in successful
        ),
        "unique_prompt_hashes": len(
            {
                str(request.get("prompt_sha256"))
                for request in requests
                if request.get("prompt_sha256")
            }
        ),
        "unique_payload_hashes": len(
            {
                str(request.get("payload_sha256"))
                for request in requests
                if request.get("payload_sha256")
            }
        ),
        "ttft_ms": metric_summary(_numeric_values(successful, "ttft_ms")),
        "prefill_duration_ms": metric_summary(
            _numeric_values(successful, "prefill_duration_ms")
        ),
        "prefill_tps": metric_summary(_numeric_values(successful, "prefill_tps")),
        "decode_tps": metric_summary(_numeric_values(successful, "decode_tps")),
        "stream_chunk_itl_ms": metric_summary(
            _numeric_values(successful, "stream_chunk_itl_ms_mean")
        ),
        "errors": counts(
            str(request.get("error")) for request in requests if request.get("error")
        ),
        "gate_failures": counts(
            str(check.get("name") or "unknown")
            for request in requests
            for check in request.get("gates") or []
            if check.get("status") == "fail"
        ),
    }


def fetch_metrics(url: str, timeout: float) -> dict[str, float]:
    if not url:
        return {}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
    except (OSError, urllib.error.URLError):
        return {}
    return parse_prometheus_metrics(text)


def parse_prometheus_metrics(text: str) -> dict[str, float]:
    samples: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or not line.startswith(METRIC_PREFIXES):
            continue
        identity, separator, raw_value = line.rpartition(" ")
        if not separator:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        name = identity.split("{", 1)[0]
        if name.endswith(("_total", "_sum", "_count")):
            samples[identity] = value
    return samples


def metrics_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, Any]:
    samples = {
        key: round(value - before.get(key, 0.0), 6)
        for key, value in after.items()
        if key in before and value >= before[key]
    }
    prefix_queries = _sum_metric(samples, "vllm:prefix_cache_queries_total")
    prefix_hits = _sum_metric(samples, "vllm:prefix_cache_hits_total")
    prefill_sum = _sum_metric(samples, "vllm:request_prefill_time_seconds_sum")
    prefill_count = _sum_metric(samples, "vllm:request_prefill_time_seconds_count")
    computed_tokens = _sum_metric(
        samples, "vllm:request_prefill_kv_computed_tokens_sum"
    )
    return {
        "samples": samples,
        "prefix_cache_query_tokens": prefix_queries,
        "prefix_cache_hit_tokens": prefix_hits,
        "prefix_cache_hit_ratio": (
            round(prefix_hits / prefix_queries, 6) if prefix_queries else None
        ),
        "prefill_time_seconds": prefill_sum,
        "prefill_requests": prefill_count,
        "prefill_time_ms_mean": (
            round(prefill_sum * 1000 / prefill_count, 4) if prefill_count else None
        ),
        "prefill_computed_tokens": computed_tokens,
        "prefill_computed_tps": (
            round(computed_tokens / prefill_sum, 4) if prefill_sum else None
        ),
    }


def metric_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "p50": None, "p95": None, "max": None}
    return {
        "mean": round(statistics.fmean(values), 4),
        "p50": round(percentile(values, 50) or 0, 4),
        "p95": round(percentile(values, 95) or 0, 4),
        "max": round(max(values), 4),
    }


def percentile(values: list[float], value: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * value / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def cached_token_observation(
    usage: dict[str, Any],
) -> tuple[int | None, bool, str]:
    for source, path in CACHED_TOKEN_PATHS:
        value = _nested_value(usage, path)
        if value is not None:
            return _integer(value) or 0, True, source
    return None, False, ""


def counts(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


def _encode_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _request_facts(
    *,
    encoded: bytes,
    prompt: str,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    return {
        "payload_sha256": sha256(encoded).hexdigest(),
        "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
        "request_body_bytes": len(encoded),
        "requested_prompt_chars": len(prompt),
        "request_model": model,
        "requested_max_tokens": max_tokens,
    }


def _attach_request_facts(
    result: dict[str, Any],
    request_facts: dict[str, Any],
    started_unix: float,
) -> dict[str, Any]:
    result.update(request_facts)
    result["started_at_unix"] = started_unix
    result["ended_at_unix"] = time.time()
    result.setdefault("response_headers", {})
    result.setdefault("semantic_cache_hit", None)
    return result


def _header_truth(value: Any) -> bool | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "hit"}:
        return True
    if normalized in {"0", "false", "no", "miss"}:
        return False
    return None


def _sse_events(lines: Iterable[bytes | str]) -> Iterable[dict[str, Any]]:
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        body = line[len("data:") :].strip()
        if body == "[DONE]":
            return
        event = json.loads(body)
        if isinstance(event, dict):
            yield event


def _choices(event: dict[str, Any]) -> list[dict[str, Any]]:
    choices = event.get("choices")
    if not isinstance(choices, list):
        return []
    return [choice for choice in choices if isinstance(choice, dict)]


def _merge_extensions(target: dict[str, Any], event: dict[str, Any]) -> None:
    for key in ("timings", "metrics", "usage"):
        if isinstance(event.get(key), dict):
            target[key] = event[key]


def _usage_integer(usage: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in usage:
            return _integer(usage.get(key))
    return None


def _client_decode_tps(
    completion_tokens: int | None, event_times: list[float]
) -> float | None:
    if not completion_tokens or len(event_times) < MIN_DECODE_EVENTS:
        return None
    duration = event_times[-1] - event_times[0]
    return completion_tokens / duration if duration > 0 else None


def _nanosecond_rate(count: int | None, duration_ns: float | None) -> float | None:
    if not count or not duration_ns or duration_ns <= 0:
        return None
    return count / (duration_ns / 1e9)


def _nanoseconds_to_ms(value: Any) -> float | None:
    parsed = _number(value)
    return parsed / 1e6 if parsed is not None else None


def _numeric_values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [
        float(row[key])
        for row in rows
        if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)
    ]


def _sum_metric(samples: dict[str, float], metric_name: str) -> float:
    return sum(
        value
        for identity, value in samples.items()
        if identity.split("{", 1)[0] == metric_name
    )


def _nested_value(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def _integer(value: Any) -> int | None:
    parsed = _number(value)
    return int(parsed) if parsed is not None else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
