#!/usr/bin/env python3
"""Run a checkpointed cold/warm long-context prefill matrix against one backend."""

from __future__ import annotations

import argparse
import ctypes
import json
import multiprocessing
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Any

from prefill_matrix_transport import (
    build_prompt,
    counts,
    fetch_metrics,
    metrics_delta,
    request_once,
    summarize_requests,
)

MAX_REUSE_PERCENT = 100
PR_SET_PDEATHSIG = 1


@dataclass(frozen=True)
class Cell:
    target_input_tokens: int
    reuse_percent: int
    concurrency: int
    output_tokens: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--api", choices=("openai", "ollama"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--metrics-url", default="")
    parser.add_argument("--config-label", required=True)
    parser.add_argument("--phase-label", default="coverage")
    parser.add_argument(
        "--prompt-seed-label",
        default="",
        help="shared deterministic prompt seed for matched direct/router paths",
    )
    parser.add_argument("--contexts", default="512,2048,8192,16384,32000")
    parser.add_argument("--reuse-percent", default="0,50,90")
    parser.add_argument("--concurrencies", default="1,2,4,8")
    parser.add_argument("--output-tokens", default="64,256")
    parser.add_argument("--context-window", type=int, default=0)
    parser.add_argument(
        "--context-headroom-tokens",
        "--context-overhead-tokens",
        dest="context_headroom_tokens",
        type=int,
        default=0,
        help=(
            "reserve beyond authoritative observed input plus max output; "
            "template overhead is already included in observed input"
        ),
    )
    parser.add_argument("--max-total-context-tokens", type=int, default=0)
    parser.add_argument(
        "--skip-reason",
        default="",
        help="record planned cells as skipped without sending requests",
    )
    parser.add_argument("--num-ctx", type=int, default=0)
    parser.add_argument("--trials-per-cell", type=int, default=1)
    parser.add_argument("--calibration-max-attempts", type=int, default=8)
    parser.add_argument("--calibration-output-tokens", type=int, default=1)
    parser.add_argument("--max-target-error-tokens", type=int, default=0)
    parser.add_argument("--max-target-error-percent", type=float, default=5.0)
    parser.add_argument(
        "--allow-missing-completion-usage",
        action="store_true",
        help="do not fail a request when backend output-token usage is absent",
    )
    parser.add_argument(
        "--forbid-semantic-cache-hits",
        action="store_true",
        help="fail router requests explicitly reported as semantic-cache hits",
    )
    parser.add_argument(
        "--require-semantic-cache-observation",
        action="store_true",
        help="require the router to report whether semantic response cache hit",
    )
    parser.add_argument("--warmup-requests", type=int, default=1)
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--cell-timeout", type=float, default=1800.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--sleep-between-cells", type=float, default=0.0)
    parser.add_argument("--extra-body-json", default="")
    parser.add_argument("--server-metadata-json", default="")
    parser.add_argument("--server-metadata-file", type=Path, default=None)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    args.context_values = parse_csv_ints(args.contexts)
    args.reuse_values = parse_csv_ints(args.reuse_percent)
    args.concurrency_values = parse_csv_ints(args.concurrencies)
    args.output_values = parse_csv_ints(args.output_tokens)
    args.extra_body = parse_json_object(args.extra_body_json, "--extra-body-json")
    args.server_metadata = parse_json_object(
        args.server_metadata_json, "--server-metadata-json"
    )
    if args.server_metadata_file:
        file_metadata = json.loads(args.server_metadata_file.read_text())
        if not isinstance(file_metadata, dict):
            raise ValueError("--server-metadata-file must contain a JSON object")
        args.server_metadata = {**file_metadata, **args.server_metadata}
    validate_args(args)
    return args


def parse_csv_ints(value: str) -> list[int]:
    parsed = []
    for raw in value.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        multiplier = 1
        if item.endswith("k"):
            multiplier = 1024
            item = item[:-1]
        parsed.append(int(float(item) * multiplier))
    return list(dict.fromkeys(parsed))


def parse_json_object(value: str, flag: str) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{flag} must decode to a JSON object")
    return parsed


def validate_args(args: argparse.Namespace) -> None:
    validate_dimensions(args)
    validate_calibration(args)
    validate_execution(args)


def validate_dimensions(args: argparse.Namespace) -> None:
    if not args.context_values or min(args.context_values) <= 0:
        raise ValueError("--contexts values must be positive")
    if not args.reuse_values or any(
        value < 0 or value > MAX_REUSE_PERCENT for value in args.reuse_values
    ):
        raise ValueError("--reuse-percent values must be between 0 and 100")
    if not args.concurrency_values or min(args.concurrency_values) <= 0:
        raise ValueError("--concurrencies values must be positive")
    if not args.output_values or min(args.output_values) <= 0:
        raise ValueError("--output-tokens values must be positive")
    if args.trials_per_cell <= 0:
        raise ValueError("--trials-per-cell must be positive")


def validate_calibration(args: argparse.Namespace) -> None:
    if args.calibration_max_attempts <= 0:
        raise ValueError("--calibration-max-attempts must be positive")
    if args.calibration_output_tokens <= 0:
        raise ValueError("--calibration-output-tokens must be positive")
    if args.max_target_error_tokens < 0:
        raise ValueError("--max-target-error-tokens must be non-negative")
    if not 0 <= args.max_target_error_percent <= MAX_REUSE_PERCENT:
        raise ValueError("--max-target-error-percent must be between 0 and 100")
    if args.context_headroom_tokens < 0:
        raise ValueError("--context-headroom-tokens must be non-negative")


def validate_execution(args: argparse.Namespace) -> None:
    if args.retries < 0:
        raise ValueError("--retries must be non-negative")
    if args.cell_timeout <= 0:
        raise ValueError("--cell-timeout must be positive")
    if args.heartbeat_seconds <= 0:
        raise ValueError("--heartbeat-seconds must be positive")


def planned_cells(args: argparse.Namespace) -> list[Cell]:
    return [
        Cell(context, reuse, concurrency, output)
        for context, reuse, concurrency, output in product(
            args.context_values,
            args.reuse_values,
            args.concurrency_values,
            args.output_values,
        )
    ]


def cell_id(args: argparse.Namespace, cell: Cell) -> str:
    return (
        f"{args.config_label}__{args.phase_label}"
        f"__ctx{cell.target_input_tokens}"
        f"__reuse{cell.reuse_percent}"
        f"__c{cell.concurrency}"
        f"__out{cell.output_tokens}"
    )


def prompt_id(args: argparse.Namespace, cell: Cell) -> str:
    seed = args.prompt_seed_label or args.config_label
    return (
        f"{seed}__{args.phase_label}"
        f"__ctx{cell.target_input_tokens}"
        f"__reuse{cell.reuse_percent}"
        f"__c{cell.concurrency}"
        f"__out{cell.output_tokens}"
    )


def skip_reason(args: argparse.Namespace, cell: Cell) -> str:
    if args.skip_reason:
        return args.skip_reason
    required = (
        cell.target_input_tokens + cell.output_tokens + args.context_headroom_tokens
    )
    if args.context_window and required > args.context_window:
        return (
            f"observed_input+max_output+headroom={required} exceeds "
            f"context_window={args.context_window}"
        )
    total = cell.target_input_tokens * cell.concurrency
    if args.max_total_context_tokens and total > args.max_total_context_tokens:
        return (
            f"target*concurrency={total} exceeds "
            f"max_total_context_tokens={args.max_total_context_tokens}"
        )
    return ""


def run_cell(args: argparse.Namespace, cell: Cell) -> dict[str, Any]:
    identifier = cell_id(args, cell)
    prompt_identifier = prompt_id(args, cell)
    reason = skip_reason(args, cell)
    base = {
        "schema": "agentic-prefill-cell/v2",
        "cell_id": identifier,
        "config_label": args.config_label,
        "phase_label": args.phase_label,
        "api": args.api,
        "model": args.model,
        "prompt_seed_id": prompt_identifier,
        **asdict(cell),
        "trials_per_cell": args.trials_per_cell,
        "context_budget": {
            "context_window": args.context_window,
            "authoritative_observed_input_target": cell.target_input_tokens,
            "max_output_tokens": cell.output_tokens,
            "reserved_headroom_tokens": args.context_headroom_tokens,
            "required_total_tokens": (
                cell.target_input_tokens
                + cell.output_tokens
                + args.context_headroom_tokens
            ),
            "template_overhead_accounting": (
                "included in backend-reported observed input target"
            ),
        },
    }
    if reason:
        return {
            **base,
            "status": "skipped",
            "skip_reason": reason,
            "gates": [],
            "calibration": None,
            "cold": None,
            "warm": None,
        }

    calibration = calibrate_cell(args, cell, prompt_identifier)
    if not calibration["passed"]:
        return {
            **base,
            "status": "failed",
            "skip_reason": "",
            "gates": calibration["gates"],
            "calibration": calibration,
            "cold": None,
            "warm": None,
            "cold_requests": [],
            "warm_requests": [],
            "error": "authoritative input-token calibration failed",
        }

    cold_requests, cold_elapsed, cold_metrics = run_round(
        args, cell, prompt_identifier, "cold", calibration
    )
    warm_requests, warm_elapsed, warm_metrics = run_round(
        args, cell, prompt_identifier, "warm", calibration
    )
    cold = summarize_requests(cold_requests, cold_elapsed)
    warm = summarize_requests(warm_requests, warm_elapsed)
    cold["server_metrics_delta"] = cold_metrics
    warm["server_metrics_delta"] = warm_metrics
    expected = 2 * args.trials_per_cell * cell.concurrency
    gate_passes = cold["gate_passes"] + warm["gate_passes"]
    gates = [
        gate(
            "all_measured_requests_pass",
            gate_passes == expected,
            f"passed={gate_passes} expected={expected}",
        ),
        gate(
            "cold_payloads_unique",
            cold["unique_payload_hashes"] == cold["requests"],
            (f"unique={cold['unique_payload_hashes']} requests={cold['requests']}"),
        ),
        gate(
            "cold_prompt_hashes_unique",
            cold["unique_prompt_hashes"] == cold["requests"],
            (f"unique={cold['unique_prompt_hashes']} requests={cold['requests']}"),
        ),
    ]
    status = "success" if all(item["status"] == "pass" for item in gates) else "failed"
    return {
        **base,
        "status": status,
        "skip_reason": "",
        "gates": [*calibration["gates"], *gates],
        "calibration": calibration,
        "cold": cold,
        "warm": warm,
        "cold_requests": cold_requests,
        "warm_requests": warm_requests,
    }


def calibrate_cell(
    args: argparse.Namespace,
    cell: Cell,
    identifier: str,
) -> dict[str, Any]:
    """Calibrate every measured identity without warming its measured prefix."""
    entries = []
    aggregate_gates = []
    for cohort in ("cold", "warm"):
        for trial_index in range(args.trials_per_cell):
            for request_index in range(cell.concurrency):
                entry = calibrate_request(
                    args,
                    cell,
                    identifier,
                    cohort,
                    trial_index,
                    request_index,
                )
                entries.append(entry)
                aggregate_gates.extend(
                    {
                        **item,
                        "name": (
                            f"{item['name']}[{cohort}:{trial_index}:{request_index}]"
                        ),
                    }
                    for item in entry["gates"]
                )
    return {
        "passed": bool(entries) and all(entry["passed"] for entry in entries),
        "strategy": (
            "per-request calibration uses cache-buster A; measured requests "
            "use cache-buster B with the same tokenized identity"
        ),
        "entry_count": len(entries),
        "entries": entries,
        "gates": aggregate_gates,
    }


def calibrate_request(
    args: argparse.Namespace,
    cell: Cell,
    identifier: str,
    cohort: str,
    trial_index: int,
    request_index: int,
) -> dict[str, Any]:
    filler_units = max(1, cell.target_input_tokens - 64)
    attempts: list[dict[str, Any]] = []
    seen_units: set[int] = set()
    final_gates: list[dict[str, Any]] = []
    for attempt_index in range(args.calibration_max_attempts):
        seen_units.add(filler_units)
        prompt = build_prompt(
            filler_units,
            cell.reuse_percent,
            identifier,
            cohort,
            request_index,
            trial_index=trial_index,
            target_tokens=cell.target_input_tokens,
            namespace="calibration",
        )
        result = request_with_retries(
            args,
            prompt,
            args.calibration_output_tokens,
        )
        observed = _optional_int(result.get("prompt_tokens"))
        error_tokens = (
            observed - cell.target_input_tokens if observed is not None else None
        )
        error_percent = (
            100.0 * abs(error_tokens) / cell.target_input_tokens
            if error_tokens is not None
            else None
        )
        result.update(
            {
                "calibration_attempt": attempt_index + 1,
                "cohort": cohort,
                "trial_index": trial_index,
                "request_index": request_index,
                "filler_units": filler_units,
                "target_input_tokens": cell.target_input_tokens,
                "target_error_tokens": error_tokens,
                "target_error_percent": error_percent,
            }
        )
        attempts.append(result)
        final_gates = calibration_gates(
            args,
            cell.target_input_tokens,
            result,
        )
        if all(item["status"] == "pass" for item in final_gates):
            return {
                "passed": True,
                "cohort": cohort,
                "trial_index": trial_index,
                "request_index": request_index,
                "filler_units": filler_units,
                "authoritative_observed_tokens": observed,
                "attempt_count": len(attempts),
                "attempts": attempts,
                "gates": final_gates,
            }
        if observed is None or not result.get("success"):
            break
        next_units = max(1, filler_units - int(error_tokens or 0))
        if next_units in seen_units:
            step = -1 if (error_tokens or 0) > 0 else 1
            next_units = max(1, next_units + step)
        filler_units = next_units
    return {
        "passed": False,
        "cohort": cohort,
        "trial_index": trial_index,
        "request_index": request_index,
        "filler_units": filler_units,
        "authoritative_observed_tokens": (
            attempts[-1].get("prompt_tokens") if attempts else None
        ),
        "attempt_count": len(attempts),
        "attempts": attempts,
        "gates": final_gates
        or [gate("calibration_completed", False, "no calibration attempts")],
    }


def calibration_gates(
    args: argparse.Namespace,
    target_input_tokens: int,
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    observed = _optional_int(result.get("prompt_tokens"))
    error = abs(observed - target_input_tokens) if observed is not None else None
    error_percent = 100.0 * error / target_input_tokens if error is not None else None
    return [
        gate(
            "calibration_http_success",
            bool(result.get("success")),
            f"http_status={result.get('status')}",
        ),
        gate(
            "calibration_authoritative_prompt_usage_present",
            observed is not None,
            (
                f"source={result.get('prompt_token_source') or '<missing>'} "
                f"observed={observed}"
            ),
        ),
        gate(
            "calibration_target_error_tokens",
            error is not None and error <= args.max_target_error_tokens,
            (f"absolute_error={error} limit={args.max_target_error_tokens}"),
        ),
        gate(
            "calibration_target_error_percent",
            error_percent is not None
            and error_percent <= args.max_target_error_percent,
            (
                f"error_percent={_rounded(error_percent)} "
                f"limit={args.max_target_error_percent}"
            ),
        ),
    ]


def evaluate_request_gates(
    args: argparse.Namespace,
    target_input_tokens: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    observed = _optional_int(result.get("prompt_tokens"))
    error_signed = observed - target_input_tokens if observed is not None else None
    error = abs(error_signed) if error_signed is not None else None
    error_percent = 100.0 * error / target_input_tokens if error is not None else None
    checks = [
        gate(
            "http_success",
            bool(result.get("success")),
            f"http_status={result.get('status')}",
        ),
        gate(
            "authoritative_prompt_usage_present",
            observed is not None,
            (
                f"source={result.get('prompt_token_source') or '<missing>'} "
                f"observed={observed}"
            ),
        ),
        gate(
            "completion_usage_present",
            args.allow_missing_completion_usage
            or result.get("completion_tokens") is not None,
            (
                "allowed_missing=true"
                if args.allow_missing_completion_usage
                else (f"source={result.get('completion_token_source') or '<missing>'}")
            ),
        ),
        gate(
            "target_error_tokens",
            error is not None and error <= args.max_target_error_tokens,
            (f"absolute_error={error} limit={args.max_target_error_tokens}"),
        ),
        gate(
            "target_error_percent",
            error_percent is not None
            and error_percent <= args.max_target_error_percent,
            (
                f"error_percent={_rounded(error_percent)} "
                f"limit={args.max_target_error_percent}"
            ),
        ),
        gate(
            "response_marker",
            bool(result.get("marker_correct")),
            f"marker_correct={bool(result.get('marker_correct'))}",
        ),
        gate(
            "semantic_response_cache_observed",
            not args.require_semantic_cache_observation
            or result.get("semantic_cache_hit") is not None,
            (
                f"required={args.require_semantic_cache_observation} "
                f"observed={result.get('semantic_cache_hit')}"
            ),
        ),
        gate(
            "semantic_response_cache_miss",
            not (
                args.forbid_semantic_cache_hits
                and result.get("semantic_cache_hit") is True
            ),
            (
                f"forbidden={args.forbid_semantic_cache_hits} "
                f"observed={result.get('semantic_cache_hit')}"
            ),
        ),
    ]
    result.update(
        {
            "target_input_tokens": target_input_tokens,
            "target_error_tokens": error_signed,
            "target_error_percent": error_percent,
            "gates": checks,
            "gate_passed": all(item["status"] == "pass" for item in checks),
        }
    )
    return result


def gate(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "detail": detail,
    }


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def run_cell_bounded(args: argparse.Namespace, cell: Cell) -> dict[str, Any]:
    """Run one cell in a killable process with periodic progress heartbeats."""
    args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    identifier = cell_id(args, cell)
    temp_path = args.checkpoint.parent / (
        f".{args.checkpoint.name}.{os.getpid()}.{cell.target_input_tokens}."
        f"{cell.reuse_percent}.{cell.concurrency}.{cell.output_tokens}.tmp"
    )
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_cell_worker,
        args=(args, cell, temp_path),
        daemon=False,
    )
    started = time.monotonic()
    process.start()
    while process.is_alive():
        process.join(timeout=args.heartbeat_seconds)
        elapsed = time.monotonic() - started
        if process.is_alive():
            print(
                f"  heartbeat cell={identifier} elapsed={elapsed:.1f}s",
                flush=True,
            )
        if elapsed >= args.cell_timeout and process.is_alive():
            process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            temp_path.unlink(missing_ok=True)
            return timeout_record(args, cell, elapsed)
    if process.exitcode != 0 or not temp_path.exists():
        temp_path.unlink(missing_ok=True)
        return worker_failure_record(args, cell, process.exitcode)
    record = json.loads(temp_path.read_text())
    temp_path.unlink(missing_ok=True)
    return record


def _cell_worker(
    args: argparse.Namespace,
    cell: Cell,
    temp_path: Path,
) -> None:
    _set_parent_death_signal()
    record = run_cell(args, cell)
    temp_path.write_text(json.dumps(record, sort_keys=True) + "\n")


def _set_parent_death_signal() -> None:
    """Ask Linux to terminate a cell worker if its matrix parent disappears."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except (AttributeError, OSError):
        return


def timeout_record(
    args: argparse.Namespace, cell: Cell, elapsed: float
) -> dict[str, Any]:
    return {
        "schema": "agentic-prefill-cell/v1",
        "cell_id": cell_id(args, cell),
        "config_label": args.config_label,
        "phase_label": args.phase_label,
        "api": args.api,
        "model": args.model,
        **asdict(cell),
        "status": "failed",
        "skip_reason": "",
        "cold": None,
        "warm": None,
        "error": f"cell timeout after {elapsed:.1f}s",
    }


def worker_failure_record(
    args: argparse.Namespace, cell: Cell, exitcode: int | None
) -> dict[str, Any]:
    return {
        "schema": "agentic-prefill-cell/v1",
        "cell_id": cell_id(args, cell),
        "config_label": args.config_label,
        "phase_label": args.phase_label,
        "api": args.api,
        "model": args.model,
        **asdict(cell),
        "status": "failed",
        "skip_reason": "",
        "cold": None,
        "warm": None,
        "error": f"cell worker exited {exitcode} without evidence",
    }


def run_round(
    args: argparse.Namespace,
    cell: Cell,
    identifier: str,
    cohort: str,
    calibration: dict[str, Any],
) -> tuple[list[dict[str, Any]], float, dict[str, Any]]:
    metrics_before = fetch_metrics(args.metrics_url, args.request_timeout)
    started = time.perf_counter()
    requests: list[dict[str, Any]] = []
    workers = max(1, cell.concurrency)
    for trial_index in range(args.trials_per_cell):
        wave: list[dict[str, Any] | None] = [None] * cell.concurrency
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    run_measured_request,
                    args,
                    cell,
                    identifier,
                    cohort,
                    calibrated_filler_units(
                        calibration,
                        cohort,
                        trial_index,
                        request_index,
                    ),
                    trial_index,
                    request_index,
                ): request_index
                for request_index in range(cell.concurrency)
            }
            for future in as_completed(futures):
                wave[futures[future]] = future.result()
        requests.extend(request for request in wave if request is not None)
    elapsed = time.perf_counter() - started
    metrics_after = fetch_metrics(args.metrics_url, args.request_timeout)
    return requests, elapsed, metrics_delta(metrics_before, metrics_after)


def calibrated_filler_units(
    calibration: dict[str, Any],
    cohort: str,
    trial_index: int,
    request_index: int,
) -> int:
    for entry in calibration.get("entries") or []:
        if (
            entry.get("cohort") == cohort
            and entry.get("trial_index") == trial_index
            and entry.get("request_index") == request_index
        ):
            return int(entry["filler_units"])
    raise KeyError(
        "missing calibration for "
        f"cohort={cohort} trial={trial_index} request={request_index}"
    )


def run_measured_request(
    args: argparse.Namespace,
    cell: Cell,
    identifier: str,
    cohort: str,
    filler_units: int,
    trial_index: int,
    request_index: int,
) -> dict[str, Any]:
    prompt = build_prompt(
        filler_units,
        cell.reuse_percent,
        identifier,
        cohort,
        request_index,
        trial_index=trial_index,
        target_tokens=cell.target_input_tokens,
        namespace="measure",
    )
    result = request_with_retries(args, prompt, cell.output_tokens)
    result.update(
        {
            "cohort": cohort,
            "trial_index": trial_index,
            "request_index": request_index,
            "filler_units": filler_units,
            "requested_reuse_percent": cell.reuse_percent,
        }
    )
    return evaluate_request_gates(args, cell.target_input_tokens, result)


def request_with_retries(
    args: argparse.Namespace,
    prompt: str,
    output_tokens: int,
) -> dict[str, Any]:
    last: dict[str, Any] = {}
    history: list[dict[str, Any]] = []
    for attempt in range(args.retries + 1):
        result = request_once(
            api=args.api,
            base_url=args.backend_url,
            model=args.model,
            prompt=prompt,
            max_tokens=output_tokens,
            timeout=args.request_timeout,
            api_key=args.api_key,
            num_ctx=args.num_ctx,
            extra_body=args.extra_body,
        )
        result["attempt"] = attempt + 1
        result["requested_prompt_chars"] = len(prompt)
        history.append(
            {
                "attempt": attempt + 1,
                "status": result.get("status"),
                "success": result.get("success"),
                "error": result.get("error"),
                "prompt_tokens": result.get("prompt_tokens"),
                "payload_sha256": result.get("payload_sha256"),
            }
        )
        result["attempt_history"] = list(history)
        last = result
        if result.get("success"):
            return result
        if attempt < args.retries and args.retry_delay > 0:
            time.sleep(args.retry_delay)
    return last


def warm_up(args: argparse.Namespace) -> list[dict[str, Any]]:
    results = []
    for index in range(max(0, args.warmup_requests)):
        prompt = build_prompt(
            448,
            0,
            f"{args.config_label}-warmup",
            "cold",
            index,
            target_tokens=512,
            namespace="warmup",
        )
        results.append(request_with_retries(args, prompt, 8))
    return results


def load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return records
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        identifier = record.get("cell_id")
        if isinstance(identifier, str):
            records[identifier] = record
    return records


def append_checkpoint(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def should_run(
    args: argparse.Namespace,
    identifier: str,
    existing: dict[str, dict[str, Any]],
) -> bool:
    record = existing.get(identifier)
    if not record:
        return True
    return args.rerun_failed and record.get("status") in {"failed", "partial"}


def build_summary(
    args: argparse.Namespace,
    cells: list[Cell],
    records: dict[str, dict[str, Any]],
    warmup: list[dict[str, Any]],
) -> dict[str, Any]:
    planned_ids = [cell_id(args, cell) for cell in cells]
    selected = [
        records[identifier] for identifier in planned_ids if identifier in records
    ]
    status_counts = counts(
        str(record.get("status") or "unknown") for record in selected
    )
    matrix_gates = [
        gate(
            "all_cells_recorded",
            len(selected) == len(cells),
            f"recorded={len(selected)} planned={len(cells)}",
        ),
        gate(
            "all_cells_passed",
            bool(selected)
            and all(record.get("status") == "success" for record in selected),
            f"status_counts={status_counts}",
        ),
    ]
    return {
        "schema": "agentic-prefill-matrix/v2",
        "config_label": args.config_label,
        "phase_label": args.phase_label,
        "api": args.api,
        "backend_url": args.backend_url,
        "metrics_url": args.metrics_url,
        "model": args.model,
        "prompt_seed_label": args.prompt_seed_label or args.config_label,
        "server_metadata": args.server_metadata,
        "dimensions": {
            "contexts": args.context_values,
            "reuse_percent": args.reuse_values,
            "concurrencies": args.concurrency_values,
            "output_tokens": args.output_values,
            "trials_per_cell": args.trials_per_cell,
        },
        "limits": {
            "context_window": args.context_window,
            "context_headroom_tokens": args.context_headroom_tokens,
            "context_accounting": (
                "authoritative observed input already includes chat/template "
                "overhead; budget=input+max_output+reserved_headroom"
            ),
            "max_total_context_tokens": args.max_total_context_tokens,
            "request_timeout": args.request_timeout,
            "cell_timeout": args.cell_timeout,
            "heartbeat_seconds": args.heartbeat_seconds,
            "retries": args.retries,
            "calibration_max_attempts": args.calibration_max_attempts,
            "max_target_error_tokens": args.max_target_error_tokens,
            "max_target_error_percent": args.max_target_error_percent,
        },
        "cache_semantics": {
            "reuse_percent_means": "backend prefix/KV reuse cohort",
            "semantic_response_cache": (
                "forbidden when --forbid-semantic-cache-hits is set; "
                "reported separately and never counted as prefix/KV reuse"
            ),
            "forbid_semantic_cache_hits": args.forbid_semantic_cache_hits,
            "require_semantic_cache_observation": (
                args.require_semantic_cache_observation
            ),
        },
        "planned_cells": len(cells),
        "recorded_cells": len(selected),
        "status_counts": status_counts,
        "gates": matrix_gates,
        "passed": all(item["status"] == "pass" for item in matrix_gates),
        "warmup": warmup,
        "cells": selected,
    }


def write_metadata(
    args: argparse.Namespace, cells: list[Cell], summary_path: Path
) -> None:
    manifest = {
        "schema": "agentic-prefill-manifest/v2",
        "argv": _redacted_argv(sys.argv),
        "checkpoint": str(args.checkpoint),
        "summary": str(summary_path),
        "planned_cells": [asdict(cell) for cell in cells],
        "server_metadata": args.server_metadata,
    }
    manifest_path = args.checkpoint.with_suffix(
        args.checkpoint.suffix + ".manifest.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    cells = planned_cells(args)
    summary_path = args.summary or args.checkpoint.with_suffix(".summary.json")
    write_metadata(args, cells, summary_path)
    existing = load_checkpoint(args.checkpoint)
    if args.plan_only:
        plan = [
            {
                "cell_id": cell_id(args, cell),
                **asdict(cell),
                "skip_reason": skip_reason(args, cell),
            }
            for cell in cells
        ]
        print(json.dumps(plan, indent=2))
        return 0

    warmup = warm_up(args)
    for index, cell in enumerate(cells, 1):
        identifier = cell_id(args, cell)
        if not should_run(args, identifier, existing):
            print(f"[{index}/{len(cells)}] resume-skip {identifier}")
            continue
        print(f"[{index}/{len(cells)}] run {identifier}", flush=True)
        record = run_cell_bounded(args, cell)
        append_checkpoint(args.checkpoint, record)
        existing[identifier] = record
        print(
            f"  -> {record['status']} "
            f"cold={_success_rate(record.get('cold'))} "
            f"warm={_success_rate(record.get('warm'))}",
            flush=True,
        )
        if args.sleep_between_cells > 0:
            time.sleep(args.sleep_between_cells)

    summary = build_summary(args, cells, existing, warmup)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                key: summary[key]
                for key in (
                    "config_label",
                    "phase_label",
                    "planned_cells",
                    "recorded_cells",
                    "status_counts",
                )
            },
            indent=2,
        )
    )
    return 0 if summary["passed"] else 1


def _success_rate(value: Any) -> Any:
    return value.get("success_rate") if isinstance(value, dict) else None


def _redacted_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for item in argv:
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        if item == "--api-key":
            redacted.append(item)
            hide_next = True
            continue
        if item.startswith("--api-key="):
            redacted.append("--api-key=<redacted>")
            continue
        redacted.append(item)
    return redacted


if __name__ == "__main__":
    raise SystemExit(main())
