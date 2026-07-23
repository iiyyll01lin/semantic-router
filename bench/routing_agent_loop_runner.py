"""Opt-in real agent-loop runner for the live routing workload."""

from __future__ import annotations

import json
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

TOOL_NAME = "lookup_task_status"


@dataclass(frozen=True)
class RoutingLoopHooks:
    plan_type: Callable[..., Any]
    phase_for_turn: Callable[[str, int], str]
    pause_before_turn: Callable[[Any, str, int], None]
    prompt_for_phase: Callable[[str, int, int, str], str]
    build_request_body: Callable[..., dict[str, Any]]
    post_json: Callable[..., dict[str, Any]]
    dry_response: Callable[[Any], dict[str, Any]]
    response_id: Callable[[dict[str, Any]], str]
    row_from_result: Callable[..., dict[str, Any]]


def run_agent_session(
    args: Any,
    session_idx: int,
    base_headers: dict[str, str],
    hooks: RoutingLoopHooks,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    session_id = f"{args.label}-{args.scenario}-{session_idx:04d}"
    conversation = AgentConversation(
        "You are a concise benchmark assistant. Use native tools when requested."
    )
    tool = _routing_tool()
    executor = DeterministicToolExecutor(
        {
            tool.name: lambda arguments: {
                "status": "tool result ready",
                "task": arguments["task"],
                "turn": arguments["turn"],
            }
        }
    )
    request_state = {
        "previous_response_id": "",
        "request_index": 0,
    }
    previous_selected_model = ""

    for turn in range(args.turns):
        phase = hooks.phase_for_turn(args.scenario, turn)
        hooks.pause_before_turn(args, phase, turn)
        plan = hooks.plan_type(
            session_id=session_id,
            turn=turn,
            phase=phase,
            prompt=hooks.prompt_for_phase(args.scenario, session_idx, turn, phase),
            previous_response_id=str(request_state["previous_response_id"]),
        )
        send_request = _request_sender(
            args,
            plan,
            session_id,
            base_headers,
            request_state,
            hooks,
        )
        forced_tool = TOOL_NAME if phase == "tool_loop" else ""
        prompt = _agent_prompt(plan, forced_tool)
        attempts = run_agent_turn(
            conversation=conversation,
            prompt=prompt,
            tools=(tool,),
            send_request=send_request,
            executor=executor,
            expected_phase=phase,
            forced_tool_name=forced_tool,
            retry_limit=max(0, getattr(args, "tool_retry_limit", 1)),
        )
        previous_selected_model = _append_attempt_rows(
            rows,
            attempts,
            args,
            plan,
            prompt,
            previous_selected_model,
            hooks,
        )
    return rows


def _routing_tool() -> NativeTool:
    return NativeTool(
        name=TOOL_NAME,
        description="Return deterministic status for a benchmark routing task.",
        parameters=standard_tool_parameters(),
    )


def _agent_prompt(plan: Any, forced_tool: str) -> str:
    if not forced_tool:
        return str(plan.prompt)
    return (
        "Look up the current routing task status with the available tool, "
        f"using task={plan.session_id!r} and turn={plan.turn}, then continue "
        "concisely."
    )


def _request_sender(
    args: Any,
    plan: Any,
    session_id: str,
    base_headers: dict[str, str],
    request_state: dict[str, Any],
    hooks: RoutingLoopHooks,
) -> Callable[..., dict[str, Any]]:
    url = args.base_url.rstrip("/") + "/chat/completions"

    def send_request(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: Any,
        expected_phase: str,
        attempt_kind: str,
    ) -> dict[str, Any]:
        previous_id = str(request_state["previous_response_id"])
        if args.dry_run:
            result = _dry_agent_response(
                plan,
                tool_choice,
                attempt_kind,
                hooks,
            )
        else:
            headers = dict(base_headers)
            headers[args.session_header] = session_id
            body = hooks.build_request_body(
                args,
                messages,
                previous_id,
                tools=tools,
                tool_choice=tool_choice,
            )
            result = hooks.post_json(
                url,
                body,
                headers,
                args.timeout,
                stream=getattr(args, "stream", False),
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
    plan: Any,
    prompt: str,
    previous_selected_model: str,
    hooks: RoutingLoopHooks,
) -> str:
    for turn_attempt, attempt in enumerate(attempts):
        result = attempt.result
        result["_turn_attempt"] = turn_attempt
        result["_attempt_kind"] = attempt.kind
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
        attempt_plan = hooks.plan_type(
            session_id=plan.session_id,
            turn=plan.turn,
            phase=attempt.expected_phase,
            prompt=prompt,
            previous_response_id=str(result.get("_previous_response_id") or ""),
        )
        row = hooks.row_from_result(
            args,
            attempt_plan,
            result,
            previous_selected_model,
        )
        rows.append(row)
        if row["selected_model"]:
            previous_selected_model = row["selected_model"]
    return previous_selected_model


def _dry_agent_response(
    plan: Any,
    tool_choice: Any,
    attempt_kind: str,
    hooks: RoutingLoopHooks,
) -> dict[str, Any]:
    result = hooks.dry_response(plan)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "Acknowledged.",
    }
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        name = function.get("name") if isinstance(function, dict) else ""
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": (f"dry_call_{plan.session_id}_{plan.turn}_{attempt_kind}"),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(
                            {"task": plan.session_id, "turn": plan.turn}
                        ),
                    },
                }
            ],
        }
    result["json"]["id"] = f"dry_{plan.session_id}_{plan.turn}_{attempt_kind}"
    result["json"]["choices"] = [{"message": message}]
    return result
