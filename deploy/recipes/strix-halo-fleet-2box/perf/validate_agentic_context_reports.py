#!/usr/bin/env python3
"""Validate the 2026-07-22/23 agentic-context report set.

The tracked JSON files are the machine-readable source of truth.  This validator
checks their arithmetic and then verifies that every reader-facing report uses
matching units and scope.  It intentionally permits older, explicitly dated
Halo-A/Halo-B measurements while rejecting stale blanket claims about gfx1151.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

RESULTS_DIR = Path("docs/results")
FOUR_PROOF = RESULTS_DIR / "agentic-context-customer-20260722-four-proof-status.json"
EVIDENCE_INDEX = RESULTS_DIR / "agentic-context-customer-20260722-evidence-index.json"

REPORTS = {
    "recipe_readme": Path("README.md"),
    "perf_report": Path("docs/perf-report.md"),
    "hardware_limits": Path("docs/hardware-limits.md"),
    "results_readme": RESULTS_DIR / "README.md",
    "campaign_ledger": RESULTS_DIR / "agentic-prefill-campaign-20260722.md",
    "focused_brief": RESULTS_DIR / "agentic-context-customer-onepager-20260722.md",
    "customer_report": RESULTS_DIR / "customer-report.md",
    "customer_onepager": RESULTS_DIR / "customer-onepager.md",
}

EXPECTED_CONTEXTS = [2048, 8192, 16384, 32768, 65152]
EXPECTED_COLD_TTFT_P50_MS = [3809.1309, 5886.5319, 12624.8144, 30378.7375, 83177.386]
EXPECTED_PROOF_STATUS = {
    "capacity": "PARTIAL_PASS",
    "performance": "MEASURED_NO_AGREED_SLO",
    "quality": "NOT_ACHIEVED",
    "reliability": "NOT_RUN",
}
EXPECTED_GENERATIONS = {
    "controller_prefill_manifest": (
        217,
        "d9dd7ecf6ebd1e72bdcacf1bca4f6f6a4690a12307d9182729da06338ee7ebc6",
    ),
    "interim_demo_manifest": (
        41,
        "57268733bc1734228aaad832124b391fca09547b5f5593191a349f84e0b084fc",
    ),
    "final_customer_manifest": (
        151,
        "bffa040234ed81af022a022bcad3a4a6cc7d3bc0ba7521d82b53b0ef92d5c019",
    ),
    "milestone_capacity_mirror": (
        128,
        "8241bfba5ba85516fe0ab7d507b409b42c4080121bffb03128dfb5c4a6c7b6de",
    ),
}
EXPECTED_CAMPAIGN_CROSSCHECKS = {
    "demo002_ollama_milestone": {
        "cells_total": 8,
        "cells_all_green": 8,
        "measured_requests": 24,
        "http_successes": 24,
        "marker_passes": 24,
        "evidence": [
            "/home/aup/vllm-sr-evidence/demo-002-capacity-matrix/"
            "20260722T023157Z-milestones/proof/spine/summary/direct-spine.json",
            "/home/aup/vllm-sr-evidence/demo-002-capacity-matrix/"
            "20260722T023157Z-milestones/proof/load/summary/direct-reuse.json",
            "/home/aup/vllm-sr-evidence/demo-002-capacity-matrix/"
            "20260722T023157Z-milestones/proof/load/summary/direct-concurrency.json",
        ],
    },
    "halo_a_llamacpp_out256": {
        "checkpoint_records": 6,
        "failed_records": 4,
        "skipped_records": 2,
        "measured_requests": 20,
        "http_successes": 20,
        "marker_passes": 0,
        "evidence": [
            "/home/aup/vllm-sr-evidence/agentic-prefill-20260722/"
            "llamacpp-out256-r0-c1.jsonl",
            "/home/aup/vllm-sr-evidence/agentic-prefill-20260722/"
            "llamacpp-out256-r90-c8.jsonl",
        ],
    },
}
EXPECTED_ARCHIVE_HASHES = {
    "agentic_context_customer_tar_sha256": (
        "d86f9cf206b83a908a2d1eaf11e2047747cbaf89401e2033220979e7d5138a7c"
    ),
    "agentic_prefill_tar_sha256": (
        "68e9811ca088a93c0683804f40b7c5d529967ea828277901fa3f618126d45b73"
    ),
}
CAPACITY_SUMMARIES = (
    "direct-spine.json",
    "direct-reuse.json",
    "direct-concurrency.json",
)
EXPECTED_WINDOW = {
    "configured": 65536,
    "loaded_verified": 65536,
    "max_tested_input_tokens": 65152,
    "max_output_tokens": 256,
    "reserved_headroom_tokens": 128,
    "max_tested_required_total_tokens": 65536,
}
EXPECTED_REPLAY_TOOL_TURNS = 32


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top-level JSON must be an object")
    return value


def _expect(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def _validate_schema_and_window(
    four: dict[str, Any], evidence: dict[str, Any], errors: list[str]
) -> None:
    """Validate schema versions and the configured-versus-tested context ladder."""
    _expect(
        errors,
        four.get("schema") == "agentic-context-window/four-proof-status/v2",
        "four-proof schema must be agentic-context-window/four-proof-status/v2",
    )
    _expect(
        errors,
        evidence.get("schema") == "agentic-context-window/evidence-index/v2",
        "evidence-index schema must be agentic-context-window/evidence-index/v2",
    )

    window = four.get("serving_window", {})
    for key, expected in EXPECTED_WINDOW.items():
        _expect(
            errors,
            window.get(key) == expected,
            f"serving_window.{key} must be {expected}",
        )
    required_total = (
        window.get("max_tested_input_tokens", 0)
        + window.get("max_output_tokens", 0)
        + window.get("reserved_headroom_tokens", 0)
    )
    _expect(
        errors,
        required_total == window.get("max_tested_required_total_tokens"),
        "context arithmetic must be 65152 input + 256 output + 128 headroom = 65536",
    )
    _expect(
        errors,
        window.get("router_declared_metadata") == [131072, 262144],
        "router-declared metadata must remain [131072, 262144] and unproven",
    )


def _validate_capacity(proofs: dict[str, Any], errors: list[str]) -> None:
    """Validate selected-scope cell, request, transport, and marker totals."""
    capacity = proofs.get("capacity", {})
    expected_capacity = {
        "cells_total": 17,
        "cells_all_green": 7,
        "cells_failed": 10,
        "transport_100pct": 17,
        "exact_usage_100pct": 17,
        "failed_marker_gate_only": 10,
        "measured_requests": 174,
        "http_successes": 174,
        "marker_passes": 150,
    }
    for key, expected in expected_capacity.items():
        _expect(
            errors, capacity.get(key) == expected, f"capacity.{key} must be {expected}"
        )
    _expect(
        errors,
        capacity.get("cells_all_green", 0) + capacity.get("cells_failed", 0)
        == capacity.get("cells_total"),
        "green + failed cells must equal total cells",
    )
    _expect(
        errors,
        capacity.get("failed_marker_gate_only") == capacity.get("cells_failed"),
        "all failed cells must remain marker-gate-only for this selected scope",
    )


def _validate_performance(proofs: dict[str, Any], errors: list[str]) -> None:
    """Validate TTFT spine values and backend-specific cache attribution."""
    performance = proofs.get("performance", {})
    spine = performance.get("spine", [])
    _expect(errors, isinstance(spine, list), "performance.spine must be a list")
    if isinstance(spine, list):
        contexts = [row.get("ctx") for row in spine if isinstance(row, dict)]
        _expect(
            errors,
            contexts == EXPECTED_CONTEXTS,
            f"spine contexts must be {EXPECTED_CONTEXTS}",
        )
        p50s = [
            row.get("cold_ttft_ms", {}).get("p50")
            for row in spine
            if isinstance(row, dict)
        ]
        _expect(
            errors, p50s == EXPECTED_COLD_TTFT_P50_MS, "cold TTFT p50 values drifted"
        )
    _expect(
        errors,
        "do not attribute" in performance.get("vllm_apc_crossref", "").lower(),
        "VLLM APC cross-reference must explicitly forbid attribution to Ollama",
    )
    _expect(
        errors,
        "no prefix-cache acceleration"
        in performance.get("ollama_reuse_result", "").lower(),
        "direct Ollama reuse result must state that no acceleration was observed",
    )


def _validate_quality_and_replay(
    four: dict[str, Any], proofs: dict[str, Any], errors: list[str]
) -> None:
    """Validate tool quality, real-agent quality, reliability, and replay facts."""
    quality = proofs.get("quality", {})
    expected_quality = {
        "status": "NOT_ACHIEVED",
        "native_tool_requests": 22,
        "native_tool_json_valid": 22,
        "native_tool_name_correct": 22,
        "native_tool_args_steps_correct": 21,
        "native_tool_args_steps_percent": 95.45,
        "real_agent_transport_successes": 16,
        "real_agent_requests": 16,
        "real_agent_task_successes": 1,
        "real_agent_tasks": 4,
        "real_agent_task_success_percent": 25.0,
    }
    for key, expected in expected_quality.items():
        _expect(
            errors, quality.get(key) == expected, f"quality.{key} must be {expected}"
        )
    _expect(
        errors,
        round(100 * quality.get("native_tool_args_steps_correct", 0) / 22, 2)
        == quality.get("native_tool_args_steps_percent"),
        "native-tool percentage must equal 21/22 = 95.45%",
    )
    _expect(
        errors,
        round(100 * quality.get("real_agent_task_successes", 0) / 4, 1)
        == quality.get("real_agent_task_success_percent"),
        "real-agent percentage must equal 1/4 = 25.0%",
    )
    _expect(
        errors,
        proofs.get("reliability", {}).get("status") == "NOT_RUN",
        "reliability must remain NOT_RUN",
    )
    orchestration = four.get("run_orchestration_limitation", {})
    _expect(
        errors,
        orchestration.get("recorded_stop_reason")
        == "user explicitly deferred remaining replay and new demo-002 llama.cpp validation",
        "recorded replay stop reason must remain the explicit user scope decision",
    )
    _expect(
        errors,
        "no loginctl linger" in orchestration.get("durability_risk", ""),
        "missing login linger must remain a future durability risk",
    )
    _expect(
        errors,
        "explicit user decision" in quality.get("quality_suite", ""),
        "quality-suite status must preserve the explicit user scope closure",
    )

    for replay_name in ("v2_rep1", "v3_rep1"):
        replay = four.get("replay", {}).get(replay_name, {})
        _expect(
            errors,
            replay.get("fixed_passed") is True,
            f"{replay_name} fixed replay must pass",
        )
        _expect(
            errors,
            replay.get("fixed_tool_turns") == EXPECTED_REPLAY_TOOL_TURNS,
            f"{replay_name} total tool turns must be {EXPECTED_REPLAY_TOOL_TURNS}",
        )
        _expect(
            errors,
            replay.get("branch_passed") is True,
            f"{replay_name} branch replay must pass",
        )
        _expect(
            errors,
            replay.get("quality_rows") == 0,
            f"{replay_name} quality rows must be 0",
        )


def _validate_evidence_index(evidence: dict[str, Any], errors: list[str]) -> None:
    """Validate the normalized evidence-index rollups and context budget."""
    index_budget = evidence.get("context_budget", {})
    for key, expected in EXPECTED_WINDOW.items():
        mapped_key = "backend_configured" if key == "configured" else key
        _expect(
            errors,
            index_budget.get(mapped_key) == expected,
            f"evidence context_budget.{mapped_key} must be {expected}",
        )
    _expect(
        errors,
        index_budget.get("router_declared_metadata") == [131072, 262144],
        "evidence router-declared metadata must remain [131072, 262144]",
    )
    index_rollup = evidence.get("capacity_rollup", {})
    index_map = {
        "total": 17,
        "green": 7,
        "failed": 10,
        "transport_100pct": 17,
        "exact_usage_100pct": 17,
        "marker_only_failures": 10,
    }
    for key, expected in index_map.items():
        _expect(
            errors,
            index_rollup.get(key) == expected,
            f"evidence capacity_rollup.{key} must be {expected}",
        )
    for key, expected in {
        "measured_requests": 174,
        "http_successes": 174,
        "marker_passes": 150,
        "invalid_json_lines": 0,
    }.items():
        _expect(
            errors,
            evidence.get("request_rollup", {}).get(key) == expected,
            f"evidence request_rollup.{key} must be {expected}",
        )
    _expect(
        errors,
        evidence.get("proof_statuses") == EXPECTED_PROOF_STATUS,
        "evidence proof statuses drifted",
    )


def _validate_evidence_generations(
    four: dict[str, Any], evidence: dict[str, Any], errors: list[str]
) -> None:
    """Validate manifest generations, archives, mirrors, and campaign crosschecks."""
    four_generations = four.get("evidence_integrity", {})
    index_generations = evidence.get("evidence_generations", {})
    for name, (entries, digest) in EXPECTED_GENERATIONS.items():
        for label, generations in (
            ("four-proof", four_generations),
            ("evidence-index", index_generations),
        ):
            record = generations.get(name, {})
            _expect(
                errors,
                record.get("entries") == entries,
                f"{label} {name} entries must be {entries}",
            )
            _expect(
                errors,
                record.get("manifest_sha256") == digest,
                f"{label} {name} manifest hash drifted",
            )

    for label, generations in (
        ("four-proof", four_generations),
        ("evidence-index", index_generations),
    ):
        archives = generations.get("immutable_v1_archives", {})
        _expect(
            errors,
            archives == EXPECTED_ARCHIVE_HASHES,
            f"{label} immutable archive hashes drifted",
        )

    mirror_verification = (
        "LC_ALL=C source and controller-mirror manifests are byte-identical"
    )
    for label, generations in (
        ("four-proof", four_generations),
        ("evidence-index", index_generations),
    ):
        _expect(
            errors,
            generations.get("milestone_capacity_mirror", {}).get("content_verification")
            == mirror_verification,
            f"{label} milestone mirror verification drifted",
        )

    crosschecks = evidence.get("campaign_crosschecks", {})
    for name, expected in EXPECTED_CAMPAIGN_CROSSCHECKS.items():
        record = crosschecks.get(name, {})
        for key, value in expected.items():
            _expect(
                errors,
                record.get(key) == value,
                f"campaign crosscheck {name}.{key} must be {value}",
            )


def validate_structured(four: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    """Return structured-source consistency errors."""
    errors: list[str] = []
    _validate_schema_and_window(four, evidence, errors)
    proofs = four.get("proofs", {})
    for proof_name, expected_status in EXPECTED_PROOF_STATUS.items():
        _expect(
            errors,
            proofs.get(proof_name, {}).get("status") == expected_status,
            f"four-proof {proof_name} status must be {expected_status}",
        )
    _validate_capacity(proofs, errors)
    _validate_performance(proofs, errors)
    _validate_quality_and_replay(four, proofs, errors)
    _validate_evidence_index(evidence, errors)
    _validate_evidence_generations(four, evidence, errors)
    return errors


def validate_selected_scope(summary: dict[str, Any]) -> list[str]:
    """Validate the preserved final selected-scope rollup."""
    errors: list[str] = []
    totals = summary.get("capacity", {}).get("totals", {})
    expected_totals = {
        "planned_cells": 17,
        "recorded_cells": 17,
        "measured_requests": 174,
        "http_successes": 174,
        "marker_passes": 150,
        "invalid_json_lines": 0,
        "status_counts": {"failed": 10, "success": 7},
    }
    for key, expected in expected_totals.items():
        _expect(
            errors,
            totals.get(key) == expected,
            f"selected-scope capacity total {key} must be {expected}",
        )

    native = summary.get("vllm", {}).get("native_tools", {})
    for key, expected in {
        "requests": 22,
        "json_valid": 22,
        "name_correct": 22,
        "args_correct": 21,
        "step_correct": 21,
    }.items():
        _expect(
            errors,
            native.get(key) == expected,
            f"selected-scope native_tools.{key} must be {expected}",
        )

    smoke = summary.get("vllm", {}).get("real_agent_smoke", {})
    for key, expected in {
        "requests": 16,
        "http_successes": 16,
        "task_count": 4,
        "task_successes": 1,
    }.items():
        _expect(
            errors,
            smoke.get(key) == expected,
            f"selected-scope real_agent_smoke.{key} must be {expected}",
        )
    return errors


def validate_capacity_summaries(summary_dir: Path) -> list[str]:
    """Re-derive exact usage and marker totals from all capacity cells."""
    errors: list[str] = []
    cells: list[dict[str, Any]] = []
    for filename in CAPACITY_SUMMARIES:
        path = summary_dir / filename
        try:
            summary = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
            continue
        phase_cells = summary.get("cells", [])
        if not isinstance(phase_cells, list):
            errors.append(f"{path}: cells must be a list")
            continue
        cells.extend(cell for cell in phase_cells if isinstance(cell, dict))

    requests = 0
    successes = 0
    markers = 0
    green = 0
    marker_only_failed = 0
    for cell in cells:
        cell_id = str(cell.get("cell_id", "unknown"))
        budget = cell.get("context_budget", {})
        target = budget.get("authoritative_observed_input_target")
        required_total = (
            target
            + budget.get("max_output_tokens", 0)
            + budget.get("reserved_headroom_tokens", 0)
            if isinstance(target, int)
            else None
        )
        _expect(
            errors,
            required_total == budget.get("required_total_tokens"),
            f"{cell_id}: context budget arithmetic drifted",
        )
        cell_gate_names: set[str] = set()
        for cohort_name in ("cold", "warm"):
            cohort = cell.get(cohort_name, {})
            count = cohort.get("requests", 0)
            requests += count
            successes += cohort.get("successes", 0)
            gate_failures = cohort.get("gate_failures", {})
            cell_gate_names.update(gate_failures)
            markers += count - gate_failures.get("response_marker", 0)
            _expect(
                errors,
                cohort.get("success_rate") == 1.0,
                f"{cell_id}/{cohort_name}: success_rate must be 1.0",
            )
            _expect(
                errors,
                cohort.get("prompt_usage_field_rate") == 1.0,
                f"{cell_id}/{cohort_name}: prompt usage field rate must be 1.0",
            )
            prompt_tokens = cohort.get("prompt_tokens", {})
            for stat in ("mean", "p50", "p95", "max"):
                _expect(
                    errors,
                    prompt_tokens.get(stat) == float(target),
                    f"{cell_id}/{cohort_name}: prompt_tokens.{stat} "
                    f"must equal {target}",
                )
        if cell.get("status") == "success":
            green += 1
        elif cell_gate_names == {"response_marker"}:
            marker_only_failed += 1
        else:
            errors.append(
                f"{cell_id}: failed cell has non-marker gates "
                f"{sorted(cell_gate_names)}"
            )

    expected = {
        "cells": (len(cells), 17),
        "requests": (requests, 174),
        "successes": (successes, 174),
        "markers": (markers, 150),
        "green": (green, 7),
        "marker-only failed": (marker_only_failed, 10),
    }
    for label, (actual, wanted) in expected.items():
        _expect(
            errors,
            actual == wanted,
            f"capacity summaries {label}: {actual} != {wanted}",
        )
    return errors


def _nested_request_rollup(
    cells: list[dict[str, Any]], label: str
) -> tuple[dict[str, int], list[str]]:
    """Count requests nested in cell checkpoints without treating rows as requests."""
    errors: list[str] = []
    requests: list[dict[str, Any]] = []
    for cell in cells:
        cell_id = str(cell.get("cell_id", "unknown"))
        for key in ("cold_requests", "warm_requests"):
            cohort = cell.get(key, [])
            if not isinstance(cohort, list):
                errors.append(f"{label}/{cell_id}: {key} must be a list")
                continue
            requests.extend(row for row in cohort if isinstance(row, dict))
    rollup = {
        "measured_requests": len(requests),
        "http_successes": sum(
            bool(row.get("success")) and not row.get("error") for row in requests
        ),
        "marker_passes": sum(row.get("marker_correct") is True for row in requests),
    }
    return rollup, errors


def validate_milestone_evidence(mirror_root: Path) -> list[str]:
    """Re-derive the mirror hash and 8-cell milestone rollup."""
    errors: list[str] = []
    manifest_rows: list[str] = []
    try:
        mirror_files = sorted(path for path in mirror_root.rglob("*") if path.is_file())
        for path in mirror_files:
            relative = path.relative_to(mirror_root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_rows.append(f"{digest}  ./{relative}")
    except OSError as exc:
        errors.append(str(exc))
        mirror_files = []
    manifest = ("\n".join(sorted(manifest_rows)) + "\n").encode()
    manifest_digest = hashlib.sha256(manifest).hexdigest()
    expected_mirror = EXPECTED_GENERATIONS["milestone_capacity_mirror"]
    _expect(
        errors,
        len(mirror_files) == expected_mirror[0],
        f"milestone mirror files: {len(mirror_files)} != {expected_mirror[0]}",
    )
    _expect(
        errors,
        manifest_digest == expected_mirror[1],
        f"milestone mirror manifest hash drifted: {manifest_digest}",
    )

    relative_paths = (
        "20260722T023157Z-milestones/proof/spine/summary/direct-spine.json",
        "20260722T023157Z-milestones/proof/load/summary/direct-reuse.json",
        "20260722T023157Z-milestones/proof/load/summary/direct-concurrency.json",
    )
    cells: list[dict[str, Any]] = []
    for relative in relative_paths:
        path = mirror_root / relative
        try:
            summary = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
            continue
        phase_cells = summary.get("cells", [])
        if not isinstance(phase_cells, list):
            errors.append(f"{path}: cells must be a list")
            continue
        cells.extend(cell for cell in phase_cells if isinstance(cell, dict))

    rollup, request_errors = _nested_request_rollup(cells, "milestone")
    errors.extend(request_errors)
    actual = {
        "cells_total": len(cells),
        "cells_all_green": sum(cell.get("status") == "success" for cell in cells),
        **rollup,
    }
    expected = EXPECTED_CAMPAIGN_CROSSCHECKS["demo002_ollama_milestone"]
    for key in (
        "cells_total",
        "cells_all_green",
        "measured_requests",
        "http_successes",
        "marker_passes",
    ):
        _expect(
            errors,
            actual[key] == expected[key],
            f"milestone evidence {key}: {actual[key]} != {expected[key]}",
        )
    return errors


def validate_llamacpp_out256(prefill_root: Path) -> list[str]:
    """Re-derive checkpoint and nested-request totals for llama.cpp output-256."""
    errors: list[str] = []
    paths = (
        prefill_root / "llamacpp-out256-r0-c1.jsonl",
        prefill_root / "llamacpp-out256-r90-c8.jsonl",
    )
    latest: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            errors.append(str(exc))
            continue
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                cell = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{path}:{line_number}: {exc}")
                continue
            if not isinstance(cell, dict) or not isinstance(cell.get("cell_id"), str):
                errors.append(f"{path}:{line_number}: invalid cell checkpoint")
                continue
            latest[cell["cell_id"]] = cell

    cells = list(latest.values())
    statuses = Counter(str(cell.get("status")) for cell in cells)
    rollup, request_errors = _nested_request_rollup(cells, "llamacpp-out256")
    errors.extend(request_errors)
    actual = {
        "checkpoint_records": len(cells),
        "failed_records": statuses["failed"],
        "skipped_records": statuses["skipped"],
        **rollup,
    }
    expected = EXPECTED_CAMPAIGN_CROSSCHECKS["halo_a_llamacpp_out256"]
    for key in (
        "checkpoint_records",
        "failed_records",
        "skipped_records",
        "measured_requests",
        "http_successes",
        "marker_passes",
    ):
        _expect(
            errors,
            actual[key] == expected[key],
            f"llama.cpp output-256 evidence {key}: {actual[key]} != {expected[key]}",
        )
    _expect(
        errors,
        sum("timeout after" in str(cell.get("error")) for cell in cells) == 1,
        "llama.cpp output-256 evidence must retain exactly one timed-out checkpoint",
    )
    return errors


def validate_markdown(texts: dict[str, str]) -> list[str]:
    """Return reader-facing report consistency errors."""
    errors: list[str] = []

    def normalized(value: str) -> str:
        """Ignore prose wrapping and Markdown emphasis during claim matching."""
        return re.sub(r"\s+", " ", value.replace("**", "").replace("`", "")).strip()

    required_by_report = {
        "recipe_readme": [
            "17 cells / 174 measured requests",
            "174/174 HTTP successes",
            "largest tested input was 65,152 tokens",
            "output 256",
            "reserved headroom 128",
        ],
        "perf_report": [
            "17 cells / 174 measured requests",
            "83.2 s",
            "quality **NOT ACHIEVED**",
            "reliability **NOT RUN**",
        ],
        "hardware_limits": [
            "17 cells / 174 measured",
            "65,152",
            "reliability **NOT RUN**",
        ],
        "campaign_ledger": [
            "17 cells and",
            "174 requests",
            "151/151 OK",
            "quality rows 0",
            "24/24 executed requests returned HTTP success and preserved markers",
            "20/20 requests nested in those checkpoint records",
            "8241bfba5ba85516fe0ab7d507b409b42c4080121bffb03128dfb5c4a6c7b6de",
        ],
        "focused_brief": [
            "17 cells / 174 measured requests",
            "Maximum tested input: 65,152 tokens",
            "256 output tokens",
            "128 reserved headroom",
            "Quality — NOT ACHIEVED",
            "Reliability — NOT RUN",
        ],
        "customer_report": [
            "Separate agentic-context addendum",
            "17 cells / 174 requests",
            "historical parity row not rerun",
        ],
        "customer_onepager": [
            "agentic-context addendum",
            "17 cells / 174 requests",
            "historical `rocm/vllm-dev` parity image failed",
        ],
        "results_readme": ["17 cells from", "174 requests", "150 marker passes"],
    }
    for report, snippets in required_by_report.items():
        text = normalized(texts.get(report, ""))
        for snippet in snippets:
            _expect(
                errors,
                normalized(snippet) in text,
                f"{report} missing canonical snippet: {snippet!r}",
            )

    stale = "vLLM is skip-with-reason on gfx1151"
    for report in ("customer_report", "customer_onepager"):
        _expect(
            errors,
            stale not in texts.get(report, ""),
            f"{report} retains stale blanket vLLM claim",
        )

    obsolete_abort_claims = (
        "stopped their background runners when the launching SSH sessions ended",
        "stopped each background runner when its launching SSH session ended",
        "stopped the background runner after its launching SSH session ended",
        "Partial, lifecycle-interrupted; scope later closed",
    )
    for report, text in texts.items():
        for claim in obsolete_abort_claims:
            _expect(
                errors,
                claim not in text,
                f"{report} retains obsolete replay-abort causality: {claim!r}",
            )

    ledger = texts.get("campaign_ledger", "")
    _expect(
        errors,
        ledger.count("## Final four-proof status") == 1,
        "campaign ledger must contain exactly one final proof section",
    )
    _expect(
        errors,
        "## 2026-07-23 finalization:" not in ledger,
        "campaign ledger retains duplicate finalization section",
    )
    focused = texts.get("focused_brief", "")
    _expect(
        errors,
        "Test-proven serving window: 65,536" not in focused,
        "focused brief conflates tested input with configured context",
    )
    _expect(
        errors,
        "serves a real 64K context window" not in focused,
        "focused brief contains ambiguous unqualified 64K claim",
    )
    return errors


def validate_report_set(base: Path) -> list[str]:
    errors: list[str] = []
    try:
        four = _load_json(base / FOUR_PROOF)
        evidence = _load_json(base / EVIDENCE_INDEX)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [str(exc)]
    errors.extend(validate_structured(four, evidence))
    texts: dict[str, str] = {}
    for name, relative in REPORTS.items():
        path = base / relative
        try:
            texts[name] = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(str(exc))
    errors.extend(validate_markdown(texts))
    return errors


def default_base() -> Path:
    return Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        type=Path,
        default=default_base(),
        help="strix-halo-fleet-2box recipe root",
    )
    parser.add_argument(
        "--selected-summary",
        type=Path,
        help="optional preserved analysis/final-selected-scope-summary.json",
    )
    parser.add_argument(
        "--capacity-summary-dir",
        type=Path,
        help="optional directory with direct spine/reuse/concurrency summaries",
    )
    parser.add_argument(
        "--milestone-mirror-root",
        type=Path,
        help="optional controller mirror of demo-002 capacity-matrix evidence",
    )
    parser.add_argument(
        "--prefill-evidence-root",
        type=Path,
        help="optional Halo-A agentic-prefill evidence root",
    )
    args = parser.parse_args()
    errors = validate_report_set(args.base.resolve())
    if args.selected_summary:
        try:
            errors.extend(validate_selected_scope(_load_json(args.selected_summary)))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
    if args.capacity_summary_dir:
        errors.extend(validate_capacity_summaries(args.capacity_summary_dir.resolve()))
    if args.milestone_mirror_root:
        errors.extend(validate_milestone_evidence(args.milestone_mirror_root.resolve()))
    if args.prefill_evidence_root:
        errors.extend(validate_llamacpp_out256(args.prefill_evidence_root.resolve()))
    if errors:
        print(
            f"FAIL: {len(errors)} agentic-context report consistency error(s)",
            file=sys.stderr,
        )
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    success = (
        "PASS: agentic-context structured facts and reader-facing reports "
        "are consistent"
    )
    print(success)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
