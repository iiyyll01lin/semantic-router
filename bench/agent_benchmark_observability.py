"""Shared OpenAI response telemetry for live agent benchmarks."""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

PROMPT_TOKEN_KEYS = ("prompt_tokens", "input_tokens")
COMPLETION_TOKEN_KEYS = ("completion_tokens", "output_tokens")
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
PREFILL_DURATION_PATHS = (
    ("response.prompt_eval_duration", ("prompt_eval_duration",), 1e-6),
    (
        "usage.prompt_eval_duration",
        ("usage", "prompt_eval_duration"),
        1e-6,
    ),
    ("usage.prefill_duration_ms", ("usage", "prefill_duration_ms"), 1.0),
    ("metrics.prefill_duration_ms", ("metrics", "prefill_duration_ms"), 1.0),
    ("timings.prefill_ms", ("timings", "prefill_ms"), 1.0),
    ("timings.prompt_ms", ("timings", "prompt_ms"), 1.0),
)
PREFILL_TPS_PATHS = (
    ("usage.prefill_tokens_per_second", ("usage", "prefill_tokens_per_second")),
    ("metrics.prefill_tokens_per_second", ("metrics", "prefill_tokens_per_second")),
    ("timings.prefill_tokens_per_second", ("timings", "prefill_tokens_per_second")),
    ("timings.prompt_per_second", ("timings", "prompt_per_second")),
)
TTFT_PATHS = (
    ("response.ttft_ms", ("ttft_ms",)),
    ("usage.ttft_ms", ("usage", "ttft_ms")),
    ("metrics.ttft_ms", ("metrics", "ttft_ms")),
    ("timings.ttft_ms", ("timings", "ttft_ms")),
    ("timings.time_to_first_token_ms", ("timings", "time_to_first_token_ms")),
)
TTFT_HEADERS = (
    "x-vllm-ttft-ms",
    "x-ttft-ms",
    "x-time-to-first-token-ms",
)


def usage_value(response_json: dict[str, Any], keys: tuple[str, ...]) -> int:
    usage = response_json.get("usage")
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = _number(usage.get(key))
        if value is not None:
            return int(value)
    return 0


def cached_token_observation(
    response_json: dict[str, Any],
) -> tuple[int, bool, str]:
    usage = response_json.get("usage")
    if not isinstance(usage, dict):
        return 0, False, ""
    for source, path in CACHED_TOKEN_PATHS:
        value = _nested_value(usage, path)
        if value is not None:
            parsed = _number(value)
            return int(parsed or 0), True, source
    return 0, False, ""


def prefill_observation(
    response_json: dict[str, Any],
) -> tuple[float | None, float | None, str]:
    duration_ms = None
    source = ""
    for candidate_source, path, scale in PREFILL_DURATION_PATHS:
        value = _number(_nested_value(response_json, path))
        if value is not None:
            duration_ms = value * scale
            source = candidate_source
            break

    prefill_tps = None
    for candidate_source, path in PREFILL_TPS_PATHS:
        value = _number(_nested_value(response_json, path))
        if value is not None:
            prefill_tps = value
            source = source or candidate_source
            break

    prompt_tokens = usage_value(response_json, PROMPT_TOKEN_KEYS)
    if prefill_tps is None and prompt_tokens and duration_ms and duration_ms > 0:
        prefill_tps = prompt_tokens / (duration_ms / 1000.0)
    return duration_ms, prefill_tps, source


def ttft_observation(
    response_json: dict[str, Any],
    headers: dict[str, str],
    client_ttft_ms: float | None = None,
) -> tuple[float | None, str]:
    if client_ttft_ms is not None:
        return client_ttft_ms, "client_stream"

    normalized_headers = {str(key).lower(): value for key, value in headers.items()}
    for header in TTFT_HEADERS:
        value = _number(normalized_headers.get(header))
        if value is not None:
            return value, f"header.{header}"
    server_timing = normalized_headers.get("server-timing", "")
    parsed_server_timing = _server_timing_ttft(server_timing)
    if parsed_server_timing is not None:
        return parsed_server_timing, "header.server-timing"

    for source, path in TTFT_PATHS:
        value = _number(_nested_value(response_json, path))
        if value is not None:
            return value, source
    return None, ""


def observation_from_result(result: dict[str, Any]) -> dict[str, Any]:
    response_json = result.get("json")
    if not isinstance(response_json, dict):
        response_json = {}
    headers = result.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    cached, cached_present, cached_source = cached_token_observation(response_json)
    prefill_ms, prefill_tps, prefill_source = prefill_observation(response_json)
    client_ttft = _number(result.get("ttft_ms"))
    ttft_ms, ttft_source = ttft_observation(response_json, headers, client_ttft)
    return {
        "prompt_tokens": usage_value(response_json, PROMPT_TOKEN_KEYS),
        "completion_tokens": usage_value(response_json, COMPLETION_TOKEN_KEYS),
        "cached_tokens": cached,
        "cached_token_field_present": cached_present,
        "cached_token_source": cached_source,
        "prefill_duration_ms": _rounded(prefill_ms),
        "prefill_tokens_per_second": _rounded(prefill_tps),
        "prefill_source": prefill_source,
        "ttft_ms": _rounded(ttft_ms),
        "ttft_source": ttft_source,
    }


def parse_openai_stream(
    lines: Iterable[bytes | str],
    started: float,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[dict[str, Any], float | None]:
    """Reconstruct one OpenAI chat completion and client-side TTFT from SSE."""
    response: dict[str, Any] = {}
    choice_states: dict[int, dict[str, Any]] = {}
    first_token_at = None

    for event in _sse_events(lines):
        for key in ("id", "object", "created", "model", "system_fingerprint"):
            if key in event:
                response[key] = event[key]
        if isinstance(event.get("usage"), dict):
            response["usage"] = event["usage"]
        if "error" in event:
            response["error"] = event["error"]

        for fallback_index, choice in enumerate(_stream_choices(event)):
            first_token_at = _record_stream_choice(
                choice_states,
                choice,
                fallback_index,
                first_token_at,
                clock,
            )

    response["choices"] = [
        _stream_choice(choice_states[index]) for index in sorted(choice_states)
    ]
    ttft_ms = (
        (first_token_at - started) * 1000.0 if first_token_at is not None else None
    )
    return response, ttft_ms


def _sse_events(lines: Iterable[bytes | str]) -> Iterator[dict[str, Any]]:
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


def _stream_choices(event: dict[str, Any]) -> list[dict[str, Any]]:
    choices = event.get("choices")
    if not isinstance(choices, list):
        return []
    return [choice for choice in choices if isinstance(choice, dict)]


def _record_stream_choice(
    choice_states: dict[int, dict[str, Any]],
    choice: dict[str, Any],
    fallback_index: int,
    first_token_at: float | None,
    clock: Callable[[], float],
) -> float | None:
    index = int(choice.get("index", fallback_index))
    state = choice_states.setdefault(
        index,
        {
            "index": index,
            "content": [],
            "reasoning": [],
            "tool_calls": {},
            "finish_reason": None,
        },
    )
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        delta = {}
    if _meaningful_delta(delta) and first_token_at is None:
        first_token_at = clock()
    content = delta.get("content")
    if isinstance(content, str):
        state["content"].append(content)
    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
    if isinstance(reasoning, str):
        state["reasoning"].append(reasoning)
    _merge_tool_call_deltas(state["tool_calls"], delta.get("tool_calls"))
    if choice.get("finish_reason") is not None:
        state["finish_reason"] = choice["finish_reason"]
    return first_token_at


def summarize_observability(rows: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in rows if row.get("success")]
    prompt_tokens = sum(int(row.get("prompt_tokens") or 0) for row in successful)
    cached_tokens = sum(int(row.get("cached_tokens") or 0) for row in successful)
    present = sum(
        1 for row in successful if bool(row.get("cached_token_field_present"))
    )
    cache_states = {
        state: _summarize_slice(
            [row for row in rows if str(row.get("cache_state") or "") == state]
        )
        for state in ("cold", "warm")
    }
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": sum(
            int(row.get("completion_tokens") or 0) for row in successful
        ),
        "cached_tokens": cached_tokens,
        "cached_prompt_ratio": (
            round(cached_tokens / prompt_tokens, 6) if prompt_tokens else None
        ),
        "cached_token_field_present": present,
        "cached_token_field_rate": (
            round(present / len(successful), 4) if successful else 0.0
        ),
        "cached_token_source_counts": _counts(
            str(row.get("cached_token_source"))
            for row in successful
            if row.get("cached_token_source")
        ),
        "prefill_duration_ms": _metric_summary(
            row.get("prefill_duration_ms") for row in successful
        ),
        "prefill_tokens_per_second": _metric_summary(
            row.get("prefill_tokens_per_second") for row in successful
        ),
        "prefill_source_counts": _counts(
            str(row.get("prefill_source"))
            for row in successful
            if row.get("prefill_source")
        ),
        "ttft_ms": _metric_summary(row.get("ttft_ms") for row in successful),
        "ttft_source_counts": _counts(
            str(row.get("ttft_source")) for row in successful if row.get("ttft_source")
        ),
        "cache_state_metrics": cache_states,
    }


def _summarize_slice(rows: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in rows if row.get("success")]
    prompt_tokens = sum(int(row.get("prompt_tokens") or 0) for row in successful)
    cached_tokens = sum(int(row.get("cached_tokens") or 0) for row in successful)
    return {
        "requests": len(rows),
        "successes": len(successful),
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "cached_prompt_ratio": (
            round(cached_tokens / prompt_tokens, 6) if prompt_tokens else None
        ),
        "ttft_ms": _metric_summary(row.get("ttft_ms") for row in successful),
        "prefill_duration_ms": _metric_summary(
            row.get("prefill_duration_ms") for row in successful
        ),
    }


def _stream_choice(state: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(state["content"]) or None,
    }
    reasoning = "".join(state["reasoning"])
    if reasoning:
        message["reasoning_content"] = reasoning
    tool_calls = [
        _stream_tool_call(state["tool_calls"][index])
        for index in sorted(state["tool_calls"])
    ]
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "index": state["index"],
        "message": message,
        "finish_reason": state["finish_reason"],
    }


def _stream_tool_call(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": state["id"],
        "type": state["type"],
        "function": {
            "name": "".join(state["name"]),
            "arguments": "".join(state["arguments"]),
        },
    }


def _merge_tool_call_deltas(states: dict[int, dict[str, Any]], fragments: Any) -> None:
    if not isinstance(fragments, list):
        return
    for fallback_index, fragment in enumerate(fragments):
        if not isinstance(fragment, dict):
            continue
        index = int(fragment.get("index", fallback_index))
        state = states.setdefault(
            index,
            {
                "id": "",
                "type": "function",
                "name": [],
                "arguments": [],
            },
        )
        if fragment.get("id"):
            state["id"] = str(fragment["id"])
        if fragment.get("type"):
            state["type"] = str(fragment["type"])
        function = fragment.get("function")
        if not isinstance(function, dict):
            continue
        if function.get("name"):
            state["name"].append(str(function["name"]))
        if function.get("arguments"):
            state["arguments"].append(str(function["arguments"]))


def _meaningful_delta(delta: dict[str, Any]) -> bool:
    if delta.get("content") or delta.get("reasoning") or delta.get("reasoning_content"):
        return True
    tool_calls = delta.get("tool_calls")
    return isinstance(tool_calls, list) and bool(tool_calls)


def _metric_summary(values: Iterable[Any]) -> dict[str, float | None]:
    parsed = [number for value in values if (number := _number(value)) is not None]
    if not parsed:
        return {"mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(parsed)
    return {
        "mean": round(statistics.fmean(ordered), 3),
        "p50": round(_percentile(ordered, 50), 3),
        "p95": round(_percentile(ordered, 95), 3),
        "max": round(max(ordered), 3),
    }


def _percentile(ordered: list[float], pct: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _nested_value(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _server_timing_ttft(value: str) -> float | None:
    for item in value.split(","):
        name, *parameters = item.split(";")
        if name.strip().lower() not in {"ttft", "time-to-first-token"}:
            continue
        for parameter in parameters:
            key, separator, raw = parameter.partition("=")
            if separator and key.strip().lower() == "dur":
                return _number(raw.strip().strip('"'))
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rounded(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _counts(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))
