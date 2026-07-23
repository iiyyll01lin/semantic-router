import json

import agent_benchmark_observability as observability

PROMPT_TOKENS = 200
COMPLETION_TOKENS = 12
CACHED_TOKENS = 150
PREFILL_DURATION_NS = 50_000_000
PREFILL_DURATION_MS = 50.0
PREFILL_TPS = 4000.0
CLIENT_TTFT_MS = 12.5
SERVER_TIMING_TTFT_MS = 8.25
STREAM_TTFT_MS = 20.0


def test_observation_extracts_responses_usage_prefill_and_client_ttft():
    result = {
        "json": {
            "prompt_eval_duration": PREFILL_DURATION_NS,
            "usage": {
                "input_tokens": PROMPT_TOKENS,
                "output_tokens": COMPLETION_TOKENS,
                "input_tokens_details": {"cached_tokens": CACHED_TOKENS},
            },
        },
        "headers": {},
        "ttft_ms": CLIENT_TTFT_MS,
    }

    metric = observability.observation_from_result(result)

    assert metric["prompt_tokens"] == PROMPT_TOKENS
    assert metric["completion_tokens"] == COMPLETION_TOKENS
    assert metric["cached_tokens"] == CACHED_TOKENS
    assert metric["cached_token_field_present"]
    assert metric["prefill_duration_ms"] == PREFILL_DURATION_MS
    assert metric["prefill_tokens_per_second"] == PREFILL_TPS
    assert metric["ttft_ms"] == CLIENT_TTFT_MS
    assert metric["ttft_source"] == "client_stream"


def test_ttft_falls_back_to_server_timing_header():
    value, source = observability.ttft_observation(
        {},
        {"Server-Timing": f"queue;dur=1, ttft;dur={SERVER_TIMING_TTFT_MS}"},
    )

    assert value == SERVER_TIMING_TTFT_MS
    assert source == "header.server-timing"


def test_openai_stream_reconstructs_content_tool_calls_usage_and_ttft():
    lines = [
        b'data: {"id":"response","model":"model","choices":[{"index":0,'
        b'"delta":{"role":"assistant"}}]}\n',
        b'data: {"choices":[{"index":0,"delta":{"content":"ready "}}]}\n',
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        b'"id":"call_1","function":{"name":"lookup","arguments":"{\\"id\\":"}}]}}]}\n',
        (
            b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
            b'"function":{"arguments":"1}"}}]},"finish_reason":"tool_calls"}],'
            b'"usage":{"prompt_tokens":200,"completion_tokens":12,'
            b'"prompt_tokens_details":{"cached_tokens":150}}}\n'
        ),
        b"data: [DONE]\n",
    ]
    response, ttft_ms = observability.parse_openai_stream(
        lines,
        started=0.0,
        clock=lambda: STREAM_TTFT_MS / 1000.0,
    )
    message = response["choices"][0]["message"]

    assert ttft_ms == STREAM_TTFT_MS
    assert message["content"] == "ready "
    assert message["tool_calls"][0]["function"]["name"] == "lookup"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"id":1}'
    assert response["usage"]["prompt_tokens"] == PROMPT_TOKENS


def test_observability_summary_separates_cold_and_warm_cache_evidence():
    base = {
        "success": True,
        "completion_tokens": COMPLETION_TOKENS,
        "cached_token_field_present": True,
        "cached_token_source": "usage.prompt_tokens_details.cached_tokens",
        "prefill_source": "response.prompt_eval_duration",
        "prefill_duration_ms": PREFILL_DURATION_MS,
        "prefill_tokens_per_second": PREFILL_TPS,
        "ttft_source": "client_stream",
    }
    rows = [
        {
            **base,
            "cache_state": "cold",
            "prompt_tokens": PROMPT_TOKENS,
            "cached_tokens": 0,
            "ttft_ms": CLIENT_TTFT_MS,
        },
        {
            **base,
            "cache_state": "warm",
            "prompt_tokens": PROMPT_TOKENS,
            "cached_tokens": CACHED_TOKENS,
            "ttft_ms": SERVER_TIMING_TTFT_MS,
        },
    ]

    summary = observability.summarize_observability(rows)

    assert summary["prompt_tokens"] == PROMPT_TOKENS * len(rows)
    assert summary["cached_tokens"] == CACHED_TOKENS
    assert summary["cache_state_metrics"]["cold"]["requests"] == 1
    assert summary["cache_state_metrics"]["warm"]["requests"] == 1
    assert summary["ttft_source_counts"] == {"client_stream": len(rows)}


def test_stream_parser_output_is_json_serializable():
    response, _ttft = observability.parse_openai_stream(
        [b'data: {"choices":[],"usage":{"prompt_tokens":1}}\n', b"data: [DONE]\n"],
        started=0.0,
        clock=lambda: 0.1,
    )

    json.dumps(response)
