#!/usr/bin/env python3
"""Run the reproducible Strix Halo 64K customer prefill capacity profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTEXT_WINDOW = 65_536
OUTPUT_TOKENS = 256
HEADROOM_TOKENS = 128
MINIMAL_SPINE_TRIALS = 1
EXTENDED_SPINE_TRIALS = 3
RESOURCE_INTERVAL_SECONDS = 1.0
TOKENS_32K = 32_768
FULL_GPU_PERCENT = 100.0


@dataclass(frozen=True)
class Target:
    label: str
    nominal_input_tokens: int
    observed_input_target: int
    output_reservation_tokens: int
    operational_headroom_tokens: int


@dataclass(frozen=True)
class Phase:
    name: str
    target_labels: tuple[str, ...]
    reuse_percent: tuple[int, ...]
    concurrencies: tuple[int, ...]
    trials_per_cell: int
    purpose: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--direct-url", default="http://127.0.0.1:11434")
    parser.add_argument("--direct-api", choices=("ollama", "openai"), default="ollama")
    parser.add_argument("--direct-model", required=True)
    parser.add_argument("--router-url", default="")
    parser.add_argument("--router-model", default="")
    parser.add_argument("--router-api-key", default="")
    parser.add_argument("--require-router", action="store_true")
    parser.add_argument(
        "--profile",
        choices=("minimal", "extended"),
        default="minimal",
        help="minimal milestone proof or the larger exploratory matrix",
    )
    parser.add_argument("--phases", default="spine,reuse,concurrency")
    parser.add_argument("--context-window", type=int, default=CONTEXT_WINDOW)
    parser.add_argument("--output-tokens", type=int, default=OUTPUT_TOKENS)
    parser.add_argument("--headroom-tokens", type=int, default=HEADROOM_TOKENS)
    parser.add_argument("--serving-parallel-slots", type=int, default=1)
    parser.add_argument("--request-timeout", type=float, default=1800)
    parser.add_argument("--cell-timeout", type=float, default=7200)
    parser.add_argument("--heartbeat-seconds", type=float, default=30)
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--runtime-provenance-file", type=Path, default=None)
    parser.add_argument(
        "--resource-interval",
        type=float,
        default=RESOURCE_INTERVAL_SECONDS,
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def profile_targets(args: argparse.Namespace) -> list[Target]:
    near_limit = args.context_window - args.output_tokens - args.headroom_tokens
    return [
        Target("2k", 2_048, 2_048, args.output_tokens, 0),
        Target("8k", 8_192, 8_192, args.output_tokens, 0),
        Target("16k", 16_384, 16_384, args.output_tokens, 0),
        Target("32k", 32_768, 32_768, args.output_tokens, 0),
        Target(
            "64k-reserved",
            65_536,
            near_limit,
            args.output_tokens,
            args.headroom_tokens,
        ),
    ]


def profile_phases(profile: str = "minimal") -> list[Phase]:
    if profile == "minimal":
        return [
            Phase(
                "spine",
                ("2k", "8k", "16k", "32k", "64k-reserved"),
                (0,),
                (1,),
                MINIMAL_SPINE_TRIALS,
                "one exact cold/warm pair at each capacity rung",
            ),
            Phase(
                "reuse",
                ("32k",),
                (90,),
                (1,),
                1,
                "one immediate 90% backend prefix/KV reuse pair",
            ),
            Phase(
                "concurrency",
                ("8k",),
                (0,),
                (2, 4),
                1,
                "small queue/saturation cells; c1 is supplied by the spine",
            ),
        ]
    return [
        Phase(
            "spine",
            ("2k", "8k", "16k", "32k", "64k-reserved"),
            (0,),
            (1,),
            EXTENDED_SPINE_TRIALS,
            "three unique cold trials at each customer capacity rung",
        ),
        Phase(
            "reuse",
            ("8k", "32k", "64k-reserved"),
            (50, 90),
            (1,),
            1,
            "paired cold-seed/warm requests for backend prefix or KV reuse",
        ),
        Phase(
            "concurrency",
            ("8k", "32k", "64k-reserved"),
            (0,),
            (2, 4),
            1,
            "external queue/saturation probes; c1 is supplied by the spine",
        ),
    ]


def validate_args(args: argparse.Namespace) -> None:
    if args.context_window <= 0 or args.output_tokens <= 0:
        raise ValueError("context window and output tokens must be positive")
    if args.headroom_tokens < 0:
        raise ValueError("headroom tokens must be non-negative")
    if args.context_window != CONTEXT_WINDOW:
        raise ValueError("this customer profile is fixed to an explicit 65,536 window")
    if args.output_tokens != OUTPUT_TOKENS:
        raise ValueError("this customer profile is fixed to output reservation 256")
    if args.context_window - args.output_tokens - args.headroom_tokens <= TOKENS_32K:
        raise ValueError("near-limit target must remain above the 32K rung")
    if args.serving_parallel_slots <= 0:
        raise ValueError("serving parallel slots must be positive")
    if args.resource_interval <= 0:
        raise ValueError("resource interval must be positive")
    if args.router_url and not args.router_model:
        raise ValueError("--router-model is required with --router-url")
    if args.require_router and not args.router_url:
        raise ValueError("--require-router requires --router-url")
    known = {phase.name for phase in profile_phases(args.profile)}
    selected = selected_phases(args)
    unknown = sorted(set(selected) - known)
    if unknown:
        raise ValueError(f"unknown phases: {','.join(unknown)}")


def selected_phases(args: argparse.Namespace) -> list[str]:
    return [item.strip() for item in args.phases.split(",") if item.strip()]


def http_json(base_url: str, path: str, timeout: float = 20) -> Any:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (
        json.JSONDecodeError,
        OSError,
        TimeoutError,
        urllib.error.URLError,
    ) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_readonly(argv: list[str], timeout: float = 30) -> dict[str, Any]:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
        return {
            "argv": argv,
            "returncode": result.returncode,
            "stdout": result.stdout[-100_000:],
            "stderr": result.stderr[-20_000:],
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "argv": argv,
            "returncode": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }


def runtime_facts(base_url: str, model: str) -> dict[str, Any]:
    container = run_readonly(
        [
            "docker",
            "inspect",
            "--format",
            (
                "{{json .Id}} {{json .Config.Image}} {{json .Image}} "
                "{{json .State.Status}} {{json .State.StartedAt}} "
                "{{json .RestartCount}}"
            ),
            "ollama",
        ]
    )
    image = run_readonly(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            (
                "{{json .Id}} {{json .RepoDigests}} {{json .Created}} "
                "{{json .Architecture}} {{json .Os}}"
            ),
            "ollama/ollama:rocm",
        ]
    )
    processes = http_json(base_url, "/api/ps")
    selected = None
    for row in processes.get("models", []) if isinstance(processes, dict) else []:
        if row.get("name") == model or row.get("model") == model:
            selected = row
            break
    processor = None
    if selected:
        size = int(selected.get("size") or 0)
        size_vram = int(selected.get("size_vram") or 0)
        processor = {
            "size_bytes": size,
            "size_vram_bytes": size_vram,
            "gpu_percent": round(100 * size_vram / size, 4) if size else None,
            "cpu_offload_bytes": max(0, size - size_vram) if size else None,
            "context_length": selected.get("context_length"),
        }
    return {
        "schema": "vllm-sr/capacity-runtime-snapshot/v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "kernel": platform.release(),
        },
        "ollama_version": http_json(base_url, "/api/version"),
        "ollama_processes": processes,
        "selected_model": selected,
        "processor_offload": processor,
        "container": container,
        "image": image,
        "rocm": run_readonly(
            [
                "rocm-smi",
                "--showproductname",
                "--showdriverversion",
                "--showuse",
                "--showmeminfo",
                "vram",
                "gtt",
                "--json",
            ]
        ),
    }


def start_resource_sampler(
    script_dir: Path,
    artifact_dir: Path,
    interval: float,
) -> tuple[Path, Path]:
    trace = artifact_dir / "raw" / "resources.jsonl"
    pidfile = artifact_dir / "raw" / "resources.pid"
    subprocess.run(
        [
            sys.executable,
            str(script_dir / "resource_sampler.py"),
            "start",
            "--out",
            str(trace),
            "--pidfile",
            str(pidfile),
            "--interval",
            str(interval),
        ],
        check=True,
    )
    return trace, pidfile


def stop_resource_sampler(
    script_dir: Path,
    trace: Path,
    pidfile: Path,
    summary: Path,
) -> None:
    subprocess.run(
        [
            sys.executable,
            str(script_dir / "resource_sampler.py"),
            "stop",
            "--pidfile",
            str(pidfile),
            "--in",
            str(trace),
            "--out",
            str(summary),
        ],
        check=False,
    )


def matrix_command(
    args: argparse.Namespace,
    *,
    script: Path,
    path_name: str,
    base_url: str,
    api: str,
    model: str,
    api_key: str,
    phase: Phase,
    targets: dict[str, Target],
) -> tuple[list[str], Path, Path, Path]:
    checkpoint = args.artifact_dir / "raw" / f"{path_name}-{phase.name}.jsonl"
    summary = args.artifact_dir / "summary" / f"{path_name}-{phase.name}.json"
    log = args.artifact_dir / "logs" / f"{path_name}-{phase.name}.log"
    contexts = [targets[label].observed_input_target for label in phase.target_labels]
    command = [
        sys.executable,
        str(script),
        "--backend-url",
        base_url,
        "--api",
        api,
        "--model",
        model,
        "--config-label",
        path_name,
        "--phase-label",
        phase.name,
        "--prompt-seed-label",
        "customer-capacity-v1",
        "--contexts",
        ",".join(str(value) for value in contexts),
        "--reuse-percent",
        ",".join(str(value) for value in phase.reuse_percent),
        "--concurrencies",
        ",".join(str(value) for value in phase.concurrencies),
        "--output-tokens",
        str(args.output_tokens),
        "--context-window",
        str(args.context_window),
        "--context-headroom-tokens",
        str(args.headroom_tokens),
        "--trials-per-cell",
        str(phase.trials_per_cell),
        "--calibration-max-attempts",
        "8",
        "--max-target-error-tokens",
        "0",
        "--max-target-error-percent",
        "5",
        "--warmup-requests",
        "0",
        "--request-timeout",
        str(args.request_timeout),
        "--cell-timeout",
        str(args.cell_timeout),
        "--heartbeat-seconds",
        str(args.heartbeat_seconds),
        "--retries",
        str(args.retries),
        "--checkpoint",
        str(checkpoint),
        "--summary",
        str(summary),
    ]
    if api_key:
        command.extend(["--api-key", api_key])
    if api == "ollama":
        command.extend(["--num-ctx", str(args.context_window)])
    if path_name == "router":
        command.extend(
            [
                "--forbid-semantic-cache-hits",
                "--require-semantic-cache-observation",
            ]
        )
    if args.runtime_provenance_file:
        command.extend(["--server-metadata-file", str(args.runtime_provenance_file)])
    if args.resume:
        command.append("--rerun-failed")
    return command, checkpoint, summary, log


def run_streamed(command: list[str], log_path: Path) -> int:
    safe = redact_argv(command)
    print("==> " + " ".join(safe), flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
        return process.wait()


def compact_matrix(summary_path: Path) -> dict[str, Any]:
    if not summary_path.exists():
        return {"present": False, "path": str(summary_path)}
    try:
        data = json.loads(summary_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "present": True,
            "path": str(summary_path),
            "error": f"{type(exc).__name__}: {exc}",
        }
    cells = []
    for cell in data.get("cells") or []:
        observed = [
            request.get("prompt_tokens")
            for key in ("cold_requests", "warm_requests")
            for request in cell.get(key) or []
            if isinstance(request.get("prompt_tokens"), (int, float))
        ]
        cells.append(
            {
                "cell_id": cell.get("cell_id"),
                "status": cell.get("status"),
                "target_input_tokens": cell.get("target_input_tokens"),
                "reuse_percent": cell.get("reuse_percent"),
                "concurrency": cell.get("concurrency"),
                "output_tokens": cell.get("output_tokens"),
                "trials_per_cell": cell.get("trials_per_cell"),
                "observed_prompt_tokens": sorted({int(value) for value in observed}),
                "cold": cell.get("cold"),
                "warm": cell.get("warm"),
                "gates": cell.get("gates"),
                "error": cell.get("error"),
            }
        )
    return {
        "present": True,
        "path": str(summary_path),
        "passed": data.get("passed"),
        "status_counts": data.get("status_counts"),
        "gates": data.get("gates"),
        "cells": cells,
    }


def runtime_stability_gates(
    before: dict[str, Any],
    after: dict[str, Any],
    context_window: int,
) -> list[dict[str, str]]:
    before_container = before.get("container") or {}
    after_container = after.get("container") or {}
    after_processor = after.get("processor_offload") or {}
    return [
        check(
            "ollama_container_unchanged",
            before_container.get("stdout") == after_container.get("stdout"),
            "container id/image/start/restart tuple compared before and after",
        ),
        check(
            "loaded_context_preserved",
            after_processor.get("context_length") == context_window,
            (
                f"observed={after_processor.get('context_length')} "
                f"expected={context_window}"
            ),
        ),
        check(
            "model_remained_fully_gpu_resident",
            after_processor.get("gpu_percent") == FULL_GPU_PERCENT,
            (
                f"gpu_percent={after_processor.get('gpu_percent')} "
                f"cpu_offload_bytes={after_processor.get('cpu_offload_bytes')}"
            ),
        ),
    ]


def check(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "detail": detail,
    }


def redact_argv(argv: list[str]) -> list[str]:
    result: list[str] = []
    hide_next = False
    for item in argv:
        if hide_next:
            result.append("<redacted>")
            hide_next = False
        elif item == "--api-key":
            result.append(item)
            hide_next = True
        else:
            result.append(item)
    return result


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_checksums(root: Path) -> Path:
    output = root / "checksums.sha256"
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == output or path.name.endswith(".pid"):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{digest}  {path.relative_to(root)}")
    output.write_text("\n".join(rows) + "\n")
    return output


def prepare_artifact_dir(args: argparse.Namespace) -> None:
    if (
        args.artifact_dir.exists()
        and not args.resume
        and next(args.artifact_dir.iterdir(), None) is not None
    ):
        raise ValueError(
            "artifact directory is not empty; use a fresh path or --resume"
        )
    for child in ("raw", "summary", "logs"):
        (args.artifact_dir / child).mkdir(parents=True, exist_ok=True)


def profile_paths(
    args: argparse.Namespace,
) -> tuple[list[tuple[str, str, str, str, str]], dict[str, Any]]:
    paths = [
        (
            "direct",
            args.direct_url,
            args.direct_api,
            args.direct_model,
            "",
        )
    ]
    router_status: dict[str, Any]
    if args.router_url:
        paths.append(
            (
                "router",
                args.router_url,
                "openai",
                args.router_model,
                args.router_api_key,
            )
        )
        router_status = {"requested": True, "reason": ""}
    else:
        router_status = {
            "requested": False,
            "reason": "router URL not supplied; orchestration remains executable",
        }
    return paths, router_status


def build_manifest(
    args: argparse.Namespace,
    targets_list: list[Target],
    phases: list[Phase],
    router_status: dict[str, Any],
) -> dict[str, Any]:
    targets = {target.label: target for target in targets_list}
    return {
        "schema": "vllm-sr/customer-capacity-profile-manifest/v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "argv": redact_argv(sys.argv),
        "profile": args.profile,
        "targets": [asdict(target) for target in targets_list],
        "phases": [asdict(phase) for phase in phases],
        "serving_allocation": {
            "context_window": args.context_window,
            "parallel_slots": args.serving_parallel_slots,
            "output_reservation_tokens": args.output_tokens,
            "operational_headroom_tokens": args.headroom_tokens,
            "near_limit_observed_input_target": targets[
                "64k-reserved"
            ].observed_input_target,
            "concurrency_interpretation": (
                "concurrency above serving slots measures queue/saturation, "
                "not simultaneous backend slots"
            ),
        },
        "cache_semantics": {
            "reuse_cohorts": "backend prefix/KV cache only",
            "semantic_response_cache": (
                "separate; router-reported hits fail measured requests"
            ),
        },
        "router": router_status,
        "runtime_provenance_file": (
            str(args.runtime_provenance_file) if args.runtime_provenance_file else None
        ),
    }


def plan_commands(
    args: argparse.Namespace,
    matrix_script: Path,
    paths: list[tuple[str, str, str, str, str]],
    phases: list[Phase],
    targets: dict[str, Target],
) -> list[dict[str, Any]]:
    commands = []
    for path_name, base_url, api, model, api_key in paths:
        for phase in phases:
            command, checkpoint, summary, log = matrix_command(
                args,
                script=matrix_script,
                path_name=path_name,
                base_url=base_url,
                api=api,
                model=model,
                api_key=api_key,
                phase=phase,
                targets=targets,
            )
            commands.append(
                {
                    "path": path_name,
                    "phase": phase.name,
                    "argv": redact_argv(command),
                    "checkpoint": str(checkpoint),
                    "summary": str(summary),
                    "log": str(log),
                }
            )
    return commands


def execute_profile(
    args: argparse.Namespace,
    script_dir: Path,
    matrix_script: Path,
    paths: list[tuple[str, str, str, str, str]],
    phases: list[Phase],
    targets: dict[str, Target],
    commands: list[dict[str, Any]],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    Path | None,
    Path,
]:
    before = runtime_facts(args.direct_url, args.direct_model)
    write_json(args.artifact_dir / "runtime-before.json", before)
    resource_trace: Path | None = None
    resource_pid: Path | None = None
    resource_summary = args.artifact_dir / "summary" / "resources.json"
    outcomes = []
    try:
        resource_trace, resource_pid = start_resource_sampler(
            script_dir,
            args.artifact_dir,
            args.resource_interval,
        )
        for command_spec in commands:
            # The redacted command is for evidence only. Rebuild the executable
            # command so an API key never has to be recovered from the manifest.
            path_row = next(row for row in paths if row[0] == command_spec["path"])
            phase = next(row for row in phases if row.name == command_spec["phase"])
            command, _checkpoint, summary_path, log_path = matrix_command(
                args,
                script=matrix_script,
                path_name=path_row[0],
                base_url=path_row[1],
                api=path_row[2],
                model=path_row[3],
                api_key=path_row[4],
                phase=phase,
                targets=targets,
            )
            started = time.time()
            returncode = run_streamed(command, log_path)
            outcomes.append(
                {
                    "path": path_row[0],
                    "phase": phase.name,
                    "returncode": returncode,
                    "elapsed_s": round(time.time() - started, 4),
                    "summary": compact_matrix(summary_path),
                }
            )
    finally:
        if resource_trace is not None and resource_pid is not None:
            stop_resource_sampler(
                script_dir,
                resource_trace,
                resource_pid,
                resource_summary,
            )

    after = runtime_facts(args.direct_url, args.direct_model)
    write_json(args.artifact_dir / "runtime-after.json", after)
    return before, after, outcomes, resource_trace, resource_summary


def build_profile_summary(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    targets_list: list[Target],
    router_status: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    outcomes: list[dict[str, Any]],
    resource_trace: Path | None,
    resource_summary: Path,
) -> dict[str, Any]:
    stability = runtime_stability_gates(before, after, args.context_window)
    all_requested_passed = bool(outcomes) and all(
        outcome["returncode"] == 0 and outcome["summary"].get("passed") is True
        for outcome in outcomes
    )
    stability_passed = all(item["status"] == "pass" for item in stability)
    return {
        "schema": "vllm-sr/customer-capacity-profile/v1",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "passed": all_requested_passed and stability_passed,
        "targets": [asdict(target) for target in targets_list],
        "serving_allocation": manifest["serving_allocation"],
        "cache_semantics": manifest["cache_semantics"],
        "router": router_status,
        "outcomes": outcomes,
        "runtime_stability_gates": stability,
        "resource_trace": (str(resource_trace) if resource_trace is not None else None),
        "resource_summary": str(resource_summary),
    }


def print_completion(
    summary: dict[str, Any],
    profile_summary: Path,
    checksum_path: Path,
    outcomes: list[dict[str, Any]],
) -> None:
    print(
        json.dumps(
            {
                "passed": summary["passed"],
                "profile_summary": str(profile_summary),
                "checksums": str(checksum_path),
                "outcomes": [
                    {
                        "path": outcome["path"],
                        "phase": outcome["phase"],
                        "returncode": outcome["returncode"],
                        "passed": outcome["summary"].get("passed"),
                    }
                    for outcome in outcomes
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
        prepare_artifact_dir(args)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    targets_list = profile_targets(args)
    targets = {target.label: target for target in targets_list}
    selected = set(selected_phases(args))
    phases = [phase for phase in profile_phases(args.profile) if phase.name in selected]
    script_dir = Path(__file__).resolve().parent
    matrix_script = script_dir / "prefill_matrix.py"
    paths, router_status = profile_paths(args)
    manifest = build_manifest(args, targets_list, phases, router_status)
    commands = plan_commands(args, matrix_script, paths, phases, targets)
    write_json(args.artifact_dir / "manifest.json", manifest)
    write_json(args.artifact_dir / "commands.json", commands)
    if args.dry_run:
        write_checksums(args.artifact_dir)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    before, after, outcomes, resource_trace, resource_summary = execute_profile(
        args,
        script_dir,
        matrix_script,
        paths,
        phases,
        targets,
        commands,
    )
    summary = build_profile_summary(
        args,
        manifest,
        targets_list,
        router_status,
        before,
        after,
        outcomes,
        resource_trace,
        resource_summary,
    )
    profile_summary = args.artifact_dir / "summary" / "capacity-profile.json"
    write_json(profile_summary, summary)
    checksum_path = write_checksums(args.artifact_dir)
    print_completion(summary, profile_summary, checksum_path, outcomes)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
