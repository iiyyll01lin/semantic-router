import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PERF_DIR = Path(__file__).parent
DATASET_PATH = PERF_DIR / "data" / "agentic-toolcall-tasks.json"
EXPECTED_TOOL_COUNT = 25
EXPECTED_TTFT_MS = 50.0
EXPECTED_WALL_SECONDS = 0.07
EXPECTED_PROMPT_TOKENS = 120
EXPECTED_CACHED_TOKENS = 80
EXPECTED_PREFILL_TPS = 1000.0
OLLAMA_PROMPT_TOKENS = 100
OLLAMA_PROMPT_DURATION_MS = 100.0
OLLAMA_COMPLETION_TOKENS = 5
OLLAMA_LOAD_DURATION_MS = 20.0
EXPECTED_WARM_CACHE_RATIO = 0.8


def load_modules():
    sys.path.insert(0, str(PERF_DIR))
    support = __import__("agentic_toolcall_support")
    path = PERF_DIR / "agentic_toolcall.py"
    module_spec = importlib.util.spec_from_file_location("agentic_toolcall", path)
    assert module_spec is not None
    module = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module, support


def dataset():
    return json.loads(DATASET_PATH.read_text())


def task_by_id(task_id):
    return next(task for task in dataset()["tasks"] if task["id"] == task_id)


def test_dataset_covers_required_agentic_cases_and_native_schema():
    _probe, support = load_modules()
    task_categories = {task.get("category") for task in dataset()["tasks"]}

    assert {
        "no_tool",
        "ambiguous",
        "nested_args",
        "parallel_calls",
        "tool_error",
        "adversarial",
        "large_catalog",
    } <= task_categories
    assert len(dataset()["tools"]) >= EXPECTED_TOOL_COUNT

    schemas = support.openai_tools(dataset()["tools"])
    deployment = next(
        tool for tool in schemas if tool["function"]["name"] == "create_deployment"
    )
    config = deployment["function"]["parameters"]["properties"]["config"]
    assert config["type"] == "object"
    assert config["required"] == ["replicas", "environment"]


def test_prompt_extraction_supports_no_tool_and_parallel_calls():
    probe, _support = load_modules()
    parallel, _span = probe.extract_tool_calls(
        'reasoning first [{"name":"get_weather","arguments":{"location":"Tokyo"}},'
        '{"name":"get_weather","arguments":{"location":"Seoul"}}]'
    )
    no_tool, _span = probe.extract_tool_calls('final: {"name":"none","arguments":{}}')

    assert [call["arguments"]["location"] for call in parallel] == [
        "Tokyo",
        "Seoul",
    ]
    assert no_tool == []


def test_scoring_handles_nested_parallel_and_no_tool_expectations():
    probe, _support = load_modules()
    nested = [
        {
            "name": "create_deployment",
            "arguments": {
                "service": "semantic-router",
                "config": {"replicas": 2, "environment": "staging"},
            },
        }
    ]
    parallel = [
        {
            "name": "get_weather",
            "arguments": {"location": "Seoul", "unit": "celsius"},
        },
        {
            "name": "get_weather",
            "arguments": {"location": "Tokyo", "unit": "celsius"},
        },
    ]

    assert probe.score_task_calls(task_by_id("nested_deployment"), nested)[:3] == (
        True,
        True,
        True,
    )
    assert probe.score_task_calls(task_by_id("parallel_weather"), parallel)[:3] == (
        True,
        True,
        True,
    )
    assert probe.score_task_calls(task_by_id("no_tool_thanks"), [])[:3] == (
        True,
        True,
        True,
    )


def test_openai_stream_retains_usage_cache_ttft_and_native_calls():
    _probe, support = load_modules()
    lines = [
        b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n',
        (
            b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
            b'"id":"call_1","type":"function","function":{"name":"get_",'
            b'"arguments":"{\\"ticker\\":"}}]}}]}\n'
        ),
        (
            b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
            b'"function":{"name":"stock_price","arguments":"\\"AAPL\\"}"}}]}}],'
            b'"usage":{"prompt_tokens":120,"completion_tokens":4,'
            b'"prompt_tokens_details":{"cached_tokens":80}}}\n'
        ),
        b"data: [DONE]\n",
    ]
    clock_values = iter((0.01, 0.05, EXPECTED_WALL_SECONDS))

    result = support.parse_openai_stream(lines, 0.0, clock=lambda: next(clock_values))

    assert result["ttft_ms"] == EXPECTED_TTFT_MS
    assert result["wall_s"] == EXPECTED_WALL_SECONDS
    assert result["prompt_tokens"] == EXPECTED_PROMPT_TOKENS
    assert result["cached_tokens"] == EXPECTED_CACHED_TOKENS
    assert result["cached_token_field_present"]
    assert result["tool_calls"][0]["name"] == "get_stock_price"
    assert result["tool_calls"][0]["arguments"] == {"ticker": "AAPL"}


def test_ollama_native_messages_converts_openai_argument_strings_without_mutation():
    _probe, support = load_modules()
    messages = [
        {"role": "user", "content": "weather Tokyo"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location":"Tokyo"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]

    normalized = support.ollama_native_messages(messages)

    assert normalized[1]["tool_calls"][0]["function"]["arguments"] == {
        "location": "Tokyo"
    }
    assert messages[1]["tool_calls"][0]["function"]["arguments"] == (
        '{"location":"Tokyo"}'
    )


def test_ollama_native_messages_rejects_malformed_argument_json():
    _probe, support = load_modules()
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "type": "function",
                    "function": {"name": "broken", "arguments": "{"},
                }
            ],
        }
    ]

    try:
        support.ollama_native_messages(messages)
    except ValueError as exc:
        assert "valid JSON" in str(exc)
    else:
        raise AssertionError("malformed Ollama history arguments must fail locally")


def test_ollama_stream_retains_authoritative_prefill_metrics(monkeypatch):
    _probe, support = load_modules()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'{"response":"ok","done":false}\n'
            yield (
                b'{"done":true,"prompt_eval_count":100,'
                b'"prompt_eval_duration":100000000,"eval_count":5,'
                b'"eval_duration":50000000,"load_duration":20000000}\n'
            )

    monkeypatch.setattr(
        support.urllib.request, "urlopen", lambda *_args, **_kw: FakeResponse()
    )
    result = support.stream_ollama(
        "http://localhost:11434",
        "model",
        "prompt",
        {"temperature": 0},
        1.0,
    )

    assert result["prompt_tokens"] == OLLAMA_PROMPT_TOKENS
    assert result["prompt_eval_duration_ms"] == OLLAMA_PROMPT_DURATION_MS
    assert result["prefill_tps"] == EXPECTED_PREFILL_TPS
    assert result["completion_tokens"] == OLLAMA_COMPLETION_TOKENS
    assert result["load_duration_ms"] == OLLAMA_LOAD_DURATION_MS


def test_cold_warm_aggregation_keeps_separate_evidence():
    probe, _support = load_modules()
    base = {
        "json_valid": True,
        "name_correct": True,
        "args_correct": True,
        "step_correct": True,
        "completion_tokens": 2,
        "cached_token_field_present": True,
        "cached_token_source": "usage.prompt_tokens_details.cached_tokens",
    }
    trials = [
        {
            **base,
            "cache_state": "cold",
            "prompt_tokens": 100,
            "cached_tokens": 0,
            "ttft_ms": 20.0,
            "prefill_tps": 100.0,
        },
        {
            **base,
            "cache_state": "warm",
            "prompt_tokens": 100,
            "cached_tokens": 80,
            "ttft_ms": 5.0,
            "prefill_tps": 400.0,
        },
    ]

    summary = probe.aggregate_trials(trials)

    assert summary["cache_states"]["cold"]["requests"] == 1
    assert summary["cache_states"]["warm"]["requests"] == 1
    assert (
        summary["cache_states"]["warm"]["cached_prompt_ratio"]
        == EXPECTED_WARM_CACHE_RATIO
    )


def test_native_openai_mode_sends_tools_and_tool_choice(monkeypatch):
    probe, support = load_modules()
    captured = {}

    def fake_stream_openai(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {}

    monkeypatch.setattr(probe, "stream_openai", fake_stream_openai)
    args = SimpleNamespace(
        api="openai",
        tool_mode="native",
        backend_url="http://localhost:8000/v1",
        num_predict=32,
        timeout=1.0,
        api_key="key",
        tool_choice="auto",
    )
    tools = support.openai_tools(dataset()["tools"])

    probe.run_probe_request(
        args,
        "model",
        task_by_id("weather_tokyo"),
        "legacy prompt",
        tools,
        {},
    )

    assert captured["kwargs"]["tools"] == tools
    assert captured["kwargs"]["tool_choice"] == "auto"
    assert captured["args"][2][-1]["content"] == task_by_id("weather_tokyo")["query"]


def test_cli_defaults_preserve_ollama_prompt_mode():
    probe, _support = load_modules()
    args = probe.build_parser().parse_args(["--models", "model"])

    assert args.api == "ollama"
    assert args.tool_mode == "prompt"
    assert args.warm_runs == 0
