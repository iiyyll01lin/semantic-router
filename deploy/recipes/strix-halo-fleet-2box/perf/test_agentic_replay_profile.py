import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path

PERF_DIR = Path(__file__).parent
REPO_ROOT = PERF_DIR.parents[3]
BENCH_DIR = REPO_ROOT / "bench"
EXPECTED_PAYLOAD_ATTEMPTS = 2
EXPECTED_CHECKPOINT = 8_192
EXPECTED_TOOL_TURNS = 28
EXPECTED_PREFILL_TPS = 2_000.0
EXPECTED_CONTEXT_WINDOW = 65_536
EXPECTED_TOOL_COUNT = 25
EXPECTED_TASK_COUNT = 22
PAYLOAD_TOKENS = 256


def load_profile():
    for path in (BENCH_DIR, PERF_DIR):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    module_path = PERF_DIR / "agentic_replay_profile.py"
    spec = importlib.util.spec_from_file_location(
        "agentic_replay_profile",
        module_path,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_dataset():
    return json.loads((PERF_DIR / "data" / "agentic-toolcall-tasks.json").read_text())


def success_result(prompt_tokens, marker=""):
    return {
        "status": 200,
        "success": True,
        "text": marker,
        "message": {"role": "assistant", "content": marker or "OK"},
        "json": {
            "id": "response",
            "choices": [{"message": {"role": "assistant", "content": marker or "OK"}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 1,
            },
        },
        "prompt_tokens": prompt_tokens,
        "completion_tokens": 1,
        "prompt_token_source": "mock.prompt_tokens",
        "completion_token_source": "mock.completion_tokens",
        "marker_correct": bool(marker),
        "ttft_ms": 1.0,
        "prompt_eval_duration_ms": 2.0,
        "prefill_tps": 1000.0,
        "decode_tps": 10.0,
        "finish_reason": "stop",
        "truncated": False,
        "payload_sha256": "payload",
        "messages_sha256": "messages",
        "tool_schema_sha256": "schema",
        "response_sha256": "response",
        "semantic_cache_hit": None,
        "error": "",
    }


def test_scripted_replay_preserves_assistant_calls_and_tool_results():
    profile = load_profile()
    replay = __import__("agent_replay")
    loop = __import__("agent_loop")
    task = load_dataset()["tasks"][0]
    conversation = loop.AgentConversation("system")

    event = replay.append_scripted_tool_turn(
        conversation,
        task,
        replay.deterministic_tool_payload(256, 4, namespace="B"),
        turn_index=0,
        payload_target_tokens=256,
    )

    assert [message["role"] for message in conversation.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert conversation.messages[2]["tool_calls"][0]["id"] == event.call_ids[0]
    assert conversation.messages[3]["tool_call_id"] == event.call_ids[0]
    assert (
        sum(len(schedule) for schedule in profile.CHECKPOINT_TURN_SCHEDULES.values())
        == EXPECTED_TOOL_TURNS
    )


def test_payload_history_hashes_and_event_serialization_are_deterministic():
    load_profile()
    replay = __import__("agent_replay")
    loop = __import__("agent_loop")
    task = load_dataset()["tasks"][0]
    payload = replay.deterministic_tool_payload(
        replay.PAYLOAD_TOKENS_1K,
        16,
        namespace="B",
    )
    events = []
    histories = []
    for _index in range(2):
        conversation = loop.AgentConversation("system")
        event = replay.append_scripted_tool_turn(
            conversation,
            task,
            payload,
            turn_index=7,
            payload_target_tokens=replay.PAYLOAD_TOKENS_1K,
        )
        events.append(event)
        histories.append(conversation.snapshot())

    serialized = json.loads(json.dumps(asdict(events[0]), sort_keys=True))

    assert payload == replay.deterministic_tool_payload(
        replay.PAYLOAD_TOKENS_1K,
        16,
        namespace="B",
    )
    assert replay.text_sha256(payload) == events[0].tool_result_sha256[0]
    assert events[0].history_sha256 == events[1].history_sha256
    assert replay.json_sha256(histories[0]) == replay.json_sha256(histories[1])
    assert serialized["history_sha256"] == events[0].history_sha256
    assert serialized["tool_result_sha256"] == list(events[0].tool_result_sha256)


def test_default_checkpoint_plan_reserves_verified_context_headroom():
    profile = load_profile()
    replay = __import__("agent_replay")
    args = profile.build_parser().parse_args(
        ["--artifact-dir", "/tmp/unused", "--direct-model", "model"]
    )

    profile.validate_args(args)

    assert args.checkpoint_values == profile.DEFAULT_CHECKPOINTS
    assert tuple(replay.PAYLOAD_TARGETS) == (256, 1_024, 4_096)
    assert (
        max(args.checkpoint_values) + args.output_tokens + args.headroom_tokens
        == args.context_window
        == EXPECTED_CONTEXT_WINDOW
    )


def test_native_ollama_chat_parser_retains_tool_calls_and_timings():
    _profile = load_profile()
    support = __import__("agentic_toolcall_support")
    lines = [
        (
            b'{"message":{"role":"assistant","content":"","tool_calls":['
            b'{"function":{"name":"get_weather","arguments":'
            b'{"location":"Tokyo","unit":"celsius"}}}]},"done":false}\n'
        ),
        (
            b'{"message":{"role":"assistant","content":""},"done":true,'
            b'"done_reason":"stop","prompt_eval_count":8192,'
            b'"prompt_eval_duration":4096000000,"eval_count":4,'
            b'"eval_duration":40000000}\n'
        ),
    ]
    clock = iter((1.0, 2.0))

    result = support.parse_ollama_chat_stream(
        lines,
        0.0,
        max_tokens=16,
        clock=lambda: next(clock),
    )

    assert result["prompt_tokens"] == EXPECTED_CHECKPOINT
    assert result["prefill_tps"] == EXPECTED_PREFILL_TPS
    assert result["finish_reason"] == "stop"
    assert not result["truncated"]
    assert result["tool_calls"][0]["name"] == "get_weather"
    assert (
        result["message"]["tool_calls"][0]["function"]["arguments"]
        == '{"location":"Tokyo","unit":"celsius"}'
    )


def test_payload_calibration_uses_authoritative_delta_and_shared_gates():
    profile = load_profile()
    args = profile.build_parser().parse_args(
        ["--artifact-dir", "/tmp/unused", "--direct-model", "model"]
    )
    profile.validate_args(args)
    dataset = load_dataset()
    schemas = profile.openai_tools(dataset["tools"])

    class FakeBackend:
        def send(self, messages, _tools, **_kwargs):
            content = next(
                message["content"] for message in messages if message["role"] == "tool"
            )
            if not content:
                payload_tokens = 0
            else:
                payload = json.loads(content)
                payload_tokens = 40 + len(payload["padding"].split())
            return success_result(1000 + payload_tokens)

    _payload, record = profile.calibrate_payload(
        args,
        FakeBackend(),
        schemas,
        dataset["tasks"][0],
        PAYLOAD_TOKENS,
    )

    assert record["passed"]
    assert record["authoritative_payload_tokens"] == PAYLOAD_TOKENS
    assert len(record["attempts"]) == EXPECTED_PAYLOAD_ATTEMPTS
    assert all(gate["status"] == "pass" for gate in record["measured_gates"])


def test_checkpoint_calibration_hits_exact_observed_usage():
    profile = load_profile()
    loop = __import__("agent_loop")
    args = profile.build_parser().parse_args(
        ["--artifact-dir", "/tmp/unused", "--direct-model", "model"]
    )
    profile.validate_args(args)
    dataset = load_dataset()
    schemas = profile.openai_tools(dataset["tools"])

    class FakeBackend:
        spec = profile.BackendPath("direct", "ollama", "http://unused", "model")

        def send(self, messages, _tools, *, marker="", **_kwargs):
            content = [
                message["content"] for message in messages if message["role"] == "tool"
            ][-1]
            filler = len(json.loads(content)["padding"].split())
            return success_result(7000 + filler, marker)

    messages, _measured, record = profile.calibrate_and_measure_checkpoint(
        args,
        FakeBackend(),
        schemas,
        loop.AgentConversation("system").snapshot(),
        dataset["tasks"][0],
        EXPECTED_CHECKPOINT,
        "append_only",
    )

    assert record["status"] == "success"
    assert record["observed_prompt_tokens"] == EXPECTED_CHECKPOINT
    assert [message["role"] for message in messages[-4:]] == [
        "user",
        "assistant",
        "tool",
        "user",
    ]


def test_live_quality_starts_auto_and_retries_actual_tool_error():
    profile = load_profile()
    loop = __import__("agent_loop")
    dataset = load_dataset()
    schemas = profile.openai_tools(dataset["tools"])
    tools = profile.native_tools(schemas)
    task = next(
        task for task in dataset["tasks"] if task["id"] == "retry_stock_after_error"
    )
    args = profile.build_parser().parse_args(
        ["--artifact-dir", "/tmp/unused", "--direct-model", "model"]
    )
    profile.validate_args(args)

    class FakeBackend:
        spec = profile.BackendPath("direct", "ollama", "http://unused", "model")

        def __init__(self):
            self.choices = []

        def send(
            self,
            messages,
            _tools,
            *,
            marker="",
            tool_choice=None,
            **_kwargs,
        ):
            self.choices.append(tool_choice)
            if tool_choice == "none":
                return success_result(8200, marker)
            call = {
                "id": f"call_{len(self.choices)}",
                "type": "function",
                "function": {
                    "name": "get_stock_price",
                    "arguments": '{"ticker":"AAPL"}',
                },
            }
            result = success_result(8192)
            result["json"]["choices"][0]["message"] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [call],
            }
            result["message"] = result["json"]["choices"][0]["message"]
            return result

    backend = FakeBackend()
    row = profile.run_quality_task(
        args,
        backend,
        schemas,
        tools,
        loop.AgentConversation("accumulated").snapshot(),
        EXPECTED_CHECKPOINT,
        task,
    )

    assert backend.choices[0] == "auto"
    assert row["step_correct"]
    assert row["output_correct"]
    assert row["tool_execution_errors"] == 1
    assert row["tool_retries"] == 1
    assert any(message["role"] == "tool" for message in row["branch_messages"])


def test_dry_run_plans_direct_and_router_without_execution(tmp_path):
    profile = load_profile()
    artifact_dir = tmp_path / "replay"

    exit_code = profile.main(
        [
            "--artifact-dir",
            str(artifact_dir),
            "--direct-model",
            "gemma",
            "--router-url",
            "http://router/v1",
            "--router-model",
            "auto",
            "--dry-run",
        ]
    )
    manifest = json.loads((artifact_dir / "manifest.json").read_text())

    assert exit_code == 0
    assert [path["label"] for path in manifest["paths"]] == ["direct", "router"]
    assert manifest["dataset"]["tools"] == EXPECTED_TOOL_COUNT
    assert manifest["dataset"]["tasks"] == EXPECTED_TASK_COUNT
    assert (
        manifest["serving_allocation"]["near_limit_observed_input_target"]
        + manifest["serving_allocation"]["output_reservation_tokens"]
        + manifest["serving_allocation"]["headroom_tokens"]
        == manifest["serving_allocation"]["context_window"]
    )
    assert manifest["runtime_provenance_file"] is None
    assert manifest["history_semantics"]["fixed"].startswith("append-only")
    assert manifest["history_semantics"]["branch_probe"].startswith("50%")
    assert manifest["history_semantics"]["compaction"].startswith("not applied")
    assert (artifact_dir / "checksums.sha256").exists()
