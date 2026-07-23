import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

PERF_DIR = Path(__file__).parent
PROMPT_TARGET = 512
PROMPT_REUSE_HIGH = 90
PROMPT_REUSE_NONE = 0
PROMPT_OUTPUT = 64
EXPECTED_PREFILL_TPS = 1000.0
EXPECTED_DECODE_TPS = 100.0
EXPECTED_PLANNED_CELLS = 4
EXPECTED_ROUND_REQUESTS = 2
EXPECTED_OPENAI_CACHED_TOKENS = 400
EXPECTED_STREAM_LATENCY_MS = 10.0
EXPECTED_CACHE_QUERY_DELTA = 200
EXPECTED_CACHE_HIT_DELTA = 100
EXPECTED_CACHE_HIT_RATIO = 0.5
EXPECTED_PREFILL_TIME_MS = 250.0
EXPECTED_COMPUTED_PREFILL_TPS = 200.0
EXPECTED_CALIBRATION_ATTEMPTS = 2
EXPECTED_NEAR_LIMIT_TARGET = 65_152
EXPECTED_CONTEXT_WINDOW = 65_536
EXPECTED_EXTENDED_SPINE_TRIALS = 3


def load_modules():
    sys.path.insert(0, str(PERF_DIR))
    transport = __import__("prefill_matrix_transport")
    path = PERF_DIR / "prefill_matrix.py"
    module_spec = importlib.util.spec_from_file_location("prefill_matrix", path)
    assert module_spec is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module, transport


def load_profile():
    path = PERF_DIR / "prefill_capacity_profile.py"
    module_spec = importlib.util.spec_from_file_location(
        "prefill_capacity_profile", path
    )
    assert module_spec is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    assert module_spec.loader is not None
    module_spec.loader.exec_module(module)
    return module


def success_result(prompt_tokens=PROMPT_TARGET, identity="default"):
    return {
        "status": 200,
        "success": True,
        "wall_s": 0.1,
        "ttft_ms": 10.0,
        "stream_chunk_itl_ms_mean": 2.0,
        "stream_chunk_itl_ms_p95": 3.0,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": 4,
        "prompt_token_source": "mock.prompt_tokens",
        "completion_token_source": "mock.completion_tokens",
        "cached_tokens": 0,
        "cached_token_field_present": True,
        "cached_token_source": "usage.prompt_tokens_details.cached_tokens",
        "prefill_duration_ms": 20.0,
        "prefill_tps": 100.0,
        "decode_tps": 40.0,
        "marker_correct": True,
        "text_excerpt": "MATRIX_OK",
        "reasoning_excerpt": "",
        "raw_extensions": {},
        "prompt_sha256": hashlib.sha256(f"prompt:{identity}".encode()).hexdigest(),
        "payload_sha256": hashlib.sha256(f"payload:{identity}".encode()).hexdigest(),
        "semantic_cache_hit": False,
        "error": "",
    }


def test_prompt_builder_controls_shared_prefix():
    _matrix, transport = load_modules()
    cold_high = transport.build_prompt(
        PROMPT_TARGET, PROMPT_REUSE_HIGH, "cell", "cold", 0
    )
    warm_high = transport.build_prompt(
        PROMPT_TARGET, PROMPT_REUSE_HIGH, "cell", "warm", 0
    )
    cold_none = transport.build_prompt(
        PROMPT_TARGET, PROMPT_REUSE_NONE, "cell", "cold", 0
    )
    warm_none = transport.build_prompt(
        PROMPT_TARGET, PROMPT_REUSE_NONE, "cell", "warm", 0
    )

    assert len(os.path.commonprefix((cold_high, warm_high))) > len(
        os.path.commonprefix((cold_none, warm_none))
    )
    assert "answer exactly MATRIX_OK" in cold_high
    assert cold_high.rstrip().endswith("Do not explain.")


def test_planning_records_context_and_total_capacity_skips():
    matrix, _transport = load_modules()
    args = matrix.parse_args(
        [
            "--backend-url",
            "http://unused/v1",
            "--api",
            "openai",
            "--model",
            "model",
            "--config-label",
            "config",
            "--contexts",
            "2k,32k",
            "--reuse-percent",
            "90",
            "--concurrencies",
            "1,8",
            "--output-tokens",
            str(PROMPT_OUTPUT),
            "--context-window",
            "32768",
            "--max-total-context-tokens",
            "4096",
            "--checkpoint",
            "/tmp/unused.jsonl",
        ]
    )
    cells = matrix.planned_cells(args)
    reasons = {
        matrix.cell_id(args, cell): matrix.skip_reason(args, cell) for cell in cells
    }

    assert len(cells) == EXPECTED_PLANNED_CELLS
    assert any("max_total_context_tokens" in reason for reason in reasons.values())
    assert any("context_window" in reason for reason in reasons.values())


def test_explicit_skip_reason_records_unexecuted_cell():
    matrix, _transport = load_modules()
    args = matrix.parse_args(
        [
            "--backend-url",
            "http://unused/v1",
            "--api",
            "openai",
            "--model",
            "model",
            "--config-label",
            "skip",
            "--contexts",
            str(PROMPT_TARGET),
            "--reuse-percent",
            "90",
            "--concurrencies",
            "8",
            "--output-tokens",
            str(PROMPT_OUTPUT),
            "--skip-reason",
            "bounded hardware timeout",
            "--checkpoint",
            "/tmp/unused.jsonl",
        ]
    )

    record = matrix.run_cell(args, matrix.planned_cells(args)[0])

    assert record["status"] == "skipped"
    assert record["skip_reason"] == "bounded hardware timeout"


def test_checkpoint_resume_keeps_one_record_per_cell(tmp_path, monkeypatch):
    matrix, _transport = load_modules()
    checkpoint = tmp_path / "matrix.jsonl"
    summary = tmp_path / "summary.json"

    def fake_request_once(**kwargs):
        return success_result(identity=kwargs["prompt"])

    monkeypatch.setattr(matrix, "request_once", fake_request_once)
    argv = [
        "--backend-url",
        "http://mock/v1",
        "--api",
        "openai",
        "--model",
        "model",
        "--config-label",
        "config",
        "--contexts",
        str(PROMPT_TARGET),
        "--reuse-percent",
        str(PROMPT_REUSE_HIGH),
        "--concurrencies",
        "2",
        "--output-tokens",
        str(PROMPT_OUTPUT),
        "--warmup-requests",
        "0",
        "--checkpoint",
        str(checkpoint),
        "--summary",
        str(summary),
    ]

    assert matrix.main(argv) == 0
    assert matrix.main(argv) == 0
    records = checkpoint.read_text().splitlines()
    written = json.loads(summary.read_text())

    assert len(records) == 1
    assert written["status_counts"] == {"success": 1}
    assert written["cells"][0]["cold"]["requests"] == EXPECTED_ROUND_REQUESTS
    assert written["cells"][0]["warm"]["requests"] == EXPECTED_ROUND_REQUESTS


def test_cell_timeout_is_checkpointable_and_bounded(tmp_path, monkeypatch):
    matrix, _transport = load_modules()
    args = matrix.parse_args(
        [
            "--backend-url",
            "http://unused/v1",
            "--api",
            "openai",
            "--model",
            "model",
            "--config-label",
            "timeout",
            "--contexts",
            str(PROMPT_TARGET),
            "--reuse-percent",
            "0",
            "--concurrencies",
            "1",
            "--output-tokens",
            str(PROMPT_OUTPUT),
            "--cell-timeout",
            "0.05",
            "--heartbeat-seconds",
            "0.01",
            "--checkpoint",
            str(tmp_path / "timeout.jsonl"),
        ]
    )

    def slow_cell(_args, _cell):
        time.sleep(1)
        return {}

    monkeypatch.setattr(matrix, "run_cell", slow_cell)
    started = time.monotonic()
    record = matrix.run_cell_bounded(args, matrix.planned_cells(args)[0])

    assert time.monotonic() - started < 1
    assert record["status"] == "failed"
    assert "cell timeout" in record["error"]


def test_openai_parser_captures_cache_usage_and_stream_metrics(monkeypatch):
    _matrix, transport = load_modules()
    clock = iter((0.01, 0.02, 0.04))
    monkeypatch.setattr(transport.time, "perf_counter", lambda: next(clock))
    lines = [
        b'data: {"choices":[{"delta":{"content":"MATRIX"}}]}\n',
        b'data: {"choices":[{"delta":{"content":"_OK"}}]}\n',
        (
            b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":512,'
            b'"completion_tokens":2,"prompt_tokens_details":{"cached_tokens":400}}}\n'
        ),
        b"data: [DONE]\n",
    ]

    result = transport.parse_openai_stream(lines, started=0.0)

    assert result["success"]
    assert result["marker_correct"]
    assert result["prompt_tokens"] == PROMPT_TARGET
    assert result["cached_tokens"] == EXPECTED_OPENAI_CACHED_TOKENS
    assert result["ttft_ms"] == EXPECTED_STREAM_LATENCY_MS
    assert result["stream_chunk_itl_ms_mean"] == EXPECTED_STREAM_LATENCY_MS


def test_ollama_parser_retains_authoritative_prefill_and_decode():
    _matrix, transport = load_modules()
    lines = [
        b'{"response":"MATRIX_OK","done":false}\n',
        (
            b'{"done":true,"prompt_eval_count":512,'
            b'"prompt_eval_duration":512000000,"eval_count":4,'
            b'"eval_duration":40000000}\n'
        ),
    ]

    result = transport.parse_ollama_stream(lines, started=0.0)

    assert result["prompt_tokens"] == PROMPT_TARGET
    assert result["prefill_tps"] == EXPECTED_PREFILL_TPS
    assert result["decode_tps"] == EXPECTED_DECODE_TPS
    assert result["marker_correct"]


def test_vllm_metric_delta_reports_cache_and_prefill_evidence():
    _matrix, transport = load_modules()
    before = transport.parse_prometheus_metrics(
        "\n".join(
            [
                'vllm:prefix_cache_queries_total{engine="0"} 100',
                'vllm:prefix_cache_hits_total{engine="0"} 10',
                'vllm:request_prefill_time_seconds_sum{engine="0"} 1',
                'vllm:request_prefill_time_seconds_count{engine="0"} 2',
                'vllm:request_prefill_kv_computed_tokens_sum{engine="0"} 90',
            ]
        )
    )
    after = transport.parse_prometheus_metrics(
        "\n".join(
            [
                'vllm:prefix_cache_queries_total{engine="0"} 300',
                'vllm:prefix_cache_hits_total{engine="0"} 110',
                'vllm:request_prefill_time_seconds_sum{engine="0"} 1.5',
                'vllm:request_prefill_time_seconds_count{engine="0"} 4',
                'vllm:request_prefill_kv_computed_tokens_sum{engine="0"} 190',
            ]
        )
    )

    delta = transport.metrics_delta(before, after)

    assert delta["prefix_cache_query_tokens"] == EXPECTED_CACHE_QUERY_DELTA
    assert delta["prefix_cache_hit_tokens"] == EXPECTED_CACHE_HIT_DELTA
    assert delta["prefix_cache_hit_ratio"] == EXPECTED_CACHE_HIT_RATIO
    assert delta["prefill_time_ms_mean"] == EXPECTED_PREFILL_TIME_MS
    assert delta["prefill_computed_tps"] == EXPECTED_COMPUTED_PREFILL_TPS


def test_calibration_adjusts_against_authoritative_usage(monkeypatch):
    matrix, _transport = load_modules()
    args = matrix.parse_args(
        [
            "--backend-url",
            "http://unused",
            "--api",
            "ollama",
            "--model",
            "model",
            "--config-label",
            "calibration",
            "--contexts",
            str(PROMPT_TARGET),
            "--reuse-percent",
            "0",
            "--concurrencies",
            "1",
            "--output-tokens",
            str(PROMPT_OUTPUT),
            "--checkpoint",
            "/tmp/unused.jsonl",
        ]
    )
    observed = iter((480, PROMPT_TARGET))
    monkeypatch.setattr(
        matrix,
        "request_with_retries",
        lambda *_args, **_kwargs: success_result(next(observed)),
    )
    cell = matrix.planned_cells(args)[0]

    calibration = matrix.calibrate_request(
        args,
        cell,
        matrix.cell_id(args, cell),
        "cold",
        0,
        0,
    )

    assert calibration["passed"]
    assert calibration["attempt_count"] == EXPECTED_CALIBRATION_ATTEMPTS
    assert calibration["authoritative_observed_tokens"] == PROMPT_TARGET
    assert calibration["filler_units"] == PROMPT_TARGET - 32


def test_missing_usage_and_semantic_cache_are_executable_failures():
    matrix, _transport = load_modules()
    args = matrix.parse_args(
        [
            "--backend-url",
            "http://unused/v1",
            "--api",
            "openai",
            "--model",
            "model",
            "--config-label",
            "gates",
            "--forbid-semantic-cache-hits",
            "--checkpoint",
            "/tmp/unused.jsonl",
        ]
    )
    result = success_result()
    result["prompt_tokens"] = None
    result["prompt_token_source"] = ""
    result["semantic_cache_hit"] = True

    evaluated = matrix.evaluate_request_gates(args, PROMPT_TARGET, result)

    assert not evaluated["gate_passed"]
    failures = {
        check["name"] for check in evaluated["gates"] if check["status"] == "fail"
    }
    assert "authoritative_prompt_usage_present" in failures
    assert "target_error_tokens" in failures
    assert "semantic_response_cache_miss" in failures


def test_calibration_fails_closed_when_backend_usage_is_missing(monkeypatch):
    matrix, _transport = load_modules()
    args = matrix.parse_args(
        [
            "--backend-url",
            "http://unused",
            "--api",
            "ollama",
            "--model",
            "model",
            "--config-label",
            "missing-usage",
            "--contexts",
            str(PROMPT_TARGET),
            "--reuse-percent",
            "0",
            "--concurrencies",
            "1",
            "--output-tokens",
            str(PROMPT_OUTPUT),
            "--checkpoint",
            "/tmp/unused.jsonl",
        ]
    )
    missing = success_result()
    missing["prompt_tokens"] = None
    missing["prompt_token_source"] = ""
    monkeypatch.setattr(
        matrix,
        "request_with_retries",
        lambda *_args, **_kwargs: dict(missing),
    )
    cell = matrix.planned_cells(args)[0]

    calibration = matrix.calibrate_request(
        args,
        cell,
        matrix.prompt_id(args, cell),
        "cold",
        0,
        0,
    )

    assert not calibration["passed"]
    failures = {
        check["name"] for check in calibration["gates"] if check["status"] == "fail"
    }
    assert "calibration_authoritative_prompt_usage_present" in failures


def test_context_budget_counts_observed_input_output_and_headroom():
    matrix, _transport = load_modules()
    args = matrix.parse_args(
        [
            "--backend-url",
            "http://unused",
            "--api",
            "ollama",
            "--model",
            "model",
            "--config-label",
            "budget",
            "--contexts",
            "65280,65152",
            "--reuse-percent",
            "0",
            "--concurrencies",
            "1",
            "--output-tokens",
            "256",
            "--context-window",
            "65536",
            "--context-headroom-tokens",
            "128",
            "--checkpoint",
            "/tmp/unused.jsonl",
        ]
    )
    cells = matrix.planned_cells(args)

    assert "exceeds context_window" in matrix.skip_reason(args, cells[0])
    assert matrix.skip_reason(args, cells[1]) == ""


def test_customer_profile_is_fixed_and_dry_run_is_reproducible(tmp_path):
    profile = load_profile()
    artifact_dir = tmp_path / "capacity"
    args = profile.build_parser().parse_args(
        [
            "--artifact-dir",
            str(artifact_dir),
            "--direct-model",
            "gemma4:26b-a4b-it-q8_0",
        ]
    )
    profile.validate_args(args)
    targets = {target.label: target for target in profile.profile_targets(args)}

    assert targets["64k-reserved"].observed_input_target == EXPECTED_NEAR_LIMIT_TARGET
    assert (
        targets["64k-reserved"].observed_input_target
        + targets["64k-reserved"].output_reservation_tokens
        + targets["64k-reserved"].operational_headroom_tokens
        == EXPECTED_CONTEXT_WINDOW
    )
    assert (
        profile.main(
            [
                "--artifact-dir",
                str(artifact_dir),
                "--direct-model",
                "gemma4:26b-a4b-it-q8_0",
                "--dry-run",
            ]
        )
        == 0
    )
    commands = json.loads((artifact_dir / "commands.json").read_text())
    by_phase = {row["phase"]: row["argv"] for row in commands}
    assert by_phase["spine"][by_phase["spine"].index("--trials-per-cell") + 1] == "1"
    assert by_phase["reuse"][by_phase["reuse"].index("--contexts") + 1] == "32768"
    assert by_phase["reuse"][by_phase["reuse"].index("--reuse-percent") + 1] == "90"
    assert (
        by_phase["concurrency"][by_phase["concurrency"].index("--concurrencies") + 1]
        == "2,4"
    )

    extended = {phase.name: phase for phase in profile.profile_phases("extended")}
    assert extended["spine"].trials_per_cell == EXPECTED_EXTENDED_SPINE_TRIALS


def test_customer_profile_plans_matched_router_with_cache_separation(tmp_path):
    profile = load_profile()
    artifact_dir = tmp_path / "router-capacity"

    assert (
        profile.main(
            [
                "--artifact-dir",
                str(artifact_dir),
                "--direct-model",
                "gemma4:26b-a4b-it-q8_0",
                "--router-url",
                "http://router.example/v1",
                "--router-model",
                "local/gemma4-26b-q8",
                "--dry-run",
            ]
        )
        == 0
    )
    commands = json.loads((artifact_dir / "commands.json").read_text())
    direct_spine = next(
        row for row in commands if row["path"] == "direct" and row["phase"] == "spine"
    )
    router_spine = next(
        row for row in commands if row["path"] == "router" and row["phase"] == "spine"
    )
    direct_contexts = direct_spine["argv"][direct_spine["argv"].index("--contexts") + 1]
    router_contexts = router_spine["argv"][router_spine["argv"].index("--contexts") + 1]
    direct_seed = direct_spine["argv"][
        direct_spine["argv"].index("--prompt-seed-label") + 1
    ]
    router_seed = router_spine["argv"][
        router_spine["argv"].index("--prompt-seed-label") + 1
    ]

    assert direct_contexts == router_contexts
    assert direct_seed == router_seed
    assert "--forbid-semantic-cache-hits" in router_spine["argv"]
    assert "--require-semantic-cache-observation" in router_spine["argv"]
