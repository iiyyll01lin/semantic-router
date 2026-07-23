#!/usr/bin/env python3
"""Capture a secret-safe Ollama runtime/context provenance record.

The default mode is read-only: it inspects Docker plus Ollama's version, tags,
show, and process APIs. ``--load-probe`` is the only mode that runs inference;
it sends one short generation without ``num_ctx`` so ``ollama ps`` proves that
the server-level ``OLLAMA_CONTEXT_LENGTH`` default actually took effect.

Only an allowlist of non-secret Ollama environment variables is recorded.
Container environment, command lines, labels, and model prompts/templates are
deliberately excluded from the output.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "vllm-sr/ollama-runtime-context-proof/v1"
DEFAULT_CONTEXT = 65_536
MAX_EXPERIMENTAL_CONTEXT = 131_072
DEFAULT_MODEL = "gemma4:26b-a4b-it-q8_0"
SAFE_ENV_KEYS = frozenset(
    {
        "HSA_OVERRIDE_GFX_VERSION",
        "OLLAMA_CONTEXT_LENGTH",
        "OLLAMA_FLASH_ATTENTION",
        "OLLAMA_HOST",
        "OLLAMA_KEEP_ALIVE",
        "OLLAMA_KV_CACHE_TYPE",
        "OLLAMA_MAX_LOADED_MODELS",
        "OLLAMA_NUM_PARALLEL",
    }
)


class ProofError(RuntimeError):
    """Expected collection or validation failure."""


def _run(
    argv: list[str], *, timeout: float = 20, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProofError("%s: %s" % (" ".join(argv), exc)) from exc
    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise ProofError("%s: %s" % (" ".join(argv), message))
    return result


def _run_json(argv: list[str], *, timeout: float = 20) -> Any:
    result = _run(argv, timeout=timeout)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProofError("%s returned invalid JSON" % " ".join(argv)) from exc


def _http_json(
    base_url: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProofError("%s %s failed: %s" % (method, path, exc)) from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProofError("%s %s returned invalid JSON" % (method, path)) from exc
    if not isinstance(value, dict):
        raise ProofError("%s %s did not return a JSON object" % (method, path))
    return value


def _normalize_model_name(name: str) -> str:
    value = name.strip()
    return value[:-7] if value.endswith(":latest") else value


def select_model(
    rows: list[dict[str, Any]] | None, model: str
) -> dict[str, Any] | None:
    """Select an Ollama tag/process row while tolerating implicit ``:latest``."""
    wanted = _normalize_model_name(model)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for key in ("name", "model"):
            value = row.get(key)
            if isinstance(value, str) and _normalize_model_name(value) == wanted:
                return row
    return None


def model_context_limit(model_info: dict[str, Any] | None) -> int | None:
    """Return the largest architecture context length exposed by ``/api/show``."""
    values: list[int] = []
    for key, value in (model_info or {}).items():
        if key == "context_length" or key.endswith(".context_length"):
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                values.append(parsed)
    return max(values) if values else None


def safe_container_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce ``docker inspect`` output to a non-secret allowlist."""
    config = raw.get("Config") or {}
    host_config = raw.get("HostConfig") or {}
    state = raw.get("State") or {}
    network_settings = raw.get("NetworkSettings") or {}

    safe_env: dict[str, str] = {}
    for item in config.get("Env") or []:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key in SAFE_ENV_KEYS:
            safe_env[key] = value

    devices = []
    for device in host_config.get("Devices") or []:
        if not isinstance(device, dict):
            continue
        devices.append(
            {
                key: device.get(key)
                for key in ("PathOnHost", "PathInContainer", "CgroupPermissions")
                if device.get(key) is not None
            }
        )

    mounts = []
    for mount in raw.get("Mounts") or []:
        if not isinstance(mount, dict):
            continue
        mounts.append(
            {
                key: mount.get(key)
                for key in ("Type", "Name", "Destination", "RW")
                if mount.get(key) is not None
            }
        )

    networks = sorted((network_settings.get("Networks") or {}).keys())
    ports = network_settings.get("Ports") or {}
    safe_ports: dict[str, list[dict[str, str]] | None] = {}
    for container_port, bindings in ports.items():
        if bindings is None:
            safe_ports[str(container_port)] = None
            continue
        safe_ports[str(container_port)] = [
            {
                key: str(binding.get(key))
                for key in ("HostIp", "HostPort")
                if binding.get(key) is not None
            }
            for binding in bindings
            if isinstance(binding, dict)
        ]

    return {
        "id": raw.get("Id"),
        "name": str(raw.get("Name") or "").lstrip("/"),
        "created": raw.get("Created"),
        "config_image": config.get("Image"),
        "image_id": raw.get("Image"),
        "status": state.get("Status"),
        "running": bool(state.get("Running")),
        "started_at": state.get("StartedAt"),
        "restart_policy": (host_config.get("RestartPolicy") or {}).get("Name"),
        "environment": safe_env,
        "devices": devices,
        "mounts": mounts,
        "networks": networks,
        "ports": safe_ports,
    }


def safe_image_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce image inspection to immutable identity and platform facts."""
    return {
        "id": raw.get("Id"),
        "repo_digests": sorted(raw.get("RepoDigests") or []),
        "created": raw.get("Created"),
        "architecture": raw.get("Architecture"),
        "os": raw.get("Os"),
        "size": raw.get("Size"),
    }


def _read_first_matching(path: str, prefix: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(prefix):
                    return line.split(":", 1)[-1].strip()
    except OSError:
        return None
    return None


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _host_facts() -> dict[str, Any]:
    memory: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, raw_value = line.split(":", 1)
                if key not in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                    continue
                match = re.search(r"(\d+)", raw_value)
                if match:
                    memory[key.lower() + "_bytes"] = int(match.group(1)) * 1024
    except (OSError, ValueError):
        pass

    disk: dict[str, int] = {}
    try:
        stat = os.statvfs("/")
        disk = {
            "root_total_bytes": stat.f_blocks * stat.f_frsize,
            "root_available_bytes": stat.f_bavail * stat.f_frsize,
        }
    except OSError:
        pass

    device_access = {}
    for path in ("/dev/kfd", "/dev/dri/renderD128"):
        device_access[path] = {
            "exists": os.path.exists(path),
            "readable": os.access(path, os.R_OK),
            "writable": os.access(path, os.W_OK),
        }

    return {
        "hostname": platform.node(),
        "os": platform.platform(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "product_name": _read_text("/sys/class/dmi/id/product_name"),
        "cpu_model": _read_first_matching("/proc/cpuinfo", "model name"),
        "cpu_count": os.cpu_count(),
        "memory": memory,
        "disk": disk,
        "gpu_devices": device_access,
    }


def _rocm_facts() -> dict[str, Any]:
    try:
        result = _run(
            [
                "rocm-smi",
                "--showproductname",
                "--showdriverversion",
                "--showmeminfo",
                "all",
                "--showuse",
            ],
            timeout=30,
        )
    except ProofError as exc:
        return {"available": False, "error": str(exc)}

    patterns = {
        "driver": r"Driver version:\s*(.+)",
        "card_series": r"Card series:\s*(.+)",
        "card_model": r"Card model:\s*(.+)",
        "card_sku": r"Card SKU:\s*(.+)",
        "gpu_use_percent": r"GPU use \(%\):\s*(\d+)",
        "vram_total_bytes": r"VRAM Total Memory \(B\):\s*(\d+)",
        "vram_used_bytes": r"VRAM Total Used Memory \(B\):\s*(\d+)",
        "gtt_total_bytes": r"GTT Total Memory \(B\):\s*(\d+)",
        "gtt_used_bytes": r"GTT Total Used Memory \(B\):\s*(\d+)",
    }
    facts: dict[str, Any] = {"available": True}
    for key, pattern in patterns.items():
        match = re.search(pattern, result.stdout)
        if not match:
            continue
        value: Any = match.group(1).strip()
        if key.endswith("_bytes") or key.endswith("_percent"):
            value = int(value)
        facts[key] = value
    return facts


def _docker_facts() -> dict[str, Any]:
    try:
        value = _run_json(["docker", "version", "--format", "{{json .}}"])
    except ProofError as exc:
        return {"available": False, "error": str(exc)}
    client = value.get("Client") or {}
    server = value.get("Server") or {}
    return {
        "available": True,
        "client_version": client.get("Version"),
        "server_version": server.get("Version"),
        "server_os": server.get("Os"),
        "server_arch": server.get("Arch"),
    }


def processor_facts(process_row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not process_row:
        return None
    try:
        size = int(process_row.get("size") or 0)
        size_vram = int(process_row.get("size_vram") or 0)
    except (TypeError, ValueError):
        size = 0
        size_vram = 0
    if size <= 0:
        return {
            "size_bytes": size or None,
            "size_vram_bytes": size_vram or None,
            "gpu_percent": None,
            "cpu_percent": None,
            "label": "unknown",
        }
    gpu_percent = round(100.0 * min(size_vram, size) / size, 2)
    cpu_percent = round(max(0.0, 100.0 - gpu_percent), 2)
    if gpu_percent >= 99.5:
        label = "100% GPU"
    elif gpu_percent <= 0.5:
        label = "100% CPU"
    else:
        label = "%.1f%% GPU / %.1f%% CPU" % (gpu_percent, cpu_percent)
    return {
        "size_bytes": size,
        "size_vram_bytes": size_vram,
        "gpu_percent": gpu_percent,
        "cpu_percent": cpu_percent,
        "label": label,
    }


def _check(
    checks: list[dict[str, Any]],
    name: str,
    status: str,
    detail: str,
) -> None:
    checks.append({"name": name, "status": status, "detail": detail})


def evaluate_checks(
    *,
    container: dict[str, Any] | None,
    expected_image: str,
    expected_context: int,
    expected_parallel: int,
    minimum_context: int,
    allow_experimental_context: bool,
    runtime_version: str | None,
    model_row: dict[str, Any] | None,
    model_limit: int | None,
    process_row: dict[str, Any] | None,
    require_loaded: bool,
) -> list[dict[str, Any]]:
    """Evaluate the configured-vs-observed runtime contract."""
    checks: list[dict[str, Any]] = []
    _check(
        checks,
        "runtime_reachable",
        "pass" if runtime_version else "fail",
        "Ollama %s" % runtime_version if runtime_version else "version API unavailable",
    )
    _check(
        checks,
        "image_reference_pinned",
        "pass" if "@sha256:" in expected_image else "fail",
        expected_image,
    )
    _check(
        checks,
        "minimum_context_policy",
        "pass" if expected_context >= minimum_context else "fail",
        "configured=%d minimum=%d" % (expected_context, minimum_context),
    )
    if expected_context > minimum_context:
        _check(
            checks,
            "experimental_context_acknowledged",
            "pass" if allow_experimental_context else "fail",
            "contexts above %d require explicit acknowledgement" % minimum_context,
        )

    if not container:
        _check(checks, "container_present", "fail", "container not found")
        _check(checks, "container_image_matches", "fail", "container not found")
        _check(checks, "configured_context_matches", "fail", "container not found")
        _check(checks, "parallel_slots_match", "fail", "container not found")
    else:
        _check(checks, "container_present", "pass", container.get("name") or "present")
        actual_image = str(container.get("config_image") or "")
        _check(
            checks,
            "container_image_matches",
            "pass" if actual_image == expected_image else "fail",
            "configured=%s expected=%s" % (actual_image or "<unset>", expected_image),
        )
        environment = container.get("environment") or {}
        actual_context = environment.get("OLLAMA_CONTEXT_LENGTH")
        _check(
            checks,
            "configured_context_matches",
            "pass" if actual_context == str(expected_context) else "fail",
            "container=%s expected=%d"
            % (
                actual_context if actual_context is not None else "<unset>",
                expected_context,
            ),
        )
        actual_parallel = environment.get("OLLAMA_NUM_PARALLEL")
        _check(
            checks,
            "parallel_slots_match",
            "pass" if actual_parallel == str(expected_parallel) else "fail",
            "container=%s expected=%d"
            % (
                actual_parallel if actual_parallel is not None else "<unset>",
                expected_parallel,
            ),
        )

    _check(
        checks,
        "model_present",
        "pass" if model_row else "fail",
        str(
            (model_row or {}).get("name") or (model_row or {}).get("model") or "missing"
        ),
    )
    if model_limit is None:
        _check(
            checks,
            "context_within_model_metadata",
            "not_observed",
            "model metadata did not expose a context length",
        )
    else:
        _check(
            checks,
            "context_within_model_metadata",
            "pass" if expected_context <= model_limit else "fail",
            "configured=%d model_metadata_max=%d" % (expected_context, model_limit),
        )

    if process_row:
        try:
            loaded_context = int(process_row.get("context_length"))
        except (TypeError, ValueError):
            loaded_context = None
        _check(
            checks,
            "loaded_context_matches",
            "pass" if loaded_context == expected_context else "fail",
            "ollama_ps=%s expected=%d"
            % (
                loaded_context if loaded_context is not None else "<missing>",
                expected_context,
            ),
        )
    else:
        _check(
            checks,
            "loaded_context_matches",
            "fail" if require_loaded else "not_observed",
            "model is not loaded; use --load-probe for an allocation proof",
        )
    return checks


def _model_facts(
    model_row: dict[str, Any] | None, show: dict[str, Any] | None
) -> dict[str, Any]:
    row = model_row or {}
    show = show or {}
    return {
        "name": row.get("name") or row.get("model"),
        "digest": row.get("digest"),
        "size": row.get("size"),
        "modified_at": row.get("modified_at"),
        "details": show.get("details") or row.get("details"),
        "capabilities": show.get("capabilities"),
        "parameters": show.get("parameters"),
        "model_info": show.get("model_info") or {},
    }


def _process_facts(
    process_row: dict[str, Any] | None, ollama_ps: str | None
) -> dict[str, Any]:
    if not process_row:
        return {"loaded": False, "ollama_ps": ollama_ps or ""}
    return {
        "loaded": True,
        "name": process_row.get("name"),
        "model": process_row.get("model"),
        "digest": process_row.get("digest"),
        "expires_at": process_row.get("expires_at"),
        "context_length": process_row.get("context_length"),
        "processor": processor_facts(process_row),
        "ollama_ps": ollama_ps or "",
    }


def _load_probe(base_url: str, model: str, *, timeout: float) -> dict[str, Any]:
    response = _http_json(
        base_url,
        "/api/generate",
        payload={
            "model": model,
            "prompt": "Reply with OK.",
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            # Deliberately omit num_ctx. The proof must observe the server default.
            "options": {"temperature": 0, "num_predict": 1},
        },
        timeout=timeout,
    )
    return {
        key: response.get(key)
        for key in (
            "done",
            "done_reason",
            "load_duration",
            "prompt_eval_count",
            "prompt_eval_duration",
            "eval_count",
            "eval_duration",
            "total_duration",
        )
    }


def collect(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    load_probe = None
    if args.load_probe:
        load_probe = _load_probe(args.base_url, args.model, timeout=args.probe_timeout)

    container_raw = _run_json(["docker", "inspect", args.container])
    if not isinstance(container_raw, list) or not container_raw:
        raise ProofError("docker inspect returned no container")
    container = safe_container_facts(container_raw[0])

    image_raw = _run_json(["docker", "image", "inspect", str(container["image_id"])])
    if not isinstance(image_raw, list) or not image_raw:
        raise ProofError("docker image inspect returned no image")
    image = safe_image_facts(image_raw[0])

    version_data = _http_json(args.base_url, "/api/version")
    runtime_version = version_data.get("version")
    tags_data = _http_json(args.base_url, "/api/tags")
    model_row = select_model(tags_data.get("models"), args.model)

    show = None
    show_error = None
    try:
        show = _http_json(
            args.base_url,
            "/api/show",
            # Verbose mode includes the full tensor inventory and can exceed a
            # bounded provenance response. The default still returns model_info,
            # details, parameters, and capabilities needed by this proof.
            payload={"model": args.model},
            timeout=args.http_timeout,
        )
    except ProofError as exc:
        show_error = str(exc)

    ps_data = _http_json(args.base_url, "/api/ps")
    process_row = select_model(ps_data.get("models"), args.model)
    ps_result = _run(
        ["docker", "exec", args.container, "ollama", "ps"],
        timeout=args.http_timeout,
        check=False,
    )
    ollama_ps = ps_result.stdout.strip() if ps_result.returncode == 0 else ""

    model_info = (show or {}).get("model_info") or {}
    limit = model_context_limit(model_info)
    checks = evaluate_checks(
        container=container,
        expected_image=args.expected_image,
        expected_context=args.expected_context,
        expected_parallel=args.expected_parallel,
        minimum_context=args.minimum_context,
        allow_experimental_context=args.allow_experimental_context,
        runtime_version=str(runtime_version) if runtime_version else None,
        model_row=model_row,
        model_limit=limit,
        process_row=process_row,
        require_loaded=args.require_loaded or args.load_probe,
    )
    passed = not any(check["status"] == "fail" for check in checks)

    record = {
        "schema": SCHEMA,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "expected_image": args.expected_image,
            "expected_context": args.expected_context,
            "minimum_context": args.minimum_context,
            "model_metadata_max_context": limit,
            "experimental_above_minimum": args.expected_context > args.minimum_context,
            "experimental_context_acknowledged": args.allow_experimental_context,
            "expected_parallel_slots": args.expected_parallel,
            "primary_model": args.model,
            "capacity_acceptance": "not_run",
            "note": (
                "This record proves configuration/allocation only; exact-token "
                "capacity and quality acceptance belong to later phases."
            ),
        },
        "host": _host_facts(),
        "rocm": _rocm_facts(),
        "docker": _docker_facts(),
        "container": container,
        "image": image,
        "runtime": {
            "version": runtime_version,
            "base_url": args.base_url,
        },
        "model": _model_facts(model_row, show),
        "process": _process_facts(process_row, ollama_ps),
        "load_probe": load_probe,
        "collection_warnings": ([show_error] if show_error else []),
        "checks": checks,
        "passed": passed,
    }
    return record, passed


def _write_atomic(path: str, record: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=target.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, target)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--container", default="ollama")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--expected-context", type=int, default=DEFAULT_CONTEXT)
    parser.add_argument("--minimum-context", type=int, default=DEFAULT_CONTEXT)
    parser.add_argument("--expected-parallel", type=int, default=1)
    parser.add_argument("--allow-experimental-context", action="store_true")
    parser.add_argument("--require-loaded", action="store_true")
    parser.add_argument(
        "--load-probe",
        action="store_true",
        help=(
            "opt in to one short generation, without num_ctx, before capture; "
            "this loads the model but is not a capacity test"
        ),
    )
    parser.add_argument("--http-timeout", type=float, default=30)
    parser.add_argument("--probe-timeout", type=float, default=900)
    parser.add_argument("--output", help="write the provenance JSON atomically")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.expected_context <= 0 or args.minimum_context <= 0:
        print("ERROR: context values must be positive integers", file=sys.stderr)
        return 2
    if args.expected_parallel <= 0:
        print("ERROR: --expected-parallel must be positive", file=sys.stderr)
        return 2
    if args.expected_context > MAX_EXPERIMENTAL_CONTEXT:
        print(
            "ERROR: configured context %d exceeds this phase's experimental ceiling %d"
            % (args.expected_context, MAX_EXPERIMENTAL_CONTEXT),
            file=sys.stderr,
        )
        return 2
    try:
        record, passed = collect(args)
    except ProofError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    if args.output:
        _write_atomic(args.output, record)
    json.dump(record, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
