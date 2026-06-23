"""Support functions for the router calibration loop CLI."""

from __future__ import annotations

import http.client
import json
import subprocess
import tempfile
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

from router_calibration_manifest import (
    Probe,
    resolve_acceptance,
    summarize_decision_results,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SEMANTIC_ROUTER_MODULE_ROOT = REPO_ROOT / "src" / "semantic-router"
DEFAULT_REPORT_ROOT = REPO_ROOT / ".augment" / "router-loop"
HTTP_OK_MIN = 200
HTTP_REDIRECT_MIN = 300
HTTP_TIMEOUT_SECONDS = 60

# Resilience knobs for transient router crashes. The classification API can drop
# the connection mid-request when it restarts (notably the ROCm ONNX classifier
# segfault under --platform amd), so retry connection drops a bounded number of
# times before surfacing an actionable error.
DEFAULT_HTTP_RETRIES = 4
DEFAULT_HTTP_BACKOFF = 2.0

# Active resilience settings, seeded from the defaults above. Held in a mutable
# mapping (not rebound module globals) so configure_http_resilience can tune them
# from CLI flags without churning http_json's deep call sites.
_HTTP_RESILIENCE = {
    "retries": DEFAULT_HTTP_RETRIES,
    "backoff": DEFAULT_HTTP_BACKOFF,
}

# Connection-drop exceptions that are NOT urllib.error.URLError subclasses, so
# they would otherwise escape http_json's handlers as a raw traceback.
CONNECTION_DROP_ERRORS = (
    http.client.RemoteDisconnected,
    ConnectionResetError,
    ConnectionAbortedError,
)


def resolve_repo_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def normalize_router_url(router_url: str) -> str:
    normalized = router_url.strip().rstrip("/")
    eval_suffix = "/api/v1/eval"
    if normalized.endswith(eval_suffix):
        normalized = normalized[: -len(eval_suffix)]
    return normalized


def configure_http_resilience(
    max_retries: int | None = None, retry_backoff: float | None = None
) -> None:
    """Set module-level HTTP retry defaults from CLI flags (called once at start)."""
    if max_retries is not None:
        _HTTP_RESILIENCE["retries"] = max(int(max_retries), 0)
    if retry_backoff is not None:
        _HTTP_RESILIENCE["backoff"] = max(float(retry_backoff), 0.0)


def _is_connection_drop(exc: error.URLError) -> bool:
    reason = exc.reason
    return isinstance(reason, (ConnectionError, *CONNECTION_DROP_ERRORS))


def _connection_drop_message(url: str, attempts: int, exc: Exception | None) -> str:
    detail = f"{type(exc).__name__}: {exc}" if exc is not None else "connection dropped"
    return (
        f"router closed the connection to {url} without responding after "
        f"{attempts} attempt(s) ({detail}) — the classification API on :8080 likely "
        "crashed mid-request. Under `--platform amd` this is usually the ROCm ONNX "
        "classifier segfault: ensure `VLLM_SR_AMD_PRESERVE_CPU=1` was exported before "
        "`vllm-sr serve`, check `docker ps` / `docker logs vllm-sr-router-container`, "
        "and see docs/poc/03-strix-halo-runbook.md."
    )


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    retries: int | None = None,
    backoff: float | None = None,
    timeout: float | None = None,
) -> tuple[int, dict[str, Any] | list[Any] | str]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url=url, method=method.upper(), data=body, headers=headers)
    max_retries = max(
        _HTTP_RESILIENCE["retries"] if retries is None else int(retries), 0
    )
    backoff_seconds = max(
        _HTTP_RESILIENCE["backoff"] if backoff is None else float(backoff), 0.0
    )
    timeout_seconds = HTTP_TIMEOUT_SECONDS if timeout is None else timeout

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                status = response.getcode()
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            # A real 4xx/5xx response — surface it, never retry.
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            return exc.code, parsed
        except CONNECTION_DROP_ERRORS as exc:
            last_exc = exc
        except error.URLError as exc:
            if not _is_connection_drop(exc):
                raise RuntimeError(f"request to {url} failed: {exc}") from exc
            last_exc = exc
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            return status, parsed

        if attempt < max_retries and backoff_seconds > 0:
            time.sleep(backoff_seconds * (attempt + 1))

    raise RuntimeError(
        _connection_drop_message(url, max_retries + 1, last_exc)
    ) from last_exc


def ensure_success(status: int, payload: Any, action: str) -> Any:
    if HTTP_OK_MIN <= status < HTTP_REDIRECT_MIN:
        return payload
    raise RuntimeError(
        f"{action} failed with status {status}: {json.dumps(payload, ensure_ascii=False)}"
    )


def preflight_router(router_url: str, timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Fail fast if the router api is not reachable/ready before a full run.

    Uses a short-timeout GET /health (fallback /ready) with no retries so a down
    router is reported immediately with an actionable message rather than after
    partial work or as a raw connection traceback.
    """
    base = normalize_router_url(router_url)
    failures: list[str] = []
    for path in ("/health", "/ready"):
        try:
            status, payload = http_json(
                "GET", f"{base}{path}", retries=0, timeout=timeout_seconds
            )
        except RuntimeError as exc:
            failures.append(f"{path}: {exc}")
            continue
        if HTTP_OK_MIN <= status < HTTP_REDIRECT_MIN:
            return {
                "router_url": base,
                "checked_at": utc_now(),
                "endpoint": path,
                "status_code": status,
                "payload": payload,
            }
        failures.append(f"{path}: status {status}")

    raise RuntimeError(
        f"router api at {base} is not reachable or ready before the calibration run "
        f"(checked /health then /ready): {'; '.join(failures)}. Confirm `vllm-sr serve` "
        "is running (`docker ps` / `docker logs vllm-sr-router-container`); under "
        "`--platform amd` ensure `VLLM_SR_AMD_PRESERVE_CPU=1` was exported before serve. "
        "See docs/poc/03-strix-halo-runbook.md."
    )


def fetch_router_snapshot(router_url: str) -> dict[str, Any]:
    base = normalize_router_url(router_url)
    router_cfg = ensure_success(
        *http_json("GET", f"{base}/config/router"),
        action="GET /config/router",
    )
    versions = ensure_success(
        *http_json("GET", f"{base}/config/router/versions"),
        action="GET /config/router/versions",
    )
    ready_status, ready_payload = http_json("GET", f"{base}/ready")
    health_status, health_payload = http_json("GET", f"{base}/health")
    return {
        "router_url": base,
        "captured_at": utc_now(),
        "config_router": router_cfg,
        "config_versions": versions,
        "ready": {"status_code": ready_status, "payload": ready_payload},
        "health": {"status_code": health_status, "payload": health_payload},
    }


def wait_for_router_ready(
    router_url: str, timeout_seconds: float = 300.0, interval_seconds: float = 5.0
) -> dict[str, Any]:
    base = normalize_router_url(router_url)
    deadline = time.monotonic() + timeout_seconds
    last_status = 0
    last_payload: Any = {"status": "unknown", "ready": False}

    while time.monotonic() < deadline:
        status, payload = http_json("GET", f"{base}/ready")
        last_status = status
        last_payload = payload
        if (
            HTTP_OK_MIN <= status < HTTP_REDIRECT_MIN
            and isinstance(payload, dict)
            and bool(payload.get("ready"))
        ):
            return {
                "router_url": base,
                "checked_at": utc_now(),
                "status_code": status,
                "payload": payload,
            }
        time.sleep(max(interval_seconds, 0.1))

    raise RuntimeError(
        "router did not become ready after deploy: "
        f"status={last_status}, payload={json.dumps(last_payload, ensure_ascii=False)}"
    )


def evaluate_probe(router_url: str, probe: Probe) -> dict[str, Any]:
    request_payload: dict[str, Any]
    if probe.messages:
        request_payload = {"messages": list(probe.messages)}
    else:
        request_payload = {"text": probe.query}
    status, payload = http_json(
        "POST",
        f"{normalize_router_url(router_url)}/api/v1/eval",
        request_payload,
    )
    data = ensure_success(status, payload, "POST /api/v1/eval")
    if not isinstance(data, dict):
        raise RuntimeError(
            f"unexpected eval payload for probe {probe.probe_id}: {data!r}"
        )

    decision_result = data.get("decision_result") or {}
    actual_decision = (
        str(data.get("routing_decision") or "").strip()
        or str(decision_result.get("decision_name") or "").strip()
    )
    actual_models = data.get("recommended_models") or []
    matched = actual_decision == probe.expected_decision
    return {
        "id": probe.probe_id,
        "decision_id": probe.decision_id,
        "variant_id": probe.variant_id,
        "expected_decision": probe.expected_decision,
        "expected_alias": probe.expected_alias,
        "query": probe.query or summarize_probe_messages(probe.messages),
        "messages": list(probe.messages),
        "notes": probe.notes,
        "tags": list(probe.tags),
        "actual_decision": actual_decision,
        "matched": matched,
        "recommended_models": actual_models,
        "used_signals": decision_result.get("used_signals") or {},
        "matched_signals": decision_result.get("matched_signals") or {},
        "unmatched_signals": decision_result.get("unmatched_signals") or {},
        "signal_confidences": data.get("signal_confidences") or {},
        "metrics": data.get("metrics") or {},
    }


def summarize_probe_messages(messages: tuple[dict[str, Any], ...]) -> str:
    if not messages:
        return ""
    for message in reversed(messages):
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        content = summarize_message_content(message.get("content"))
        if content:
            return content
    return json.dumps(list(messages), ensure_ascii=False)


def summarize_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        part_type = str(item.get("type") or "").strip().lower()
        if part_type not in ("", "text", "input_text"):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def evaluate_probes(
    router_url: str, probes: Iterable[Probe], manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    results = [evaluate_probe(router_url, probe) for probe in probes]
    decision_summaries = summarize_decision_results(results, manifest or {})
    matched = sum(1 for result in results if result["matched"])
    total = len(results)
    matched_decisions = sum(
        1 for summary in decision_summaries if bool(summary.get("passed"))
    )
    total_decisions = len(decision_summaries)
    acceptance = resolve_acceptance(manifest or {})
    probe_success_rate = round((matched / total) * 100, 1) if total else 0.0
    decision_success_rate = (
        round((matched_decisions / total_decisions) * 100, 1)
        if total_decisions
        else 0.0
    )
    return {
        "router_url": normalize_router_url(router_url),
        "evaluated_at": utc_now(),
        "matched": matched,
        "total": total,
        "success_rate": probe_success_rate,
        "matched_decisions": matched_decisions,
        "total_decisions": total_decisions,
        "decision_success_rate": decision_success_rate,
        "acceptance": acceptance,
        "passed": (
            probe_success_rate >= acceptance["min_probe_pass_rate"]
            and all(summary["passed"] for summary in decision_summaries)
        ),
        "decisions": decision_summaries,
        "results": results,
    }


def run_validate(dsl_path: Path | None, yaml_path: Path | None) -> dict[str, Any]:
    dsl_path = resolve_repo_path(dsl_path)
    yaml_path = resolve_repo_path(yaml_path)

    if dsl_path is None and yaml_path is None:
        return {"skipped": True, "reason": "no local DSL or YAML asset provided"}

    temp_dsl: Path | None = None
    target_dsl = dsl_path
    repo_cwd = str(SEMANTIC_ROUTER_MODULE_ROOT)

    try:
        if target_dsl is None:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".dsl", prefix="router-calibration-", delete=False
            ) as temp_file:
                temp_dsl = Path(temp_file.name)
            decompile_cmd = [
                "go",
                "run",
                "./cmd/dsl",
                "decompile",
                "-o",
                str(temp_dsl),
                str(yaml_path),
            ]
            decompile_run = subprocess.run(
                decompile_cmd,
                cwd=repo_cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            if decompile_run.returncode != 0:
                return {
                    "skipped": False,
                    "valid": False,
                    "mode": "yaml->dsl",
                    "command": decompile_cmd,
                    "returncode": decompile_run.returncode,
                    "stdout": decompile_run.stdout,
                    "stderr": decompile_run.stderr,
                }
            target_dsl = temp_dsl

        validate_cmd = [
            "go",
            "run",
            "./cmd/dsl",
            "validate",
            str(target_dsl),
        ]
        validate_run = subprocess.run(
            validate_cmd,
            cwd=repo_cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "skipped": False,
            "valid": validate_run.returncode == 0,
            "mode": "dsl",
            "command": validate_cmd,
            "returncode": validate_run.returncode,
            "stdout": validate_run.stdout,
            "stderr": validate_run.stderr,
        }
    finally:
        if temp_dsl is not None:
            temp_dsl.unlink(missing_ok=True)


def deploy_config(
    router_url: str, yaml_path: Path, dsl_path: Path | None
) -> dict[str, Any]:
    yaml_path = resolve_repo_path(yaml_path)
    dsl_path = resolve_repo_path(dsl_path)
    payload = {"yaml": yaml_path.read_text(encoding="utf-8")}
    if dsl_path is not None:
        payload["dsl"] = dsl_path.read_text(encoding="utf-8")
    status, response = http_json(
        "PUT",
        f"{normalize_router_url(router_url)}/config/router",
        payload,
    )
    return ensure_success(status, response, "PUT /config/router")


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_report_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_REPORT_ROOT / stamp
