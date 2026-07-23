"""Deterministic native-tool agent loop used by live benchmarks."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NativeTool:
    name: str
    description: str
    parameters: dict[str, Any]

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": deepcopy(self.parameters),
            },
        }


@dataclass(frozen=True)
class ToolExecution:
    call_id: str
    name: str
    arguments: dict[str, Any]
    content: str
    is_error: bool
    attempt: int

    def as_message(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "content": self.content,
        }


@dataclass(frozen=True)
class LoopAttempt:
    result: dict[str, Any]
    expected_phase: str
    kind: str
    final_response: bool
    tool_executions: tuple[ToolExecution, ...] = ()


class AgentConversation:
    """Mutable message history containing actual model and tool messages."""

    def __init__(self, system_prompt: str) -> None:
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    @classmethod
    def from_messages(cls, messages: list[dict[str, Any]]) -> AgentConversation:
        if not messages:
            raise ValueError("agent conversation history must not be empty")
        conversation = cls.__new__(cls)
        conversation.messages = deepcopy(messages)
        return conversation

    def append_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def append_retry_request(self, tool_name: str) -> None:
        self.messages.append(
            {
                "role": "user",
                "content": (
                    f"Retry the failed step by calling {tool_name} again with "
                    "correct arguments."
                ),
            }
        )

    def append_assistant_result(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        message = assistant_message(result.get("json"))
        self.messages.append(message)
        return normalize_tool_calls(message.get("tool_calls"))

    def append_tool_executions(self, executions: tuple[ToolExecution, ...]) -> None:
        self.messages.extend(execution.as_message() for execution in executions)

    def snapshot(self) -> list[dict[str, Any]]:
        return deepcopy(self.messages)


class DeterministicToolExecutor:
    """Execute local handlers with an optional deterministic fail-first schedule."""

    def __init__(
        self,
        handlers: Mapping[str, Callable[[dict[str, Any]], Any]],
        fail_first: Mapping[str, int] | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._fail_first = dict(fail_first or {})
        self._attempts: dict[tuple[str, str], int] = {}

    def execute_many(
        self, tool_calls: list[dict[str, Any]]
    ) -> tuple[ToolExecution, ...]:
        return tuple(self.execute(call) for call in tool_calls)

    def execute(self, tool_call: dict[str, Any]) -> ToolExecution:
        function = tool_call.get("function")
        if not isinstance(function, dict):
            function = {}
        name = str(function.get("name") or "")
        call_id = str(tool_call.get("id") or f"call_{name or 'unknown'}")
        arguments, parse_error = parse_tool_arguments(function.get("arguments"))
        attempt_key = (name, json.dumps(arguments, sort_keys=True))
        attempt = self._attempts.get(attempt_key, 0) + 1
        self._attempts[attempt_key] = attempt

        if parse_error:
            return self._error_execution(call_id, name, arguments, parse_error, attempt)
        if name not in self._handlers:
            return self._error_execution(
                call_id, name, arguments, f"unknown tool: {name}", attempt
            )
        if attempt <= int(self._fail_first.get(name, 0)):
            return self._error_execution(
                call_id,
                name,
                arguments,
                f"injected retryable failure for {name}",
                attempt,
            )
        try:
            value = self._handlers[name](deepcopy(arguments))
        except Exception as exc:  # pragma: no cover - handlers are caller supplied
            return self._error_execution(
                call_id, name, arguments, f"{type(exc).__name__}: {exc}", attempt
            )
        return ToolExecution(
            call_id=call_id,
            name=name,
            arguments=arguments,
            content=json.dumps(
                {"ok": True, "result": value},
                sort_keys=True,
                separators=(",", ":"),
            ),
            is_error=False,
            attempt=attempt,
        )

    @staticmethod
    def _error_execution(
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        message: str,
        attempt: int,
    ) -> ToolExecution:
        return ToolExecution(
            call_id=call_id,
            name=name,
            arguments=arguments,
            content=json.dumps(
                {
                    "ok": False,
                    "error": message,
                    "retryable": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            is_error=True,
            attempt=attempt,
        )


SendRequest = Callable[
    [list[dict[str, Any]], list[dict[str, Any]], Any, str, str],
    dict[str, Any],
]


def run_agent_turn(
    conversation: AgentConversation,
    prompt: str,
    tools: tuple[NativeTool, ...],
    send_request: SendRequest,
    executor: DeterministicToolExecutor,
    expected_phase: str,
    forced_tool_name: str = "",
    retry_limit: int = 1,
    initial_tool_choice: Any | None = None,
) -> list[LoopAttempt]:
    """Run one user turn through tool calls, local execution, and final response."""
    conversation.append_user(prompt)
    openai_tools = [tool.as_openai_tool() for tool in tools]
    tool_choice: Any = initial_tool_choice
    if tool_choice is None:
        tool_choice = (
            named_tool_choice(forced_tool_name) if forced_tool_name else "none"
        )
    result = send_request(
        conversation.snapshot(),
        openai_tools,
        tool_choice,
        expected_phase,
        "model",
    )
    tool_calls = conversation.append_assistant_result(result)
    if not tool_calls:
        if not forced_tool_name:
            return [
                LoopAttempt(
                    result=result,
                    expected_phase=expected_phase,
                    kind="model",
                    final_response=True,
                )
            ]
        return _retry_missing_tool_call(
            conversation,
            tools,
            send_request,
            executor,
            forced_tool_name,
            result,
            expected_phase,
            retry_limit,
        )

    attempts: list[LoopAttempt] = []
    retries_remaining = max(0, retry_limit)
    request_kind = "tool_request"
    while tool_calls:
        executions = executor.execute_many(tool_calls)
        conversation.append_tool_executions(executions)
        attempts.append(
            LoopAttempt(
                result=result,
                expected_phase=expected_phase,
                kind=request_kind,
                final_response=False,
                tool_executions=executions,
            )
        )
        failed = any(execution.is_error for execution in executions)
        if failed and retries_remaining > 0:
            retries_remaining -= 1
            conversation.append_retry_request(forced_tool_name or executions[0].name)
            result = send_request(
                conversation.snapshot(),
                openai_tools,
                named_tool_choice(forced_tool_name or executions[0].name),
                "tool_loop",
                "tool_retry",
            )
            tool_calls = conversation.append_assistant_result(result)
            expected_phase = "tool_loop"
            request_kind = "tool_retry"
            continue
        break

    final_result = send_request(
        conversation.snapshot(),
        openai_tools,
        "none",
        "tool_loop",
        "tool_followup",
    )
    conversation.append_assistant_result(final_result)
    attempts.append(
        LoopAttempt(
            result=final_result,
            expected_phase="tool_loop",
            kind="tool_followup",
            final_response=True,
        )
    )
    return attempts


def _retry_missing_tool_call(
    conversation: AgentConversation,
    tools: tuple[NativeTool, ...],
    send_request: SendRequest,
    executor: DeterministicToolExecutor,
    forced_tool_name: str,
    first_result: dict[str, Any],
    expected_phase: str,
    retry_limit: int,
) -> list[LoopAttempt]:
    attempts = [
        LoopAttempt(
            result=first_result,
            expected_phase=expected_phase,
            kind="missing_tool_call",
            final_response=False,
        )
    ]
    if retry_limit <= 0:
        return attempts
    conversation.append_retry_request(forced_tool_name)
    openai_tools = [tool.as_openai_tool() for tool in tools]
    retry_result = send_request(
        conversation.snapshot(),
        openai_tools,
        named_tool_choice(forced_tool_name),
        "tool_loop",
        "tool_retry",
    )
    tool_calls = conversation.append_assistant_result(retry_result)
    if tool_calls:
        executions = executor.execute_many(tool_calls)
        conversation.append_tool_executions(executions)
        attempts.append(
            LoopAttempt(
                result=retry_result,
                expected_phase="tool_loop",
                kind="tool_retry",
                final_response=False,
                tool_executions=executions,
            )
        )
        final_result = send_request(
            conversation.snapshot(),
            openai_tools,
            "none",
            "tool_loop",
            "tool_followup",
        )
        conversation.append_assistant_result(final_result)
        attempts.append(
            LoopAttempt(
                result=final_result,
                expected_phase="tool_loop",
                kind="tool_followup",
                final_response=True,
            )
        )
    else:
        attempts.append(
            LoopAttempt(
                result=retry_result,
                expected_phase="tool_loop",
                kind="missing_tool_call_retry",
                final_response=True,
            )
        )
    return attempts


def named_tool_choice(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name}}


def assistant_message(response_json: Any) -> dict[str, Any]:
    if isinstance(response_json, dict):
        choices = response_json.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                normalized = deepcopy(message)
                normalized.setdefault("role", "assistant")
                normalized.setdefault("content", None)
                if "tool_calls" in normalized:
                    normalized["tool_calls"] = normalize_tool_calls(
                        normalized["tool_calls"]
                    )
                return normalized
    return {"role": "assistant", "content": ""}


def normalize_tool_calls(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, call in enumerate(value):
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, (dict, list)):
            arguments = json.dumps(arguments, sort_keys=True)
        normalized.append(
            {
                "id": str(call.get("id") or f"call_{index}"),
                "type": str(call.get("type") or "function"),
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": str(arguments),
                },
            }
        )
    return normalized


def parse_tool_arguments(value: Any) -> tuple[dict[str, Any], str]:
    if isinstance(value, dict):
        return deepcopy(value), ""
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON arguments: {exc.msg}"
    if not isinstance(parsed, dict):
        return {}, "tool arguments must be a JSON object"
    return parsed, ""


def standard_tool_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "turn": {"type": "integer", "minimum": 0},
        },
        "required": ["task", "turn"],
        "additionalProperties": False,
    }
