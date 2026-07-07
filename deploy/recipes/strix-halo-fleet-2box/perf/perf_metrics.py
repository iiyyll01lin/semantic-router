#!/usr/bin/env python3
"""Aggregate per-box perf results into ONE fleet-wide record (stdlib only).

The two perf harnesses each drop a per-box JSON into a run bundle:
  overhead-<box>.json   (overhead-bench.sh / Test 1)
  server-<box>.json     (server-bench.sh / Test 2)

This analyzer parses every such file in a bundle and rolls them into a single
fleet view -- mirroring fleet_metrics.py, which does the same for the config
control plane. It emits:
  perf-metrics.json   machine-readable fleet record (aggregation / paper table)
  perf-summary.md     a human/report-ready markdown summary

Fleet aggregation choices:
  * Overhead throughput-drop per tier is averaged across boxes; the fleet
    "max usable model" is the WORST box's boundary (the safe fleet-wide answer).
  * Server comparison keeps each box's table and marks the fastest measured
    server per box, plus a fleet consensus when boxes agree.

Usage:
  python3 perf_metrics.py --bundle /path/to/run-YYYYmmdd-HHMMSS
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import sys


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return (sum(xs) / len(xs)) if xs else None


def _gib(nbytes):
    return round(nbytes / 1024**3, 2) if isinstance(nbytes, (int, float)) else None


def aggregate_overhead(files):
    boxes = {}
    for path in files:
        rep = _load(path)
        box = rep.get("box") or os.path.basename(path)
        boxes[box] = rep
    # Per-tier drop averaged across boxes.
    tier_drops = {}
    for rep in boxes.values():
        for tier in rep.get("tiers", []):
            d = tier_drops.setdefault(tier["tag"], {"contention": [], "end_to_end": []})
            d["contention"].append(tier.get("throughput_drop_pct_contention"))
            d["end_to_end"].append(tier.get("throughput_drop_pct_end_to_end"))
    per_tier = [
        {
            "tag": tag,
            "mean_drop_pct_contention": _mean(v["contention"]),
            "mean_drop_pct_end_to_end": _mean(v["end_to_end"]),
        }
        for tag, v in sorted(tier_drops.items())
    ]
    # Fleet-safe max usable = the smallest/worst per-box boundary.
    max_usable = [rep.get("max_usable_tag") for rep in boxes.values() if rep.get("max_usable_tag")]
    footprints = [rep.get("stack_footprint", {}).get("stack_container_mem_total_b") for rep in boxes.values()]
    return {
        "boxes": sorted(boxes),
        "per_box": {
            b: {
                "unified_mem_gib": _gib(r.get("unified_mem_total_b")),
                "stack_footprint_gib": _gib(r.get("stack_footprint", {}).get("stack_container_mem_total_b")),
                "max_usable_tag": r.get("max_usable_tag"),
                "first_unusable_tag": r.get("first_unusable_tag"),
            }
            for b, r in boxes.items()
        },
        "per_tier_drop": per_tier,
        "fleet_max_usable_tag": (sorted(set(max_usable))[0] if max_usable else None),
        "mean_stack_footprint_gib": _gib(_mean(footprints)),
    }


def aggregate_servers(files):
    boxes = {}
    for path in files:
        rep = _load(path)
        box = rep.get("box") or os.path.basename(path)
        rows = rep.get("servers", [])
        measured = [r for r in rows if r.get("status") == "measured" and r.get("direct_decode_tps")]
        fastest = max(measured, key=lambda r: r["direct_decode_tps"]) if measured else None
        boxes[box] = {
            "common_base_model": rep.get("common_base_model"),
            "servers": [
                {
                    "server": r["server"],
                    "status": r.get("status"),
                    "quant": r.get("quant"),
                    "direct_decode_tps": r.get("direct_decode_tps"),
                    "direct_ttft_ms": r.get("direct_ttft_ms"),
                    "decode_tps_vs_ollama_pct": r.get("decode_tps_vs_ollama_pct"),
                    "router_overhead_pct": r.get("router_overhead_pct"),
                }
                for r in rows
            ],
            "fastest_server": fastest["server"] if fastest else None,
        }
    fastest_votes = [b["fastest_server"] for b in boxes.values() if b.get("fastest_server")]
    consensus = None
    if fastest_votes and len(set(fastest_votes)) == 1:
        consensus = fastest_votes[0]
    return {"boxes": sorted(boxes), "per_box": boxes, "fleet_fastest_consensus": consensus}


def build(bundle):
    overhead_files = sorted(glob.glob(os.path.join(bundle, "overhead-*.json")))
    server_files = sorted(glob.glob(os.path.join(bundle, "server-*.json")))
    metrics = {
        "schema": "perf-metrics/v1",
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bundle": os.path.basename(bundle.rstrip("/")),
        "overhead": aggregate_overhead(overhead_files) if overhead_files else None,
        "servers": aggregate_servers(server_files) if server_files else None,
    }
    return metrics


def to_markdown(m):
    out = ["# Fleet perf summary (%s)" % m["bundle"], ""]
    ov = m.get("overhead")
    if ov:
        out.append("## Test 1 — vllm-sr co-location overhead")
        out.append("")
        out.append("Fleet-safe max usable model: **%s**  ·  mean stack RAM footprint: **%s GiB**"
                   % (ov.get("fleet_max_usable_tag"), ov.get("mean_stack_footprint_gib")))
        out.append("")
        out.append("| box | unified mem (GiB) | stack RAM (GiB) | max usable | first unusable |")
        out.append("|---|---|---|---|---|")
        for b, r in sorted(ov["per_box"].items()):
            out.append("| %s | %s | %s | %s | %s |" % (
                b, r["unified_mem_gib"], r["stack_footprint_gib"], r["max_usable_tag"], r["first_unusable_tag"]))
        out.append("")
        out.append("| model tier | mean drop % (contention) | mean drop % (end-to-end) |")
        out.append("|---|---|---|")
        for t in ov["per_tier_drop"]:
            out.append("| %s | %s | %s |" % (
                t["tag"],
                "-" if t["mean_drop_pct_contention"] is None else "%.1f" % t["mean_drop_pct_contention"],
                "-" if t["mean_drop_pct_end_to_end"] is None else "%.1f" % t["mean_drop_pct_end_to_end"]))
        out.append("")
    sv = m.get("servers")
    if sv:
        out.append("## Test 2 — inference-server comparison (bundled with vllm-sr)")
        out.append("")
        if sv.get("fleet_fastest_consensus"):
            out.append("Fleet consensus fastest server: **%s**" % sv["fleet_fastest_consensus"])
            out.append("")
        for b, rec in sorted(sv["per_box"].items()):
            out.append("### %s (base %s, fastest: %s)" % (b, rec.get("common_base_model"), rec.get("fastest_server")))
            out.append("")
            out.append("| server | status | decode tok/s | TTFT ms | vs ollama | router overhead % | quant |")
            out.append("|---|---|---|---|---|---|---|")
            for r in rec["servers"]:
                out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                    r["server"], r.get("status"),
                    "-" if r.get("direct_decode_tps") is None else "%.1f" % r["direct_decode_tps"],
                    "-" if r.get("direct_ttft_ms") is None else "%.0f" % r["direct_ttft_ms"],
                    "-" if r.get("decode_tps_vs_ollama_pct") is None else "%+.1f%%" % r["decode_tps_vs_ollama_pct"],
                    "-" if r.get("router_overhead_pct") is None else "%.1f" % r["router_overhead_pct"],
                    r.get("quant") or "-"))
            out.append("")
    if not ov and not sv:
        out.append("_No overhead-*.json or server-*.json found in the bundle._")
    return "\n".join(out) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(prog="perf_metrics", description=__doc__)
    p.add_argument("--bundle", required=True, help="run bundle directory with per-box perf JSON")
    p.add_argument("--out", default="", help="perf-metrics.json path (default: <bundle>/perf-metrics.json)")
    p.add_argument("--summary", default="", help="perf-summary.md path (default: <bundle>/perf-summary.md)")
    args = p.parse_args(argv)

    metrics = build(args.bundle)
    out = args.out or os.path.join(args.bundle, "perf-metrics.json")
    summary = args.summary or os.path.join(args.bundle, "perf-summary.md")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2, sort_keys=True)
            fh.write("\n")
        with open(summary, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(metrics))
    except OSError as exc:
        print("WARNING: could not write perf outputs: %s" % exc, file=sys.stderr)
    print(to_markdown(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
