#!/usr/bin/env python3
"""Run deterministic long-context replay and native-tool quality evidence."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[3]
for import_dir in (REPO_ROOT / "bench", SCRIPT_DIR):
    if import_dir.is_dir() and str(import_dir) not in sys.path:
        sys.path.insert(0, str(import_dir))

from agent_loop import (  # noqa: E402
    AgentConversation,
    DeterministicToolExecutor,
    NativeTool,
    run_agent_turn,
)
from agent_replay import (  # noqa: E402
    CHECKPOINT_TURN_SCHEDULES,
    PAYLOAD_TARGETS,
    actionable_tasks,
    append_checkpoint_padding_turn,
    append_scripted_tool_turn,
    clone_conversation,
    deterministic_tool_payload,
    is_message_prefix,
    json_sha256,
    task_expected_calls,
    text_sha256,
)
from agentic_toolcall_eval import (  # noqa: E402
    aggregate_trials,
    normalize_tool_calls,
    score_task_calls,
)
from agentic_toolcall_support import (  # noqa: E402
    openai_tools,
    stream_ollama_chat,
    stream_openai,
)
from prefill_capacity_profile import (  # noqa: E402
    CONTEXT_WINDOW,
    HEADROOM_TOKENS,
    OUTPUT_TOKENS,
    runtime_facts,
    runtime_stability_gates,
    start_resource_sampler,
    stop_resource_sampler,
    write_checksums,
    write_json,
)
from prefill_matrix import calibration_gates, evaluate_request_gates  # noqa: E402
from prefill_matrix_transport import metric_summary  # noqa: E402

HTTP_OK = 200
HTTP_REDIRECT_START = 300
QUALITY_JSON_GATE = 0.99
QUALITY_TOOL_GATE = 0.95
QUALITY_OUTPUT_GATE = 0.90
DEFAULT_CHECKPOINTS = (8_192, 16_384, 32_768, 65_152)
DEFAULT_QUALITY_CHECKPOINTS = (8_192, 32_768)
APPEND_TURN_MINIMUM = 24


@dataclass(frozen=True)
class BackendPath:
    label: str
    api: str
    base_url: str
    model: str
    api_key: str = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=SCRIPT_DIR / "data" / "agentic-toolcall-tasks.json",
    )
    parser.add_argument("--direct-url", default="http://127.0.0.1:11434")
    parser.add_argument("--direct-model", required=True)
    parser.add_argument("--router-url", default="")
    parser.add_argument("--router-model", default="")
    parser.add_argument("--router-api-key", default="")
    parser.add_argument("--require-router", action="store_true")
    parser.add_argument("--router-blocker-file", type=Path, default=None)
    parser.add_argument("--phases", default="fixed,branch,quality")
    parser.add_argument("--checkpoints", default="8192,16384,32768,65152")
    parser.add_argument("--quality-checkpoints", default="8192,32768")
    parser.add_argument("--quality-limit", type=int, default=0)
    parser.add_argument("--context-window", type=int, default=CONTEXT_WINDOW)
    parser.add_argument("--output-tokens", type=int, default=OUTPUT_TOKENS)
    parser.add_argument("--headroom-tokens", type=int, default=HEADROOM_TOKENS)
    parser.add_argument("--quality-max-tokens", type=int, default=128)
    parser.add_argument("--num-ctx", type=int, default=CONTEXT_WINDOW)
    parser.add_argument("--calibration-max-attempts", type=int, default=8)
    parser.add_argument("--max-target-error-tokens", type=int, default=0)
    parser.add_argument("--max-target-error-percent", type=float, default=5.0)
    parser.add_argument("--tool-retry-limit", type=int, default=1)
    parser.add_argument("--request-timeout", type=float, default=1800)
    parser.add_argument("--runtime-provenance-file", type=Path, default=None)
    parser.add_argument("--resource-interval", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fail-on-quality-gate", action="store_true")
    return parser


def parse_csv_ints(value: str) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(item.strip()) for item in value.split(",") if item))


def selected_phases(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(item.strip() for item in args.phases.split(",") if item.strip())


def validate_args(args: argparse.Namespace) -> None:
    args.checkpoint_values = parse_csv_ints(args.checkpoints)
    args.quality_checkpoint_values = parse_csv_ints(args.quality_checkpoints)
    validate_context_args(args)
    validate_quality_args(args)
    validate_path_args(args)
    validate_execution_args(args)


def validate_context_args(args: argparse.Namespace) -> None:
    if args.context_window != CONTEXT_WINDOW or args.num_ctx != CONTEXT_WINDOW:
        raise ValueError("this replay profile requires the verified 65,536 context")
    if args.output_tokens != OUTPUT_TOKENS:
        raise ValueError("this replay profile reserves exactly 256 output tokens")
    if args.headroom_tokens != HEADROOM_TOKENS:
        raise ValueError("this replay profile reserves exactly 128 headroom tokens")
    if args.checkpoint_values != DEFAULT_CHECKPOINTS:
        raise ValueError("fixed replay checkpoints must be 8192,16384,32768,65152")
    required = max(args.checkpoint_values) + args.output_tokens + args.headroom_tokens
    if required > args.context_window:
        raise ValueError("near-limit checkpoint exceeds the context budget")


def validate_quality_args(args: argparse.Namespace) -> None:
    if not set(args.quality_checkpoint_values) <= set(args.checkpoint_values):
        raise ValueError("quality checkpoints must come from fixed checkpoints")
    if not set(DEFAULT_QUALITY_CHECKPOINTS) <= set(args.quality_checkpoint_values):
        raise ValueError("quality evidence must include both 8K and 32K")
    if args.quality_limit < 0:
        raise ValueError("--quality-limit must be non-negative")


def validate_path_args(args: argparse.Namespace) -> None:
    if args.router_url and not args.router_model:
        raise ValueError("--router-model is required with --router-url")
    if args.require_router and not args.router_url:
        raise ValueError("--require-router requires --router-url")


def validate_execution_args(args: argparse.Namespace) -> None:
    if args.calibration_max_attempts <= 0:
        raise ValueError("--calibration-max-attempts must be positive")
    unknown = set(selected_phases(args)) - {"fixed", "branch", "quality"}
    if unknown:
        raise ValueError(f"unknown phases: {','.join(sorted(unknown))}")


class ChatBackend:
    def __init__(self, spec: BackendPath, args: argparse.Namespace) -> None:
        self.spec = spec
        self.args = args

    def send(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int,
        marker: str = "",
        tool_choice: Any = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if self.spec.api == "ollama":
                metrics = stream_ollama_chat(
                    self.spec.base_url,
                    self.spec.model,
                    messages,
                    max_tokens,
                    self.args.request_timeout,
                    tools=tools,
                    tool_choice=tool_choice,
                    num_ctx=self.args.num_ctx,
                )
            else:
                metrics = stream_openai(
                    self.spec.base_url,
                    self.spec.model,
                    messages,
                    max_tokens,
                    self.args.request_timeout,
                    api_key=self.spec.api_key,
                    tools=tools,
                    tool_choice=tool_choice,
                )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            metrics = failed_metrics(
                exc.code,
                f"HTTPError: {body[:1000]}",
                time.perf_counter() - started,
            )
        except (
            json.JSONDecodeError,
            OSError,
            TimeoutError,
            ValueError,
            urllib.error.URLError,
        ) as exc:
            metrics = failed_metrics(
                0,
                f"{type(exc).__name__}: {exc}",
                time.perf_counter() - started,
            )
        return canonical_result(self.spec, metrics, marker)


def failed_metrics(status: int, error: str, wall_s: float) -> dict[str, Any]:
    return {
        "status": status,
        "success": False,
        "text": "",
        "reasoning": "",
        "message": {"role": "assistant", "content": ""},
        "tool_calls": [],
        "wall_s": wall_s,
        "ttft_ms": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "prompt_token_source": "",
        "completion_token_source": "",
        "prompt_eval_duration_ms": None,
        "prefill_tps": None,
        "decode_tps": None,
        "cached_tokens": None,
        "cached_token_field_present": False,
        "cached_token_source": "",
        "finish_reason": "",
        "truncated": False,
        "payload_sha256": "",
        "messages_sha256": "",
        "tool_schema_sha256": "",
        "response_sha256": "",
        "response_headers": {},
        "semantic_cache_hit": None,
        "usage": {},
        "error": error,
    }


def canonical_result(
    backend: BackendPath,
    metrics: dict[str, Any],
    marker: str,
) -> dict[str, Any]:
    status = int(metrics.get("status") or 0)
    success = bool(metrics.get("success")) and HTTP_OK <= status < HTTP_REDIRECT_START
    message = metrics.get("message")
    if not isinstance(message, dict):
        message = {"role": "assistant", "content": str(metrics.get("text") or "")}
    usage = metrics.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    usage.setdefault("prompt_tokens", metrics.get("prompt_tokens"))
    usage.setdefault("completion_tokens", metrics.get("completion_tokens"))
    response_id = "replay_" + str(metrics.get("response_sha256") or "")[:24]
    result = {
        **metrics,
        "status": status,
        "success": success,
        "marker_correct": bool(marker) and marker in str(metrics.get("text") or ""),
        "error": str(metrics.get("error") or ""),
        "headers": metrics.get("response_headers") or {},
        "latency_ms": round(float(metrics.get("wall_s") or 0) * 1000, 3),
        "json": {
            "id": response_id,
            "model": backend.model,
            "choices": [
                {
                    "message": message,
                    "finish_reason": metrics.get("finish_reason"),
                }
            ],
            "usage": usage,
        },
    }
    return result


def load_dataset(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not value.get("tools") or not value.get("tasks"):
        raise ValueError("tool dataset must contain non-empty tools and tasks")
    return value


def native_tools(schemas: list[dict[str, Any]]) -> tuple[NativeTool, ...]:
    result = []
    for schema in schemas:
        function = schema["function"]
        result.append(
            NativeTool(
                name=str(function["name"]),
                description=str(function.get("description") or ""),
                parameters=function["parameters"],
            )
        )
    return tuple(result)


def calibration_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        max_target_error_tokens=args.max_target_error_tokens,
        max_target_error_percent=args.max_target_error_percent,
        allow_missing_completion_usage=False,
        require_semantic_cache_observation=False,
        forbid_semantic_cache_hits=False,
    )


def calibration_history(
    task: dict[str, Any],
    payload: str,
    target: int,
) -> list[dict[str, Any]]:
    conversation = AgentConversation(
        "You are a deterministic tool-history tokenizer calibration assistant."
    )
    append_scripted_tool_turn(
        conversation,
        task,
        payload,
        turn_index=target,
        payload_target_tokens=target,
        branch_label=f"payload{target}",
    )
    conversation.append_user("Reply with OK and do not call another tool.")
    return conversation.snapshot()


def calibrate_payload(
    args: argparse.Namespace,
    backend: ChatBackend,
    schemas: list[dict[str, Any]],
    task: dict[str, Any],
    target: int,
) -> tuple[str, dict[str, Any]]:
    gate_args = calibration_args(args)
    baseline = backend.send(
        calibration_history(task, "", target),
        schemas,
        max_tokens=1,
        tool_choice="none",
    )
    baseline_tokens = optional_int(baseline.get("prompt_tokens"))
    attempts = []
    filler_units = max(1, target)
    passed = False
    for attempt_index in range(args.calibration_max_attempts):
        payload = deterministic_tool_payload(
            target,
            filler_units,
            namespace="A",
        )
        result = backend.send(
            calibration_history(task, payload, target),
            schemas,
            max_tokens=1,
            tool_choice="none",
        )
        observed = optional_int(result.get("prompt_tokens"))
        delta = (
            observed - baseline_tokens
            if observed is not None and baseline_tokens is not None
            else None
        )
        adjusted = {**result, "prompt_tokens": delta}
        gates = calibration_gates(gate_args, target, adjusted)
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "filler_units": filler_units,
                "observed_prompt_tokens": observed,
                "baseline_prompt_tokens": baseline_tokens,
                "authoritative_payload_tokens": delta,
                "payload_sha256": text_sha256(payload),
                "gates": gates,
            }
        )
        if all(item["status"] == "pass" for item in gates):
            passed = True
            break
        if delta is None or not result.get("success"):
            break
        filler_units = max(0, filler_units + target - delta)

    measured_payload = deterministic_tool_payload(
        target,
        filler_units,
        namespace="B",
    )
    measured = backend.send(
        calibration_history(task, measured_payload, target),
        schemas,
        max_tokens=1,
        tool_choice="none",
    )
    measured_tokens = optional_int(measured.get("prompt_tokens"))
    measured_delta = (
        measured_tokens - baseline_tokens
        if measured_tokens is not None and baseline_tokens is not None
        else None
    )
    measured_gates = calibration_gates(
        gate_args,
        target,
        {**measured, "prompt_tokens": measured_delta},
    )
    record = {
        "schema": "vllm-sr/agent-replay-payload-calibration/v1",
        "target_tokens": target,
        "passed": passed and all(item["status"] == "pass" for item in measured_gates),
        "baseline_prompt_tokens": baseline_tokens,
        "filler_units": filler_units,
        "authoritative_payload_tokens": measured_delta,
        "payload_sha256": text_sha256(measured_payload),
        "tool_schema_sha256": measured.get("tool_schema_sha256"),
        "attempts": attempts,
        "measured_gates": measured_gates,
    }
    return measured_payload, record


def checkpoint_prompt(target: int, branch_label: str) -> tuple[str, str]:
    marker = f"AGENT_REPLAY_OK_{branch_label}_{target}"
    prompt = (
        "This is a deterministic replay checkpoint. Do not call a tool. "
        f"Reply with exactly {marker}."
    )
    return prompt, marker


def checkpoint_candidate(
    base_messages: list[dict[str, Any]],
    task: dict[str, Any],
    target: int,
    filler_units: int,
    namespace: str,
    branch_label: str,
) -> tuple[list[dict[str, Any]], str]:
    conversation = clone_conversation(base_messages)
    payload = deterministic_tool_payload(
        target,
        filler_units,
        namespace=namespace,
    )
    append_checkpoint_padding_turn(
        conversation,
        task,
        payload,
        checkpoint=target,
        branch_label=branch_label,
    )
    prompt, _marker = checkpoint_prompt(target, branch_label)
    conversation.append_user(prompt)
    return conversation.snapshot(), payload


def calibrate_and_measure_checkpoint(
    args: argparse.Namespace,
    backend: ChatBackend,
    schemas: list[dict[str, Any]],
    base_messages: list[dict[str, Any]],
    task: dict[str, Any],
    target: int,
    branch_label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    gate_args = calibration_args(args)
    attempts = []
    filler_units = 0
    passed = False
    for attempt_index in range(args.calibration_max_attempts):
        messages, payload = checkpoint_candidate(
            base_messages,
            task,
            target,
            filler_units,
            "A",
            branch_label,
        )
        result = backend.send(
            messages,
            schemas,
            max_tokens=1,
            tool_choice="none",
        )
        gates = calibration_gates(gate_args, target, result)
        observed = optional_int(result.get("prompt_tokens"))
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "filler_units": filler_units,
                "observed_prompt_tokens": observed,
                "target_error_tokens": (
                    observed - target if observed is not None else None
                ),
                "payload_sha256": text_sha256(payload),
                "request_payload_sha256": result.get("payload_sha256"),
                "gates": gates,
            }
        )
        if all(item["status"] == "pass" for item in gates):
            passed = True
            break
        if observed is None or not result.get("success"):
            break
        filler_units = max(0, filler_units + target - observed)

    measured_messages, measured_payload = checkpoint_candidate(
        base_messages,
        task,
        target,
        filler_units,
        "B",
        branch_label,
    )
    _prompt, marker = checkpoint_prompt(target, branch_label)
    measured = backend.send(
        measured_messages,
        schemas,
        max_tokens=args.output_tokens,
        marker=marker,
        tool_choice="none",
    )
    measured = evaluate_request_gates(gate_args, target, measured)
    measured_passed = passed and bool(measured.get("gate_passed"))
    record = {
        "schema": "vllm-sr/agent-replay-checkpoint/v1",
        "path": backend.spec.label,
        "checkpoint": target,
        "history_semantics": branch_label,
        "status": "success" if measured_passed else "failed",
        "observed_prompt_tokens": measured.get("prompt_tokens"),
        "completion_tokens": measured.get("completion_tokens"),
        "ttft_ms": rounded(measured.get("ttft_ms")),
        "prefill_duration_ms": rounded(measured.get("prompt_eval_duration_ms")),
        "prefill_tps": rounded(measured.get("prefill_tps")),
        "decode_tps": rounded(measured.get("decode_tps")),
        "finish_reason": measured.get("finish_reason"),
        "truncated": bool(measured.get("truncated")),
        "marker_correct": bool(measured.get("marker_correct")),
        "request_payload_sha256": measured.get("payload_sha256"),
        "messages_sha256": measured.get("messages_sha256"),
        "tool_schema_sha256": measured.get("tool_schema_sha256"),
        "response_sha256": measured.get("response_sha256"),
        "checkpoint_payload_sha256": text_sha256(measured_payload),
        "message_count": len(measured_messages),
        "calibration": {
            "passed": passed,
            "attempt_count": len(attempts),
            "filler_units": filler_units,
            "attempts": attempts,
        },
        "gates": measured.get("gates") or [],
        "error": measured.get("error") or "",
    }
    return measured_messages, measured, record


def append_schedule(
    conversation: AgentConversation,
    schedule: tuple[int, ...],
    tasks: list[dict[str, Any]],
    payloads: dict[int, str],
    events: list[dict[str, Any]],
    *,
    turn_offset: int,
    task_offset: int,
    branch_label: str,
) -> int:
    turn_index = turn_offset
    for local_index, target in enumerate(schedule):
        task = tasks[(task_offset + local_index) % len(tasks)]
        event = append_scripted_tool_turn(
            conversation,
            task,
            payloads[target],
            turn_index=turn_index,
            payload_target_tokens=target,
            branch_label=branch_label,
        )
        events.append(asdict(event))
        turn_index += 1
    return turn_index


def run_fixed_replay(
    args: argparse.Namespace,
    backend: ChatBackend,
    schemas: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    payloads: dict[int, str],
    output_dir: Path,
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
    conversation = AgentConversation(
        "You are a deterministic coding agent replay. Retain every assistant "
        "tool call and every authoritative tool result in the same conversation."
    )
    events: list[dict[str, Any]] = []
    records = []
    quality_bases: dict[int, list[dict[str, Any]]] = {}
    previous_post_checkpoint: list[dict[str, Any]] = conversation.snapshot()
    turn_index = 0
    for checkpoint_index, target in enumerate(args.checkpoint_values):
        schedule = CHECKPOINT_TURN_SCHEDULES[target]
        turn_index = append_schedule(
            conversation,
            schedule,
            tasks,
            payloads,
            events,
            turn_offset=turn_index,
            task_offset=turn_index,
            branch_label="append",
        )
        base_before_padding = conversation.snapshot()
        measured_messages, measured, record = calibrate_and_measure_checkpoint(
            args,
            backend,
            schemas,
            base_before_padding,
            tasks[(turn_index + checkpoint_index) % len(tasks)],
            target,
            "append_only",
        )
        quality_bases[target] = measured_messages[:-1]
        record["regular_tool_turns_total"] = turn_index
        record["append_only_from_previous_checkpoint"] = is_message_prefix(
            previous_post_checkpoint,
            measured_messages,
        )
        record["previous_history_sha256"] = json_sha256(previous_post_checkpoint)
        records.append(record)
        conversation = clone_conversation(measured_messages)
        conversation.append_assistant_result(measured)
        previous_post_checkpoint = conversation.snapshot()

    write_json(output_dir / "raw" / "fixed-history.json", conversation.snapshot())
    write_json(output_dir / "raw" / "fixed-events.json", events)
    write_jsonl(output_dir / "raw" / "fixed-checkpoints.jsonl", records)
    passed = (
        turn_index >= APPEND_TURN_MINIMUM
        and all(record["status"] == "success" for record in records)
        and all(record["append_only_from_previous_checkpoint"] for record in records)
    )
    summary = {
        "schema": "vllm-sr/agent-fixed-replay/v1",
        "path": backend.spec.label,
        "passed": passed,
        "history_semantics": {
            "mode": "append_only",
            "compaction_applied": False,
            "branching_applied": False,
            "same_conversation": True,
        },
        "regular_tool_turns": turn_index,
        "checkpoint_padding_turns": len(records),
        "total_tool_turns": turn_index + len(records),
        "assistant_tool_calls_preserved": True,
        "tool_result_messages_preserved": True,
        "checkpoints": records,
    }
    write_json(output_dir / "summary" / "fixed-replay.json", summary)
    return quality_bases, summary


def run_branch_probe(
    args: argparse.Namespace,
    backend: ChatBackend,
    schemas: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    payloads: dict[int, str],
    quality_bases: dict[int, list[dict[str, Any]]],
    output_dir: Path,
) -> dict[str, Any]:
    source_target = 16_384
    target = 32_768
    source = quality_bases[source_target]
    conversation = clone_conversation(source)
    events: list[dict[str, Any]] = []
    append_schedule(
        conversation,
        CHECKPOINT_TURN_SCHEDULES[target],
        list(reversed(tasks)),
        payloads,
        events,
        turn_offset=10_000,
        task_offset=3,
        branch_label="branch50",
    )
    measured_messages, _measured, record = calibrate_and_measure_checkpoint(
        args,
        backend,
        schemas,
        conversation.snapshot(),
        tasks[-1],
        target,
        "branch_after_16k",
    )
    record["source_checkpoint"] = source_target
    record["target_checkpoint"] = target
    record["nominal_shared_prefix_ratio"] = source_target / target
    record["shared_prefix_message_count"] = len(source)
    record["shared_prefix_sha256"] = json_sha256(source)
    record["source_is_message_prefix"] = is_message_prefix(
        source,
        measured_messages,
    )
    summary = {
        "schema": "vllm-sr/agent-branch-replay/v1",
        "path": backend.spec.label,
        "passed": record["status"] == "success" and record["source_is_message_prefix"],
        "history_semantics": {
            "mode": "branch_from_checkpoint",
            "compaction_applied": False,
            "branching_applied": True,
            "append_only_after_fork": True,
            "nominal_shared_prefix_ratio": 0.5,
        },
        "checkpoint": record,
        "events": events,
    }
    write_json(output_dir / "summary" / "branch-replay.json", summary)
    return summary


def run_quality_suite(
    args: argparse.Namespace,
    backend: ChatBackend,
    schemas: list[dict[str, Any]],
    tools: tuple[NativeTool, ...],
    tasks: list[dict[str, Any]],
    quality_bases: dict[int, list[dict[str, Any]]],
    output_dir: Path,
) -> dict[str, Any]:
    selected_tasks = tasks[: args.quality_limit or None]
    rows: list[dict[str, Any]] = []
    for checkpoint in args.quality_checkpoint_values:
        for task in selected_tasks:
            rows.append(
                run_quality_task(
                    args,
                    backend,
                    schemas,
                    tools,
                    quality_bases[checkpoint],
                    checkpoint,
                    task,
                )
            )
    write_jsonl(output_dir / "raw" / "quality.jsonl", rows)
    summary = quality_summary(rows, backend.spec.label)
    write_json(output_dir / "summary" / "quality.json", summary)
    return summary


def run_quality_task(
    args: argparse.Namespace,
    backend: ChatBackend,
    schemas: list[dict[str, Any]],
    tools: tuple[NativeTool, ...],
    base_messages: list[dict[str, Any]],
    checkpoint: int,
    task: dict[str, Any],
) -> dict[str, Any]:
    task_id = str(task["id"])
    marker = f"QUALITY_RESULT={task_id}"
    prompt = (
        f"{task['query']}\n\nSelect native tools autonomously. Preserve and use "
        "their returned evidence. If no tool is needed, answer directly. "
        f"Your final answer must include exactly {marker}."
    )
    conversation = clone_conversation(base_messages)
    expected_names = [call["name"] for call in task_expected_calls(task)]
    fail_first = (
        {expected_names[0]: 1}
        if task.get("category") == "tool_error" and expected_names
        else {}
    )
    handlers = {
        tool.name: quality_handler(tool.name, task_id, marker) for tool in tools
    }
    executor = DeterministicToolExecutor(handlers, fail_first=fail_first)
    requests: list[dict[str, Any]] = []

    def send_request(
        messages: list[dict[str, Any]],
        request_tools: list[dict[str, Any]],
        tool_choice: Any,
        expected_phase: str,
        attempt_kind: str,
    ) -> dict[str, Any]:
        result = backend.send(
            messages,
            request_tools,
            max_tokens=args.quality_max_tokens,
            marker=marker if attempt_kind in {"model", "tool_followup"} else "",
            tool_choice=tool_choice,
        )
        requests.append(
            {
                "attempt_kind": attempt_kind,
                "expected_phase": expected_phase,
                **request_evidence(result, len(messages)),
            }
        )
        return result

    attempts = run_agent_turn(
        conversation=conversation,
        prompt=prompt,
        tools=tools,
        send_request=send_request,
        executor=executor,
        expected_phase="quality",
        retry_limit=args.tool_retry_limit,
        initial_tool_choice="auto",
    )
    initial = attempts[0].result
    initial_message = (
        (initial.get("json") or {}).get("choices", [{}])[0].get("message", {})
    )
    predicted_calls = normalize_tool_calls(initial_message.get("tool_calls", []))
    structured_valid = predicted_calls is not None
    json_valid, name_ok, args_ok, detail = score_task_calls(
        task,
        predicted_calls,
        structured_valid,
    )
    final_result = next(
        (attempt.result for attempt in reversed(attempts) if attempt.final_response),
        attempts[-1].result,
    )
    final_text = str(final_result.get("text") or "")
    output_correct = marker in final_text and not final_result.get("truncated")
    executions = [
        execution for attempt in attempts for execution in attempt.tool_executions
    ]
    return {
        "schema": "vllm-sr/agent-native-quality-trial/v1",
        "path": backend.spec.label,
        "checkpoint": checkpoint,
        "id": task_id,
        "category": str(task.get("category") or "single_call"),
        "history_semantics": {
            "mode": "branch_from_fixed_checkpoint",
            "compaction_applied": False,
            "branching_applied": True,
            "actual_accumulated_base": True,
        },
        "base_history_sha256": json_sha256(base_messages),
        "base_message_count": len(base_messages),
        "json_valid": json_valid,
        "name_correct": name_ok,
        "args_correct": args_ok,
        "step_correct": json_valid and name_ok and args_ok,
        "arg_detail": detail,
        "predicted_names": [
            str(call.get("name") or "") for call in predicted_calls or []
        ],
        "output_correct": output_correct,
        "output_marker": marker,
        "output_excerpt": final_text[:800],
        "tool_executions": len(executions),
        "tool_execution_errors": sum(execution.is_error for execution in executions),
        "tool_retries": sum(attempt.kind == "tool_retry" for attempt in attempts),
        "request_count": len(requests),
        "requests": requests,
        "branch_messages": conversation.snapshot()[len(base_messages) :],
        "wall_s": rounded(sum(float(row.get("wall_s") or 0) for row in requests)),
        "ttft_ms": requests[0].get("ttft_ms") if requests else None,
        "decode_tps": requests[0].get("decode_tps") if requests else None,
        "prefill_tps": requests[0].get("prefill_tps") if requests else None,
        "prompt_eval_duration_ms": (
            requests[0].get("prefill_duration_ms") if requests else None
        ),
        "prompt_tokens": requests[0].get("prompt_tokens") if requests else None,
        "completion_tokens": sum(
            int(row.get("completion_tokens") or 0) for row in requests
        ),
        "cached_tokens": sum(int(row.get("cached_tokens") or 0) for row in requests),
        "cached_token_field_present": any(
            row.get("cached_token_field_present") for row in requests
        ),
        "cached_token_source": next(
            (
                str(row["cached_token_source"])
                for row in requests
                if row.get("cached_token_source")
            ),
            "",
        ),
        "cache_state": "branched",
        "error": "; ".join(str(row["error"]) for row in requests if row.get("error")),
    }


def quality_handler(
    tool_name: str,
    task_id: str,
    marker: str,
) -> Any:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "vllm-sr/deterministic-quality-result/v1",
            "status": "ok",
            "tool": tool_name,
            "task": task_id,
            "arguments": arguments,
            "observation": (
                "The requested operation completed in the deterministic sandbox."
            ),
            "required_final_marker": marker,
        }

    return handler


def request_evidence(
    result: dict[str, Any],
    message_count: int,
) -> dict[str, Any]:
    return {
        "success": bool(result.get("success")),
        "status": result.get("status"),
        "error": result.get("error") or "",
        "history_message_count": message_count,
        "prompt_tokens": result.get("prompt_tokens"),
        "completion_tokens": result.get("completion_tokens"),
        "cached_tokens": result.get("cached_tokens"),
        "cached_token_field_present": bool(result.get("cached_token_field_present")),
        "cached_token_source": result.get("cached_token_source") or "",
        "wall_s": rounded(result.get("wall_s")),
        "ttft_ms": rounded(result.get("ttft_ms")),
        "prefill_duration_ms": rounded(result.get("prompt_eval_duration_ms")),
        "prefill_tps": rounded(result.get("prefill_tps")),
        "decode_tps": rounded(result.get("decode_tps")),
        "finish_reason": result.get("finish_reason"),
        "truncated": bool(result.get("truncated")),
        "payload_sha256": result.get("payload_sha256"),
        "messages_sha256": result.get("messages_sha256"),
        "tool_schema_sha256": result.get("tool_schema_sha256"),
        "response_sha256": result.get("response_sha256"),
    }


def quality_summary(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    by_checkpoint: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_checkpoint[int(row["checkpoint"])].append(row)
        by_category[str(row["category"])].append(row)
    selection = aggregate_trials(rows)
    output_rate = ratio(rows, "output_correct")
    execution_success = all(
        request.get("success") for row in rows for request in row.get("requests") or []
    )
    acceptance = [
        quality_gate(
            "structured_valid_rate",
            selection.get("json_valid_rate"),
            QUALITY_JSON_GATE,
        ),
        quality_gate(
            "tool_name_and_args_rate",
            selection.get("step_correct_rate"),
            QUALITY_TOOL_GATE,
        ),
        quality_gate("output_correct_rate", output_rate, QUALITY_OUTPUT_GATE),
    ]
    return {
        "schema": "vllm-sr/agent-native-quality/v1",
        "path": label,
        "execution_passed": execution_success,
        "acceptance_passed": all(row["status"] == "pass" for row in acceptance),
        "tasks": len(rows),
        "requests": sum(int(row["request_count"]) for row in rows),
        "tool_executions": sum(int(row["tool_executions"]) for row in rows),
        "tool_execution_errors": sum(int(row["tool_execution_errors"]) for row in rows),
        "tool_retries": sum(int(row["tool_retries"]) for row in rows),
        "selection": selection,
        "output_correct_rate": output_rate,
        "acceptance_gates": acceptance,
        "history_semantics": {
            "mode": "branch_from_fixed_checkpoint",
            "compaction_applied": False,
            "branching_applied": True,
            "base_conversations_are_accumulated": True,
            "tasks_do_not_reset_to_short_system_user_prompts": True,
        },
        "checkpoints": {
            str(checkpoint): quality_slice(values)
            for checkpoint, values in sorted(by_checkpoint.items())
        },
        "categories": {
            category: quality_slice(values)
            for category, values in sorted(by_category.items())
        },
    }


def quality_slice(rows: list[dict[str, Any]]) -> dict[str, Any]:
    requests = [request for row in rows for request in row.get("requests") or []]
    return {
        "tasks": len(rows),
        "requests": len(requests),
        "json_valid_rate": ratio(rows, "json_valid"),
        "name_correct_rate": ratio(rows, "name_correct"),
        "args_correct_rate": ratio(rows, "args_correct"),
        "step_correct_rate": ratio(rows, "step_correct"),
        "output_correct_rate": ratio(rows, "output_correct"),
        "prompt_tokens": metric_summary(numeric_values(requests, "prompt_tokens")),
        "ttft_ms": metric_summary(numeric_values(requests, "ttft_ms")),
        "prefill_duration_ms": metric_summary(
            numeric_values(requests, "prefill_duration_ms")
        ),
        "prefill_tps": metric_summary(numeric_values(requests, "prefill_tps")),
        "decode_tps": metric_summary(numeric_values(requests, "decode_tps")),
        "finish_reasons": counts(
            str(request.get("finish_reason") or "missing") for request in requests
        ),
        "truncations": sum(bool(request.get("truncated")) for request in requests),
        "errors": counts(
            str(request["error"]) for request in requests if request.get("error")
        ),
    }


def quality_gate(name: str, observed: Any, minimum: float) -> dict[str, Any]:
    passed = isinstance(observed, (int, float)) and observed >= minimum
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "observed": observed,
        "minimum": minimum,
    }


def backend_paths(args: argparse.Namespace) -> tuple[list[BackendPath], dict[str, Any]]:
    paths = [
        BackendPath(
            "direct",
            "ollama",
            args.direct_url,
            args.direct_model,
        )
    ]
    if args.router_url:
        paths.append(
            BackendPath(
                "router",
                "openai",
                args.router_url,
                args.router_model,
                args.router_api_key,
            )
        )
        router = {"requested": True, "status": "planned"}
    else:
        blocker = read_optional_json(args.router_blocker_file)
        router = {
            "requested": False,
            "status": (blocker or {}).get("status", "not_configured"),
            "blocker": blocker,
        }
    return paths, router


def prepare_artifacts(args: argparse.Namespace) -> None:
    if args.artifact_dir.exists() and next(args.artifact_dir.iterdir(), None):
        raise ValueError("artifact directory must be new or empty")
    for child in ("raw", "summary", "logs"):
        (args.artifact_dir / child).mkdir(parents=True, exist_ok=True)


def build_manifest(
    args: argparse.Namespace,
    dataset: dict[str, Any],
    schemas: list[dict[str, Any]],
    paths: list[BackendPath],
    router: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": "vllm-sr/agentic-replay-manifest/v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phases": selected_phases(args),
        "paths": [
            {
                "label": path.label,
                "api": path.api,
                "base_url": path.base_url,
                "model": path.model,
            }
            for path in paths
        ],
        "router": router,
        "dataset": {
            "path": str(args.dataset),
            "schema": dataset.get("schema"),
            "tasks": len(dataset["tasks"]),
            "tools": len(dataset["tools"]),
            "dataset_sha256": json_sha256(dataset),
            "tool_schema_sha256": json_sha256(schemas),
        },
        "serving_allocation": {
            "context_window": args.context_window,
            "output_reservation_tokens": args.output_tokens,
            "headroom_tokens": args.headroom_tokens,
            "near_limit_observed_input_target": max(args.checkpoint_values),
            "parallel_slots": 1,
        },
        "payload_targets": list(PAYLOAD_TARGETS),
        "fixed_checkpoints": list(args.checkpoint_values),
        "quality_checkpoints": list(args.quality_checkpoint_values),
        "quality_task_limit": args.quality_limit,
        "history_semantics": {
            "fixed": "append-only same conversation",
            "branch_probe": "50% nominal prefix fork from observed 16K to 32K",
            "quality": "one live branch per task from accumulated checkpoint history",
            "compaction": "not applied or conflated with branching",
        },
        "runtime_provenance_file": (
            str(args.runtime_provenance_file) if args.runtime_provenance_file else None
        ),
    }


def execute_path(
    args: argparse.Namespace,
    spec: BackendPath,
    dataset: dict[str, Any],
    schemas: list[dict[str, Any]],
    tools: tuple[NativeTool, ...],
) -> dict[str, Any]:
    output_dir = args.artifact_dir / spec.label
    for child in ("raw", "summary"):
        (output_dir / child).mkdir(parents=True, exist_ok=True)
    backend = ChatBackend(spec, args)
    action_tasks = actionable_tasks(dataset["tasks"])
    payloads: dict[int, str] = {}
    calibrations = []
    for target in PAYLOAD_TARGETS:
        payload, record = calibrate_payload(
            args,
            backend,
            schemas,
            action_tasks[0],
            target,
        )
        payloads[target] = payload
        calibrations.append(record)
    write_json(output_dir / "raw" / "payloads.json", payloads)
    write_jsonl(
        output_dir / "raw" / "payload-calibration.jsonl",
        calibrations,
    )

    fixed_summary: dict[str, Any] = {"not_run": True}
    branch_summary: dict[str, Any] = {"not_run": True}
    quality: dict[str, Any] = {"not_run": True}
    quality_bases: dict[int, list[dict[str, Any]]] = {}
    if "fixed" in selected_phases(args):
        quality_bases, fixed_summary = run_fixed_replay(
            args,
            backend,
            schemas,
            action_tasks,
            payloads,
            output_dir,
        )
    if "branch" in selected_phases(args):
        if not quality_bases:
            raise ValueError("branch phase requires fixed phase")
        branch_summary = run_branch_probe(
            args,
            backend,
            schemas,
            action_tasks,
            payloads,
            quality_bases,
            output_dir,
        )
    if "quality" in selected_phases(args):
        if not quality_bases:
            raise ValueError("quality phase requires fixed phase")
        quality = run_quality_suite(
            args,
            backend,
            schemas,
            tools,
            dataset["tasks"],
            quality_bases,
            output_dir,
        )
    infrastructure_passed = (
        all(record["passed"] for record in calibrations)
        and bool(fixed_summary.get("passed"))
        and (branch_summary.get("not_run") or branch_summary.get("passed"))
        and (quality.get("not_run") or quality.get("execution_passed"))
    )
    summary = {
        "schema": "vllm-sr/agentic-replay-path/v1",
        "path": spec.label,
        "api": spec.api,
        "model": spec.model,
        "infrastructure_passed": bool(infrastructure_passed),
        "quality_acceptance_passed": quality.get("acceptance_passed"),
        "payload_calibration": calibrations,
        "fixed": fixed_summary,
        "branch": branch_summary,
        "quality": quality,
    }
    write_json(output_dir / "summary" / "path-summary.json", summary)
    return summary


def execute_profile(
    args: argparse.Namespace,
    dataset: dict[str, Any],
    schemas: list[dict[str, Any]],
    tools: tuple[NativeTool, ...],
    paths: list[BackendPath],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    before = runtime_facts(args.direct_url, args.direct_model)
    write_json(args.artifact_dir / "runtime-before.json", before)
    resource_trace, resource_pid = start_resource_sampler(
        SCRIPT_DIR,
        args.artifact_dir,
        args.resource_interval,
    )
    resource_summary = args.artifact_dir / "summary" / "resources.json"
    path_summaries = []
    try:
        for path in paths:
            path_summaries.append(execute_path(args, path, dataset, schemas, tools))
    finally:
        stop_resource_sampler(
            SCRIPT_DIR,
            resource_trace,
            resource_pid,
            resource_summary,
        )
    after = runtime_facts(args.direct_url, args.direct_model)
    write_json(args.artifact_dir / "runtime-after.json", after)
    stability = runtime_stability_gates(
        before,
        after,
        args.context_window,
    )
    stability_passed = all(gate["status"] == "pass" for gate in stability)
    infrastructure_passed = stability_passed and all(
        path["infrastructure_passed"] for path in path_summaries
    )
    quality_passed = all(
        path.get("quality_acceptance_passed") is not False for path in path_summaries
    )
    return {
        "schema": "vllm-sr/agentic-replay-profile/v1",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "infrastructure_passed": infrastructure_passed,
        "quality_acceptance_passed": quality_passed,
        "passed": infrastructure_passed
        and (quality_passed or not args.fail_on_quality_gate),
        "manifest": manifest,
        "paths": path_summaries,
        "runtime_stability_gates": stability,
        "runtime_before": str(args.artifact_dir / "runtime-before.json"),
        "runtime_after": str(args.artifact_dir / "runtime-after.json"),
        "resource_trace": str(resource_trace),
        "resource_summary": str(resource_summary),
    }


def optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def rounded(value: Any, digits: int = 4) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), digits)


def numeric_values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [
        float(row[key])
        for row in rows
        if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)
    ]


def ratio(rows: list[dict[str, Any]], key: str) -> float | None:
    if not rows:
        return None
    return round(sum(bool(row.get(key)) for row in rows) / len(rows), 4)


def counts(values: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[str(value)] = result.get(str(value), 0) + 1
    return dict(sorted(result.items()))


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def read_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
        prepare_artifacts(args)
        dataset = load_dataset(args.dataset)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    schemas = openai_tools(dataset["tools"])
    tools = native_tools(schemas)
    paths, router = backend_paths(args)
    manifest = build_manifest(args, dataset, schemas, paths, router)
    write_json(args.artifact_dir / "manifest.json", manifest)
    if args.dry_run:
        write_checksums(args.artifact_dir)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    summary = execute_profile(
        args,
        dataset,
        schemas,
        tools,
        paths,
        manifest,
    )
    summary_path = args.artifact_dir / "summary" / "agentic-replay-profile.json"
    write_json(summary_path, summary)
    checksums = write_checksums(args.artifact_dir)
    print(
        json.dumps(
            {
                "passed": summary["passed"],
                "infrastructure_passed": summary["infrastructure_passed"],
                "quality_acceptance_passed": summary["quality_acceptance_passed"],
                "summary": str(summary_path),
                "checksums": str(checksums),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
