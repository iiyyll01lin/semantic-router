"""Turn a fleet run bundle into structured, paper-ready metrics (stdlib only).

The recipe already drops a log bundle per run (`run-all-2box.sh`):
  fleet-status.txt   -- final `fleetctl status` snapshot (versions + hashes)
  fleet-audit.txt    -- the central audit log (ts, box, version, result)
  halo-*-router.log  -- the vllm-sr serve wrapper log (has "Router is ready (after Ns)")
  fleet.env          -- FLEET_MODE + SSH env

This analyzer parses that bundle (no live CCP needed, so it also works offline on
a saved bundle) and, when `CCP_URL`/`FLEET_TOKEN` are set, enriches it with the
desired-config size/hash from the live CCP. It emits:
  - metrics.json  -- machine-readable record for aggregation / a paper table
  - a human summary on stdout

Metrics captured (see docs/research-pipeline.md for the full catalogue):
  - convergence: per desired version, the cross-box span (last_apply - first_apply)
    in seconds, whether every box reached it, and mean/max span; bounded by the
    agent poll interval (context).
  - hash_agreement: do all boxes report the SAME final config hash (the core
    correctness signal -- the CCP-signed hash == each real router's /config/hash).
  - router_readiness_seconds: real vllm-sr cold-start per box (gateway mode).
  - desired_config_bytes / sha256: size of the distributed config (payload scale).

Usage:
  python3 fleet_metrics.py --bundle /path/to/run-YYYYmmdd-HHMMSS [--out metrics.json]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import sys

try:  # optional live-CCP enrichment; the analyzer works without it
    import fleet_lib  # noqa: F401

    _HAVE_FLEET_LIB = True
except Exception:  # pragma: no cover - fleet_lib always ships beside this file
    _HAVE_FLEET_LIB = False

_STATUS_HEADER = re.compile(r"desired_version=(?P<v>\S+)\s+audit_count=(?P<n>\d+)")
_STATUS_ROW = re.compile(
    r"^\s*(?P<box>\S+)\s+version=(?P<v>\S+)\s+result=(?P<r>\S+)\s+hash=(?P<h>\S+)"
)
_AUDIT_ROW = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(?P<box>\S+)\s+"
    r"(?P<v>v\d+)\s+(?P<r>\S+)"
)
_READY = re.compile(r"Router is ready \(after (?P<s>\d+)s")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _parse_ts(ts):
    return _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc
    )


def parse_status(text):
    """Return (desired_version, audit_count, {box: {version, result, hash}})."""
    desired_version, audit_count, boxes = "", 0, {}
    for line in text.splitlines():
        m = _STATUS_HEADER.search(line)
        if m:
            desired_version, audit_count = m.group("v"), int(m.group("n"))
            continue
        m = _STATUS_ROW.match(line)
        if m:
            boxes[m.group("box")] = {
                "version": m.group("v"),
                "result": m.group("r"),
                "hash": m.group("h"),
            }
    return desired_version, audit_count, boxes


def parse_audit(text):
    """Return a list of {ts, box, version, result} dicts (chronological)."""
    rows = []
    for line in text.splitlines():
        m = _AUDIT_ROW.match(line.strip())
        if m:
            rows.append(
                {
                    "ts": m.group("ts"),
                    "box": m.group("box"),
                    "version": m.group("v"),
                    "result": m.group("r"),
                }
            )
    return rows


def convergence_metrics(audit_rows, boxes):
    """Per-version cross-box convergence span in seconds.

    For each version, take the earliest and latest audit timestamp across ALL
    known boxes; the span is how long the fleet took to fully converge that
    version (bounded by the agent poll interval). Only versions that every box
    reported are counted as `applied`.
    """
    box_set = set(boxes) or {r["box"] for r in audit_rows}
    by_version = {}
    for r in audit_rows:
        by_version.setdefault(r["version"], {})[r["box"]] = _parse_ts(r["ts"])
    per_version, spans = [], []
    for version in sorted(by_version, key=lambda v: int(v.lstrip("v") or 0)):
        stamps = by_version[version]
        applied = box_set.issubset(set(stamps))
        span = (max(stamps.values()) - min(stamps.values())).total_seconds()
        per_version.append(
            {
                "version": version,
                "boxes": len(stamps),
                "span_seconds": span,
                "applied_by_all": applied,
            }
        )
        if applied:
            spans.append(span)
    return {
        "per_version": per_version,
        "converged_versions": sum(1 for p in per_version if p["applied_by_all"]),
        "max_cross_box_span_seconds": max(spans) if spans else None,
        "mean_cross_box_span_seconds": (sum(spans) / len(spans)) if spans else None,
    }


def parse_audit_json(text):
    """Parse a JSON-lines audit log (ccp_server audit.log) into record dicts.

    Unlike ``parse_audit`` (which reads the ``fleetctl audit`` TEXT dump), this
    reads the raw JSON the CCP writes, so it can see the ``apply_seconds`` field
    the agent now reports (R9). Silently skips non-JSON / non-dict lines.
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _percentile(sorted_vals, pct):
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def latency_metrics(audit_records):
    """Sub-second hot-reload latency (p50/p95) from the agent write->converge
    timer carried on ``applied`` audit records. Returns None if no samples.
    """
    samples = sorted(
        float(r["apply_seconds"])
        for r in audit_records
        if isinstance(r, dict)
        and r.get("result") == "applied"
        and r.get("apply_seconds") is not None
    )
    if not samples:
        return None
    return {
        "n": len(samples),
        "p50_seconds": round(_percentile(samples, 50), 4),
        "p95_seconds": round(_percentile(samples, 95), 4),
        "mean_seconds": round(sum(samples) / len(samples), 4),
        "min_seconds": round(samples[0], 4),
        "max_seconds": round(samples[-1], 4),
    }


def latency_from_bundle(bundle):
    """Best-effort: find a JSON audit source (records with apply_seconds) and
    compute latency percentiles. Looks in the run bundle, then ``CCP_AUDIT_LOG``.
    Returns None when no timer data is available (older bundles just omit it)."""
    text = ""
    for cand in ("audit.log", "fleet-audit.jsonl", "ccp-audit.jsonl"):
        path = os.path.join(bundle, cand) if bundle else ""
        if path and os.path.isfile(path):
            text = _read(path)
            if text:
                break
    if not text:
        env_audit = os.environ.get("CCP_AUDIT_LOG", "")
        if env_audit and os.path.isfile(env_audit):
            text = _read(env_audit)
    if not text:
        return None
    return latency_metrics(parse_audit_json(text))


def router_readiness(bundle):
    out = {}
    for path in os.listdir(bundle) if bundle and os.path.isdir(bundle) else []:
        m = re.match(r"(halo-[ab])-router\.log$", path)
        if not m:
            continue
        ready = _READY.search(_read(os.path.join(bundle, path)))
        out[m.group(1)] = int(ready.group("s")) if ready else None
    return out


def desired_from_ccp():
    """Best-effort: fetch desired config bytes + sha256 from a live CCP."""
    if not _HAVE_FLEET_LIB:
        return None
    url, token = os.environ.get("CCP_URL"), os.environ.get("FLEET_TOKEN")
    if not url or not token:
        return None
    try:
        status, obj = fleet_lib.http_get_json(
            url.rstrip("/") + "/fleet/desired", token=token
        )
    except Exception:
        return None
    if status != 200 or not isinstance(obj, dict):
        return None
    cfg = obj.get("config", "")
    return {
        "config_bytes": len(cfg.encode("utf-8")),
        "sha256": obj.get("sha256", ""),
        "version": obj.get("version", ""),
    }


def build_metrics(bundle):
    status_txt = _read(os.path.join(bundle, "fleet-status.txt")) if bundle else ""
    audit_txt = _read(os.path.join(bundle, "fleet-audit.txt")) if bundle else ""
    env_txt = _read(os.path.join(bundle, "fleet.env")) if bundle else ""

    mode_m = re.search(r"FLEET_MODE=(\S+)", env_txt)
    poll_m = re.search(r"POLL_INTERVAL=(\d+)", env_txt)
    desired_version, audit_count, boxes = parse_status(status_txt)
    audit_rows = parse_audit(audit_txt)
    conv = convergence_metrics(audit_rows, boxes)
    conv["poll_interval_seconds"] = int(poll_m.group(1)) if poll_m else None

    hashes = {b: rec["hash"] for b, rec in boxes.items()}
    hash_agreement = len(set(hashes.values())) == 1 if hashes else None

    metrics = {
        "schema": "fleet-metrics/v1",
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "bundle": os.path.basename(bundle.rstrip("/")) if bundle else "",
        "fleet_mode": mode_m.group(1) if mode_m else "",
        "boxes": sorted(boxes),
        "desired_version": desired_version,
        "audit_count": audit_count,
        "hash_agreement": hash_agreement,
        "convergence": conv,
        "router_readiness_seconds": router_readiness(bundle),
        "final": boxes,
    }
    ccp = desired_from_ccp()
    if ccp:
        metrics["desired_config"] = ccp
    # R9: sub-second hot-reload latency (p50/p95) from the agent write->converge
    # timer, when a JSON audit source is available (older bundles omit it).
    latency = latency_from_bundle(bundle)
    if latency:
        metrics["hot_reload_latency_seconds"] = latency
    return metrics


def summarize(m):
    lines = ["== fleet metrics (%s, mode=%s) ==" % (m["bundle"], m["fleet_mode"])]
    lines.append(
        "boxes=%s desired=%s audit=%s hash_agreement=%s"
        % (",".join(m["boxes"]), m["desired_version"], m["audit_count"], m["hash_agreement"])
    )
    c = m["convergence"]
    lines.append(
        "convergence: %s versions all-boxes; cross-box span mean=%s max=%s s (poll=%ss)"
        % (
            c["converged_versions"],
            _fmt(c["mean_cross_box_span_seconds"]),
            _fmt(c["max_cross_box_span_seconds"]),
            c["poll_interval_seconds"],
        )
    )
    if m.get("desired_config"):
        dc = m["desired_config"]
        lines.append("desired_config: %s bytes sha256=%s" % (dc["config_bytes"], dc["sha256"][:12]))
    if m.get("hot_reload_latency_seconds"):
        lt = m["hot_reload_latency_seconds"]
        lines.append(
            "hot_reload_latency (write->converge): p50=%.3fs p95=%.3fs mean=%.3fs (n=%d)"
            % (lt["p50_seconds"], lt["p95_seconds"], lt["mean_seconds"], lt["n"])
        )
    rr = ", ".join("%s=%ss" % (b, s) for b, s in sorted(m["router_readiness_seconds"].items()))
    if rr:
        lines.append("router_readiness: " + rr)
    return "\n".join(lines)


def _fmt(x):
    return "n/a" if x is None else ("%.1f" % x)


def main(argv=None):
    p = argparse.ArgumentParser(prog="fleet_metrics")
    p.add_argument("--bundle", required=True, help="run bundle directory")
    p.add_argument("--out", default="", help="write metrics.json here (default: <bundle>/metrics.json)")
    args = p.parse_args(argv)

    metrics = build_metrics(args.bundle)
    out = args.out or os.path.join(args.bundle, "metrics.json")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as exc:
        print("WARNING: could not write %s: %s" % (out, exc), file=sys.stderr)
    print(summarize(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
