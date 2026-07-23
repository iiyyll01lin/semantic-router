"""Transport and schema helpers for the agentic tool-call probe."""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections.abc import Callable, Iterable
from copy import deepcopy
from hashlib import sha256
from typing import Any

HTTP_OK = 200
HTTP_REDIRECT_START = 300
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


def openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": str(tool["name"]),
                "description": str(tool.get("description") or ""),
                "parameters": parameter_schema(tool.get("parameters")),
            },
        }
        for tool in tools
    ]


def parameter_schema(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("type") == "object":
        return deepcopy(value)
    parameters = value if isinstance(value, dict) else {}
    properties = {
        str(name): _compact_property_schema(description)
        for name, description in parameters.items()
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def stream_ollama(
    base: str,
    model: str,
    prompt: str,
    opts: dict[str, Any],
    timeout: float,
    think: bool | None = None,
) -> dict[str, Any]:
    payload = {"model": model, "prompt": prompt, "stream": True, "options": opts}
    if think is not None:
        payload["think"] = bool(think)
    request = _request(
        base.rstrip("/") + "/api/generate",
        payload,
        {"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    first_token_at = None
    parts: list[str] = []
    final: dict[str, Any] = {}
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            event = json.loads(line)
            piece = event.get("response")
            if piece:
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                parts.append(str(piece))
            if event.get("done"):
                final = event
                break
    ended = time.perf_counter()
    prompt_tokens = _integer(final.get("prompt_eval_count"))
    completion_tokens = _integer(final.get("eval_count"))
    prompt_duration_ns = _number(final.get("prompt_eval_duration"))
    eval_duration_ns = _number(final.get("eval_duration"))
    return {
        "text": "".join(parts),
        "tool_calls": [],
        "wall_s": ended - started,
        "ttft_ms": (
            (first_token_at - started) * 1000.0 if first_token_at is not None else None
        ),
        "decode_tps": _rate(completion_tokens, eval_duration_ns),
        "prefill_tps": _rate(prompt_tokens, prompt_duration_ns),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "prompt_eval_duration_ms": (
            prompt_duration_ns / 1e6 if prompt_duration_ns is not None else None
        ),
        "eval_duration_ms": (
            eval_duration_ns / 1e6 if eval_duration_ns is not None else None
        ),
        "load_duration_ms": _nanoseconds_to_ms(final.get("load_duration")),
        "cached_tokens": 0,
        "cached_token_field_present": False,
        "cached_token_source": "",
        "usage": {},
    }


def ollama_native_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert OpenAI-style tool history into Ollama native chat messages.

    OpenAI serializes ``function.arguments`` as a JSON string, while Ollama's
    native ``/api/chat`` endpoint requires the same value to be a JSON object.
    Keep the caller-owned history unchanged and fail locally on malformed
    argument JSON instead of sending a backend request that will return 400.
    """
    normalized = deepcopy(messages)
    for message in normalized:
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                continue
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "Ollama tool-call history arguments must contain valid JSON"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError(
                    "Ollama tool-call history arguments must decode to an object"
                )
            function["arguments"] = parsed
    return normalized


def stream_ollama_chat(
    base: str,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    timeout: float,
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    num_ctx: int = 0,
    think: bool = False,
    seed: int = 42,
    keep_alive: str = "10m",
) -> dict[str, Any]:
    """Stream native Ollama chat while retaining tool messages and server timings."""
    options: dict[str, Any] = {
        "temperature": 0,
        "num_predict": max_tokens,
        "seed": seed,
    }
    if num_ctx > 0:
        options["num_ctx"] = num_ctx
    payload: dict[str, Any] = {
        "model": model,
        "messages": ollama_native_messages(messages),
        "stream": True,
        "think": think,
        "keep_alive": keep_alive,
        "options": options,
    }
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    encoded = _encode_payload(payload)
    request = urllib.request.Request(
        base.rstrip("/") + "/api/chat",
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = parse_ollama_chat_stream(
            response,
            started,
            max_tokens=max_tokens,
        )
        result["status"] = int(response.status)
        result["success"] = HTTP_OK <= int(response.status) < HTTP_REDIRECT_START
    return _attach_payload_facts(result, payload, encoded)


def parse_ollama_chat_stream(
    lines: Iterable[bytes | str],
    started: float,
    *,
    max_tokens: int,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    raw_tool_calls: list[dict[str, Any]] = []
    first_token_at = None
    final: dict[str, Any] = {}
    ended = started
    for raw in lines:
        ended = clock()
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        message = event.get("message")
        if not isinstance(message, dict):
            message = {}
        content = message.get("content")
        reasoning = message.get("thinking") or message.get("reasoning")
        event_tool_calls = message.get("tool_calls")
        if (
            (isinstance(content, str) and content)
            or (isinstance(reasoning, str) and reasoning)
            or (isinstance(event_tool_calls, list) and event_tool_calls)
        ) and first_token_at is None:
            first_token_at = ended
        if isinstance(content, str):
            content_parts.append(content)
        if isinstance(reasoning, str):
            reasoning_parts.append(reasoning)
        if isinstance(event_tool_calls, list) and event_tool_calls:
            raw_tool_calls = [
                call for call in event_tool_calls if isinstance(call, dict)
            ]
        if event.get("done"):
            final = event
            break

    openai_calls, normalized_calls = _ollama_tool_calls(raw_tool_calls)
    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    prompt_tokens = _integer(final.get("prompt_eval_count"))
    completion_tokens = _integer(final.get("eval_count"))
    prompt_duration_ns = _number(final.get("prompt_eval_duration"))
    eval_duration_ns = _number(final.get("eval_duration"))
    finish_reason = str(
        final.get("done_reason") or ("tool_calls" if openai_calls else "")
    )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content or None,
    }
    if reasoning:
        message["reasoning_content"] = reasoning
    if openai_calls:
        message["tool_calls"] = openai_calls
    return {
        "text": content,
        "reasoning": reasoning,
        "message": message,
        "tool_calls": normalized_calls,
        "wall_s": ended - started,
        "ttft_ms": (
            (first_token_at - started) * 1000.0 if first_token_at is not None else None
        ),
        "decode_tps": _rate(completion_tokens, eval_duration_ns),
        "prefill_tps": _rate(prompt_tokens, prompt_duration_ns),
        "prompt_tokens": prompt_tokens,
        "prompt_token_source": (
            "ollama.prompt_eval_count" if prompt_tokens is not None else ""
        ),
        "completion_tokens": completion_tokens,
        "completion_token_source": (
            "ollama.eval_count" if completion_tokens is not None else ""
        ),
        "prompt_eval_duration_ms": (
            prompt_duration_ns / 1e6 if prompt_duration_ns is not None else None
        ),
        "eval_duration_ms": (
            eval_duration_ns / 1e6 if eval_duration_ns is not None else None
        ),
        "load_duration_ms": _nanoseconds_to_ms(final.get("load_duration")),
        "cached_tokens": None,
        "cached_token_field_present": False,
        "cached_token_source": "",
        "finish_reason": finish_reason,
        "truncated": finish_reason == "length"
        or (
            bool(completion_tokens)
            and completion_tokens >= max_tokens
            and finish_reason not in {"stop", "tool_calls"}
        ),
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
        "raw_extensions": {
            key: final.get(key)
            for key in (
                "done",
                "done_reason",
                "load_duration",
                "total_duration",
                "prompt_eval_count",
                "prompt_eval_duration",
                "eval_count",
                "eval_duration",
            )
        },
        "response_sha256": sha256(
            _encode_payload(
                {
                    "message": message,
                    "finish_reason": finish_reason,
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                    },
                }
            )
        ).hexdigest(),
    }


def stream_openai(
    base: str,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    timeout: float,
    api_key: str = "",
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    encoded = _encode_payload(payload)
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=encoded,
        method="POST",
        headers=headers,
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = parse_openai_stream(response, started)
        result["status"] = int(response.status)
        result["success"] = HTTP_OK <= int(response.status) < HTTP_REDIRECT_START
        response_headers = {
            key.lower(): value for key, value in response.headers.items()
        }
        result["response_headers"] = response_headers
        result["semantic_cache_hit"] = _header_truth(
            response_headers.get("x-vsr-cache-hit")
        )
        return _attach_payload_facts(result, payload, encoded)


def parse_openai_stream(  # noqa: C901
    lines: Iterable[bytes | str],
    started: float,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_states: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    first_token_at = None
    ended = started
    chunk_tokens = 0
    finish_reason = ""

    for event in _sse_events(lines):
        ended = clock()
        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            usage = event_usage
        for choice in _choices(event):
            if choice.get("finish_reason") is not None:
                finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if _meaningful_delta(delta):
                if first_token_at is None:
                    first_token_at = ended
                chunk_tokens += 1
            content = delta.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if isinstance(reasoning, str):
                reasoning_parts.append(reasoning)
            _merge_tool_calls(tool_states, delta.get("tool_calls"))

    prompt_tokens = _usage_integer(usage, ("prompt_tokens", "input_tokens"))
    completion_tokens = _usage_integer(usage, ("completion_tokens", "output_tokens"))
    if completion_tokens is None:
        completion_tokens = chunk_tokens or None
    cached_tokens, cached_present, cached_source = _cached_tokens(usage)
    decode_window = ended - first_token_at if first_token_at is not None else None
    openai_calls = [
        _final_openai_tool_call(tool_states[index]) for index in sorted(tool_states)
    ]
    content = "".join(text_parts)
    reasoning = "".join(reasoning_parts)
    message: dict[str, Any] = {"role": "assistant", "content": content or None}
    if reasoning:
        message["reasoning_content"] = reasoning
    if openai_calls:
        message["tool_calls"] = openai_calls
    return {
        "text": content,
        "reasoning": reasoning,
        "message": message,
        "tool_calls": [
            _final_tool_call(tool_states[index]) for index in sorted(tool_states)
        ],
        "wall_s": ended - started,
        "ttft_ms": (
            (first_token_at - started) * 1000.0 if first_token_at is not None else None
        ),
        "decode_tps": (
            completion_tokens / decode_window
            if completion_tokens and decode_window and decode_window > 0
            else None
        ),
        "prefill_tps": None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "prompt_eval_duration_ms": None,
        "eval_duration_ms": (
            decode_window * 1000.0 if decode_window is not None else None
        ),
        "load_duration_ms": None,
        "cached_tokens": cached_tokens,
        "cached_token_field_present": cached_present,
        "cached_token_source": cached_source,
        "finish_reason": finish_reason,
        "truncated": finish_reason == "length",
        "usage": usage,
        "prompt_token_source": (
            "openai.usage.prompt_tokens"
            if "prompt_tokens" in usage
            else "openai.usage.input_tokens" if "input_tokens" in usage else ""
        ),
        "completion_token_source": (
            "openai.usage.completion_tokens"
            if "completion_tokens" in usage
            else "openai.usage.output_tokens" if "output_tokens" in usage else ""
        ),
        "response_sha256": sha256(
            _encode_payload(
                {
                    "message": message,
                    "finish_reason": finish_reason,
                    "usage": usage,
                }
            )
        ).hexdigest(),
    }


def _request(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        data=_encode_payload(payload),
        method="POST",
        headers=headers,
    )


def _encode_payload(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _attach_payload_facts(
    result: dict[str, Any],
    payload: dict[str, Any],
    encoded: bytes,
) -> dict[str, Any]:
    result["payload_sha256"] = sha256(encoded).hexdigest()
    result["request_body_bytes"] = len(encoded)
    result["messages_sha256"] = sha256(
        _encode_payload(payload.get("messages") or [])
    ).hexdigest()
    result["tool_schema_sha256"] = sha256(
        _encode_payload(payload.get("tools") or [])
    ).hexdigest()
    result.setdefault("response_headers", {})
    result.setdefault("semantic_cache_hit", None)
    return result


def _compact_property_schema(description: Any) -> dict[str, Any]:
    text = str(description or "")
    lowered = text.lower()
    property_type = "string"
    if lowered.startswith("integer"):
        property_type = "integer"
    elif lowered.startswith("number"):
        property_type = "number"
    elif lowered.startswith("boolean"):
        property_type = "boolean"
    elif lowered.startswith("array"):
        property_type = "array"
    schema: dict[str, Any] = {"type": property_type, "description": text}
    enum_values = re.findall(r"'([^']+)'", text)
    if len(enum_values) > 1:
        schema["enum"] = enum_values
    if property_type == "array":
        schema["items"] = {"type": "string"}
    return schema


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


def _meaningful_delta(delta: dict[str, Any]) -> bool:
    if delta.get("content") or delta.get("reasoning") or delta.get("reasoning_content"):
        return True
    tool_calls = delta.get("tool_calls")
    return isinstance(tool_calls, list) and bool(tool_calls)


def _merge_tool_calls(states: dict[int, dict[str, Any]], fragments: Any) -> None:
    if not isinstance(fragments, list):
        return
    for fallback_index, fragment in enumerate(fragments):
        if not isinstance(fragment, dict):
            continue
        index = int(fragment.get("index", fallback_index))
        state = states.setdefault(
            index,
            {"id": "", "name": [], "arguments": [], "type": "function"},
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


def _final_tool_call(state: dict[str, Any]) -> dict[str, Any]:
    raw_arguments = "".join(state["arguments"]) or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        arguments = raw_arguments
    return {
        "id": state["id"],
        "type": state["type"],
        "name": "".join(state["name"]),
        "arguments": arguments,
        "raw_arguments": raw_arguments,
    }


def _final_openai_tool_call(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": state["id"],
        "type": state["type"],
        "function": {
            "name": "".join(state["name"]),
            "arguments": "".join(state["arguments"]) or "{}",
        },
    }


def _ollama_tool_calls(
    calls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    openai_calls = []
    normalized_calls = []
    for index, call in enumerate(calls):
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            raw_arguments = arguments
            try:
                parsed_arguments = json.loads(arguments)
            except json.JSONDecodeError:
                parsed_arguments = arguments
        else:
            parsed_arguments = arguments if isinstance(arguments, dict) else {}
            raw_arguments = json.dumps(
                parsed_arguments,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        call_id = str(call.get("id") or f"call_{index}")
        call_type = str(call.get("type") or "function")
        openai_calls.append(
            {
                "id": call_id,
                "type": call_type,
                "function": {
                    "name": name,
                    "arguments": raw_arguments,
                },
            }
        )
        normalized_calls.append(
            {
                "id": call_id,
                "type": call_type,
                "name": name,
                "arguments": parsed_arguments,
                "raw_arguments": raw_arguments,
            }
        )
    return openai_calls, normalized_calls


def _cached_tokens(usage: dict[str, Any]) -> tuple[int, bool, str]:
    for source, path in CACHED_TOKEN_PATHS:
        value = _nested_value(usage, path)
        if value is not None:
            return _integer(value) or 0, True, source
    return 0, False, ""


def _usage_integer(usage: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in usage:
            return _integer(usage.get(key))
    return None


def _nested_value(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _rate(count: int | None, duration_ns: float | None) -> float | None:
    if not count or not duration_ns or duration_ns <= 0:
        return None
    return count / (duration_ns / 1e9)


def _nanoseconds_to_ms(value: Any) -> float | None:
    parsed = _number(value)
    return parsed / 1e6 if parsed is not None else None


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


def _header_truth(value: Any) -> bool | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "hit"}:
        return True
    if normalized in {"0", "false", "no", "miss"}:
        return False
    return None
