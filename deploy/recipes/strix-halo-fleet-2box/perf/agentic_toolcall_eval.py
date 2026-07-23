"""Scoring and aggregation for the agentic tool-call probe."""

from __future__ import annotations

import itertools
import json
import re
import statistics
from typing import Any

NUMBER_TOLERANCE = 1e-6


def extract_tool_call(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Backward-compatible single-call view over ``extract_tool_calls``."""
    calls, span = extract_tool_calls(text)
    return ((calls or [None])[0] if calls is not None else None), span


def extract_tool_calls(
    text: str,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Recover the last valid single, parallel, or no-tool JSON verdict."""
    if not text:
        return None, None
    cleaned = text.replace("```json", "```")
    decoder = json.JSONDecoder()
    best_calls = None
    best_span = None
    best_end = -1
    for index, char in enumerate(cleaned):
        if char not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(cleaned[index:])
        except ValueError:
            continue
        calls = normalize_tool_calls(value)
        absolute_end = index + end
        if calls is not None and absolute_end > best_end:
            best_calls = calls
            best_span = cleaned[index : index + end]
            best_end = absolute_end
    return best_calls, best_span


def normalize_tool_calls(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, dict) and isinstance(value.get("tool_calls"), list):
        value = value["tool_calls"]
    if isinstance(value, dict):
        call = normalize_tool_call(value)
        if call is None:
            return None
        return [] if call["name"].lower() in {"none", "no_tool"} else [call]
    if not isinstance(value, list):
        return None
    calls = []
    for item in value:
        call = normalize_tool_call(item)
        if call is None:
            return None
        if call["name"].lower() not in {"none", "no_tool"}:
            calls.append(call)
    return calls


def normalize_tool_call(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    function = value.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function.get("arguments", {})
    else:
        name = value.get("name")
        arguments = value.get("arguments", value.get("parameters", {}))
    if not isinstance(name, str):
        return None
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(arguments, dict):
        return None
    return {"name": name, "arguments": arguments}


def check_arg(check: str, expected: Any, actual: Any) -> bool:
    if actual is None and check != "absent":
        return False
    if check == "equals":
        return _as_text(actual).strip().lower() == str(expected).strip().lower()
    if check == "contains":
        return str(expected).strip().lower() in _as_text(actual).lower()
    if check == "contains_all":
        haystack = _as_text(actual).lower()
        return all(str(value).strip().lower() in haystack for value in expected)
    if check == "equals_number":
        return _numbers_equal(expected, actual)
    return False


def score_task(
    task: dict[str, Any], obj: Any
) -> tuple[bool, bool, bool, dict[str, Any]]:
    calls = normalize_tool_calls(obj)
    return score_task_calls(task, calls, calls is not None)


def score_task_calls(
    task: dict[str, Any],
    calls: list[dict[str, Any]] | None,
    structured_valid: bool = True,
) -> tuple[bool, bool, bool, dict[str, Any]]:
    if not structured_valid or calls is None:
        return False, False, False, {}
    expected_calls = task_expected_calls(task)
    expected_names = sorted(
        str(call["name"]).strip().lower() for call in expected_calls
    )
    predicted_names = sorted(str(call["name"]).strip().lower() for call in calls)
    name_ok = expected_names == predicted_names
    if not expected_calls:
        return True, name_ok, name_ok, {}
    if len(calls) != len(expected_calls):
        return True, name_ok, False, {}

    best_detail = {}
    for permutation in itertools.permutations(calls):
        all_ok = True
        permutation_detail = {}
        for index, (expected, predicted) in enumerate(
            zip(expected_calls, permutation, strict=True)
        ):
            same_name = (
                str(expected["name"]).strip().lower()
                == str(predicted["name"]).strip().lower()
            )
            args_ok, detail = score_call_args(expected, predicted)
            permutation_detail[f"call_{index}"] = detail
            all_ok = all_ok and same_name and args_ok
        best_detail = permutation_detail
        if all_ok:
            return True, name_ok, True, permutation_detail
    return True, name_ok, False, best_detail


def task_expected_calls(task: dict[str, Any]) -> list[dict[str, Any]]:
    expected = task["expect"]
    if isinstance(expected.get("calls"), list):
        return expected["calls"]
    name = str(expected.get("name") or "")
    if name.lower() in {"", "none", "no_tool"}:
        return []
    return [{"name": name, "args": expected.get("args", {})}]


def score_call_args(
    expected_call: dict[str, Any], predicted_call: dict[str, Any]
) -> tuple[bool, dict[str, bool]]:
    args = predicted_call.get("arguments")
    if not isinstance(args, dict):
        args = {}
    detail = {}
    all_ok = True
    for key, spec in expected_call.get("args", {}).items():
        actual = lookup_arg(args, key)
        ok = check_arg(spec["check"], spec["value"], actual)
        detail[key] = ok
        all_ok = all_ok and ok
    return all_ok, detail


def lookup_arg(arguments: dict[str, Any], path: str) -> Any:
    current: Any = arguments
    for part in str(path).split("."):
        if not isinstance(current, dict):
            return None
        if part in current:
            current = current[part]
            continue
        matched = next(
            (key for key in current if str(key).lower() == part.lower()),
            None,
        )
        if matched is None:
            return None
        current = current[matched]
    return current


def build_trial_record(
    task: dict[str, Any],
    metrics: dict[str, Any],
    cache_state: str,
    warm_index: int,
    tool_mode: str,
    error: str | None,
) -> dict[str, Any]:
    text = str(metrics.get("text") or "")
    if tool_mode == "native" and not error:
        calls = normalize_tool_calls(metrics.get("tool_calls"))
        span = json.dumps(metrics.get("tool_calls") or [], sort_keys=True)
        structured_valid = calls is not None
    else:
        calls, span = extract_tool_calls(text)
        structured_valid = calls is not None
    json_valid, name_ok, args_ok, arg_detail = score_task_calls(
        task, calls, structured_valid and not error
    )
    return {
        "id": task["id"],
        "category": task.get("category", "single_call"),
        "cache_state": cache_state,
        "warm_index": warm_index,
        "json_valid": json_valid,
        "name_correct": name_ok,
        "args_correct": args_ok,
        "step_correct": json_valid and name_ok and args_ok,
        "arg_detail": arg_detail,
        "predicted_name": calls[0]["name"] if calls else None,
        "predicted_names": [call["name"] for call in calls or []],
        "wall_s": _rounded(metrics.get("wall_s"), 3),
        "ttft_ms": _rounded(metrics.get("ttft_ms"), 3),
        "decode_tps": _rounded(metrics.get("decode_tps"), 3),
        "prefill_tps": _rounded(metrics.get("prefill_tps"), 3),
        "prompt_tokens": metrics.get("prompt_tokens"),
        "completion_tokens": metrics.get("completion_tokens"),
        "prompt_eval_duration_ms": _rounded(metrics.get("prompt_eval_duration_ms"), 3),
        "eval_duration_ms": _rounded(metrics.get("eval_duration_ms"), 3),
        "load_duration_ms": _rounded(metrics.get("load_duration_ms"), 3),
        "cached_tokens": int(metrics.get("cached_tokens") or 0),
        "cached_token_field_present": bool(metrics.get("cached_token_field_present")),
        "cached_token_source": metrics.get("cached_token_source") or "",
        "raw_span": span[:800] if span else (text[:800] if text else None),
        "error": error,
    }


def aggregate_trials(trials: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(trials)
    quality = quality_summary(trials)
    walls = _numeric_values(trials, "wall_s")
    decodes = _numeric_values(trials, "decode_tps")
    prefills = _numeric_values(trials, "prefill_tps")
    ttfts = _numeric_values(trials, "ttft_ms")
    prompt_durations = _numeric_values(trials, "prompt_eval_duration_ms")
    prompt_tokens = sum(int(trial.get("prompt_tokens") or 0) for trial in trials)
    cached_tokens = sum(int(trial.get("cached_tokens") or 0) for trial in trials)
    cache_field_count = sum(
        bool(trial.get("cached_token_field_present")) for trial in trials
    )
    return {
        "n": count,
        **quality,
        "latency": {
            "wall_s_mean": _mean_or_none(walls, 3),
            "wall_s_median": _median_or_none(walls, 3),
            "decode_tps_mean": _mean_or_none(decodes, 2),
            "prefill_tps_mean": _mean_or_none(prefills, 2),
            "ttft_ms_mean": _mean_or_none(ttfts, 1),
            "ttft_ms_p95": _percentile_or_none(ttfts, 95, 1),
            "prompt_eval_duration_ms_mean": _mean_or_none(prompt_durations, 3),
            "prompt_eval_duration_ms_p95": _percentile_or_none(prompt_durations, 95, 3),
        },
        "prompt_tokens": prompt_tokens,
        "completion_tokens": sum(
            int(trial.get("completion_tokens") or 0) for trial in trials
        ),
        "cached_tokens": cached_tokens,
        "cached_prompt_ratio": (
            round(cached_tokens / prompt_tokens, 6) if prompt_tokens else None
        ),
        "cached_token_field_present": cache_field_count,
        "cached_token_field_rate": (
            round(cache_field_count / count, 4) if count else None
        ),
        "cached_token_source_counts": _counts(
            trial["cached_token_source"]
            for trial in trials
            if trial.get("cached_token_source")
        ),
        "cache_states": {
            state: _cache_state_summary(
                [trial for trial in trials if trial["cache_state"] == state]
            )
            for state in ("cold", "warm")
        },
    }


def quality_summary(trials: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(trials)
    json_valid = sum(bool(trial["json_valid"]) for trial in trials)
    name_correct = sum(bool(trial["name_correct"]) for trial in trials)
    args_correct = sum(bool(trial["args_correct"]) for trial in trials)
    step_correct = sum(bool(trial["step_correct"]) for trial in trials)
    return {
        "json_valid": json_valid,
        "name_correct": name_correct,
        "args_correct": args_correct,
        "step_correct": step_correct,
        "json_valid_rate": round(json_valid / count, 4) if count else None,
        "name_correct_rate": round(name_correct / count, 4) if count else None,
        "args_correct_rate": round(args_correct / count, 4) if count else None,
        "step_correct_rate": round(step_correct / count, 4) if count else None,
        "failure_rate": (round(1 - json_valid / count, 4) if count else None),
    }


def _cache_state_summary(trials: list[dict[str, Any]]) -> dict[str, Any]:
    quality = quality_summary(trials)
    prompt_tokens = sum(int(trial.get("prompt_tokens") or 0) for trial in trials)
    cached_tokens = sum(int(trial.get("cached_tokens") or 0) for trial in trials)
    return {
        "requests": len(trials),
        **quality,
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "cached_prompt_ratio": (
            round(cached_tokens / prompt_tokens, 6) if prompt_tokens else None
        ),
        "ttft_ms_mean": _mean_or_none(_numeric_values(trials, "ttft_ms"), 3),
        "prefill_tps_mean": _mean_or_none(_numeric_values(trials, "prefill_tps"), 3),
    }


def _as_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _numbers_equal(expected: Any, actual: Any) -> bool:
    try:
        return abs(float(actual) - float(expected)) < NUMBER_TOLERANCE
    except (TypeError, ValueError):
        match = re.search(r"-?\d+(?:\.\d+)?", _as_text(actual))
        return (
            bool(match)
            and abs(float(match.group()) - float(expected)) < NUMBER_TOLERANCE
        )


def _numeric_values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [
        float(row[key])
        for row in rows
        if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)
    ]


def _mean_or_none(values: list[float], digits: int) -> float | None:
    return round(statistics.fmean(values), digits) if values else None


def _median_or_none(values: list[float], digits: int) -> float | None:
    return round(statistics.median(values), digits) if values else None


def _percentile_or_none(
    values: list[float], percentile: float, digits: int
) -> float | None:
    value = _percentile(values, percentile)
    return round(value, digits) if value is not None else None


def _percentile(values: list[float], percentile: float) -> float | None:
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    index = max(
        0,
        min(
            len(ordered) - 1,
            round((percentile / 100.0) * (len(ordered) - 1)),
        ),
    )
    return ordered[index]


def _rounded(value: Any, digits: int) -> float | None:
    return round(float(value), digits) if isinstance(value, (int, float)) else None


def _counts(values: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[str(value)] = result.get(str(value), 0) + 1
    return dict(sorted(result.items()))
