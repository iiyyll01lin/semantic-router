"""Opt-in real agent-loop runner for the live task benchmark."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_loop import (
    AgentConversation,
    DeterministicToolExecutor,
    NativeTool,
    run_agent_turn,
    standard_tool_parameters,
)


@dataclass(frozen=True)
class TaskLoopHooks:
    session_id_for: Callable[..., str]
    send_chat_messages: Callable[..., dict[str, Any]]
    response_id: Callable[[dict[str, Any]], str]
    row_from_result: Callable[..., dict[str, Any]]
    summarize: Callable[..., dict[str, Any]]
    scoring_instruction: Callable[[tuple[str, ...]], str]
    dry_response: Callable[..., dict[str, Any]]


def run_agent_tasks(
    args: Any,
    tasks: tuple[Any, ...],
    hooks: TaskLoopHooks,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    for repetition in range(max(1, args.task_repetitions)):
        for task_index, task in enumerate(tasks):
            rows.extend(
                run_agent_task_instance(
                    args,
                    task,
                    task_index,
                    repetition,
                    hooks,
                )
            )
    elapsed = time.perf_counter() - started
    return rows, hooks.summarize(rows, elapsed, args.label)


def run_agent_task_instance(
    args: Any,
    task: Any,
    task_index: int,
    repetition: int,
    hooks: TaskLoopHooks,
) -> list[dict[str, Any]]:
    session_id = hooks.session_id_for(args, task, task_index, repetition)
    conversation = AgentConversation(
        "You are a concise coding agent. Use native tools, retain their results, "
        "and follow exact answer-token instructions."
    )
    tools = native_tools_for_task(task)
    executor = task_tool_executor(task)
    request_state = {
        "previous_response_id": "",
        "request_index": 0,
    }
    previous_selected_model = ""
    rows: list[dict[str, Any]] = []

    for turn_index, turn in enumerate(task.turns):
        prompt = turn.prompt
        if turn.expected_terms:
            prompt = prompt + "\n\n" + hooks.scoring_instruction(turn.expected_terms)
        if turn.tool_name:
            prompt = (
                f"{prompt}\n\nCall {turn.tool_name} with task={task.name!r} "
                f"and turn={turn_index}. Use the returned local evidence."
            )
        send_request = _request_sender(
            args,
            task,
            turn,
            turn_index,
            session_id,
            request_state,
            hooks,
        )
        attempts = run_agent_turn(
            conversation=conversation,
            prompt=prompt,
            tools=tools,
            send_request=send_request,
            executor=executor,
            expected_phase=turn.phase,
            forced_tool_name=turn.tool_name,
            retry_limit=max(0, getattr(args, "tool_retry_limit", 1)),
        )
        previous_selected_model = _append_attempt_rows(
            rows,
            attempts,
            args,
            task,
            task_index,
            repetition,
            turn_index,
            turn,
            session_id,
            previous_selected_model,
            hooks,
        )
    return rows


def native_tools_for_task(task: Any) -> tuple[NativeTool, ...]:
    names = dict.fromkeys(turn.tool_name for turn in task.turns if turn.tool_name)
    return tuple(
        NativeTool(
            name=name,
            description=f"Return deterministic local evidence from {name}.",
            parameters=standard_tool_parameters(),
        )
        for name in names
    )


def task_tool_executor(task: Any) -> DeterministicToolExecutor:
    handlers = {
        tool.name: _task_tool_handler(task, tool.name)
        for tool in native_tools_for_task(task)
    }
    fail_first = {
        turn.tool_name: turn.tool_failures_before_success
        for turn in task.turns
        if turn.tool_name and turn.tool_failures_before_success
    }
    return DeterministicToolExecutor(handlers, fail_first=fail_first)


def _task_tool_handler(
    task: Any, tool_name: str
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("task") != task.name:
            raise ValueError("task argument does not match active task")
        turn_index = int(arguments.get("turn", -1))
        if not 0 <= turn_index < len(task.turns):
            raise ValueError("turn argument is outside active task")
        turn = task.turns[turn_index]
        if turn.tool_name != tool_name:
            raise ValueError("tool does not match planned turn")
        return {
            "task": task.name,
            "turn": turn_index,
            "observation": turn.tool_result,
        }

    return handler


def _request_sender(
    args: Any,
    task: Any,
    turn: Any,
    turn_index: int,
    session_id: str,
    request_state: dict[str, Any],
    hooks: TaskLoopHooks,
) -> Callable[..., dict[str, Any]]:
    def send_request(
        messages: list[dict[str, Any]],
        openai_tools: list[dict[str, Any]],
        tool_choice: Any,
        expected_phase: str,
        attempt_kind: str,
    ) -> dict[str, Any]:
        previous_id = str(request_state["previous_response_id"])
        if args.dry_run:
            result = _dry_agent_response(
                task,
                turn,
                turn_index,
                expected_phase,
                tool_choice,
                attempt_kind,
                hooks,
            )
        else:
            result = hooks.send_chat_messages(
                args,
                messages,
                session_id,
                previous_id,
                tools=openai_tools,
                tool_choice=tool_choice,
            )
        request_index = int(request_state["request_index"])
        result["_previous_response_id"] = previous_id
        result["_previous_response_id_sent"] = bool(
            args.include_previous_response_id and previous_id
        )
        result["_cache_state"] = "cold" if request_index == 0 else "warm"
        result["_attempt_kind"] = attempt_kind
        result["_expected_phase"] = expected_phase
        result["_history_message_count"] = len(messages)
        request_state["request_index"] = request_index + 1
        request_state["previous_response_id"] = hooks.response_id(result)
        return result

    return send_request


def _append_attempt_rows(
    rows: list[dict[str, Any]],
    attempts: list[Any],
    args: Any,
    task: Any,
    task_index: int,
    repetition: int,
    turn_index: int,
    turn: Any,
    session_id: str,
    previous_selected_model: str,
    hooks: TaskLoopHooks,
) -> str:
    for turn_attempt, attempt in enumerate(attempts):
        result = attempt.result
        result["_turn_attempt"] = turn_attempt
        result["_attempt_kind"] = attempt.kind
        result["_expected_phase"] = attempt.expected_phase
        result["_final_response"] = attempt.final_response
        result["_tool_executions"] = [
            {
                "name": execution.name,
                "arguments": execution.arguments,
                "is_error": execution.is_error,
                "attempt": execution.attempt,
            }
            for execution in attempt.tool_executions
        ]
        row = hooks.row_from_result(
            args,
            task,
            task_index,
            repetition,
            turn_index,
            turn,
            session_id,
            result,
            previous_selected_model,
            str(result.get("_previous_response_id") or ""),
        )
        rows.append(row)
        if row["selected_model"]:
            previous_selected_model = row["selected_model"]
    return previous_selected_model


def _dry_agent_response(
    task: Any,
    turn: Any,
    turn_index: int,
    expected_phase: str,
    tool_choice: Any,
    attempt_kind: str,
    hooks: TaskLoopHooks,
) -> dict[str, Any]:
    result = hooks.dry_response(task, turn_index)
    result["headers"]["x-vsr-session-phase"] = expected_phase
    message: dict[str, Any] = {
        "role": "assistant",
        "content": (
            " ".join(turn.expected_terms) if turn.expected_terms else "Acknowledged."
        ),
    }
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        name = function.get("name") if isinstance(function, dict) else ""
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": (f"dry_call_{task.name}_{turn_index}_{attempt_kind}"),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(
                            {"task": task.name, "turn": turn_index}
                        ),
                    },
                }
            ],
        }
    result["json"]["id"] = f"dry_{task.name}_{turn_index}_{attempt_kind}"
    result["json"]["choices"] = [{"message": message}]
    return result
