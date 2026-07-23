"""Deterministic long-context history primitives for agent benchmarks."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agent_loop import AgentConversation, ToolExecution, parse_tool_arguments

PAYLOAD_TOKENS_256 = 256
PAYLOAD_TOKENS_1K = 1_024
PAYLOAD_TOKENS_4K = 4_096
PAYLOAD_TARGETS = (PAYLOAD_TOKENS_256, PAYLOAD_TOKENS_1K, PAYLOAD_TOKENS_4K)
CHECKPOINT_TURN_SCHEDULES = {
    8_192: (256, 1_024, 256, 1_024),
    16_384: (4_096, 1_024, 256, 256),
    32_768: (4_096, 1_024, 4_096, 1_024, 256, 4_096),
    65_152: (
        4_096,
        4_096,
        4_096,
        4_096,
        4_096,
        4_096,
        1_024,
        1_024,
        1_024,
        1_024,
        256,
        256,
        256,
        256,
    ),
}


@dataclass(frozen=True)
class ScriptedToolTurn:
    turn_index: int
    task_id: str
    payload_target_tokens: int
    assistant_message_sha256: str
    tool_result_sha256: tuple[str, ...]
    call_ids: tuple[str, ...]
    tool_names: tuple[str, ...]
    history_sha256: str
    history_message_count: int


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deterministic_tool_payload(
    target_tokens: int,
    filler_units: int,
    *,
    namespace: str,
) -> str:
    """Build realistic, stable JSON with an adjustable one-token filler tail."""
    label = payload_label(target_tokens)
    record = {
        "schema": "vllm-sr/deterministic-tool-result/v1",
        "result_class": label,
        "namespace": namespace,
        "status": "ok",
        "source": "workspace-index",
        "records": [
            {
                "path": "src/semantic-router/pkg/extproc/processor_req_body.go",
                "line": 137,
                "finding": "request body enters the signal and decision pipeline",
                "confidence": 0.99,
            },
            {
                "path": "bench/agent_loop.py",
                "line": 182,
                "finding": "assistant tool calls and tool results remain in history",
                "confidence": 1.0,
            },
            {
                "path": "docs/agent/testing-strategy.md",
                "line": 24,
                "finding": "the smallest relevant deterministic gate runs first",
                "confidence": 0.98,
            },
        ],
        "padding": ("alpha " * max(0, filler_units)).rstrip(),
    }
    return canonical_json(record)


def payload_label(target_tokens: int) -> str:
    if target_tokens == PAYLOAD_TOKENS_1K:
        return "1k"
    if target_tokens == PAYLOAD_TOKENS_4K:
        return "4k"
    return str(target_tokens)


def task_expected_calls(task: dict[str, Any]) -> list[dict[str, Any]]:
    expected = task.get("expect") or {}
    source_calls = expected.get("calls")
    if not isinstance(source_calls, list):
        name = str(expected.get("name") or "")
        if name.lower() in {"", "none", "no_tool"}:
            return []
        source_calls = [{"name": name, "args": expected.get("args") or {}}]

    calls = []
    for source in source_calls:
        arguments: dict[str, Any] = {}
        for path, check in (source.get("args") or {}).items():
            _set_nested(arguments, str(path), deepcopy(check.get("value")))
        calls.append({"name": str(source["name"]), "arguments": arguments})
    return calls


def actionable_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [task for task in tasks if task_expected_calls(task)]


def append_scripted_tool_turn(
    conversation: AgentConversation,
    task: dict[str, Any],
    payload: str,
    *,
    turn_index: int,
    payload_target_tokens: int,
    branch_label: str = "append",
) -> ScriptedToolTurn:
    calls = task_expected_calls(task)
    if not calls:
        raise ValueError(f"task {task.get('id')} has no expected tool call")

    conversation.append_user(str(task["query"]))
    assistant_calls = []
    for call_index, call in enumerate(calls):
        assistant_calls.append(
            {
                "id": (f"call_{branch_label}_{turn_index:03d}_{call_index:02d}"),
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": canonical_json(call["arguments"]),
                },
            }
        )
    normalized = conversation.append_assistant_result(
        {
            "json": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": assistant_calls,
                        }
                    }
                ]
            }
        }
    )
    executions = tuple(
        _scripted_execution(call, payload, turn_index) for call in normalized
    )
    conversation.append_tool_executions(executions)
    assistant_message = conversation.messages[-len(executions) - 1]
    return ScriptedToolTurn(
        turn_index=turn_index,
        task_id=str(task["id"]),
        payload_target_tokens=payload_target_tokens,
        assistant_message_sha256=json_sha256(assistant_message),
        tool_result_sha256=tuple(
            text_sha256(execution.content) for execution in executions
        ),
        call_ids=tuple(execution.call_id for execution in executions),
        tool_names=tuple(execution.name for execution in executions),
        history_sha256=json_sha256(conversation.messages),
        history_message_count=len(conversation.messages),
    )


def append_checkpoint_padding_turn(
    conversation: AgentConversation,
    task: dict[str, Any],
    payload: str,
    *,
    checkpoint: int,
    branch_label: str,
) -> ScriptedToolTurn:
    return append_scripted_tool_turn(
        conversation,
        task,
        payload,
        turn_index=checkpoint,
        payload_target_tokens=0,
        branch_label=branch_label,
    )


def is_message_prefix(
    prefix: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> bool:
    return len(prefix) <= len(messages) and prefix == messages[: len(prefix)]


def clone_conversation(messages: list[dict[str, Any]]) -> AgentConversation:
    return AgentConversation.from_messages(messages)


def snapshot(conversation: AgentConversation) -> list[dict[str, Any]]:
    return conversation.snapshot()


def _scripted_execution(
    tool_call: dict[str, Any],
    payload: str,
    turn_index: int,
) -> ToolExecution:
    function = tool_call.get("function") or {}
    arguments, parse_error = parse_tool_arguments(function.get("arguments"))
    if parse_error:
        raise ValueError(parse_error)
    return ToolExecution(
        call_id=str(tool_call["id"]),
        name=str(function["name"]),
        arguments=arguments,
        content=payload,
        is_error=False,
        attempt=turn_index + 1,
    )


def _set_nested(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = target
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"argument path collision at {path}")
        current = child
    current[parts[-1]] = value
