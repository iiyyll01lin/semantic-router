#!/usr/bin/env python3
"""Assemble the interactive-latency sweet-spot rollup from raw tokrate_probe.py
JSONs (Task A). Reads p{N}-c{C}.json files, reduces each to the sweet-spot metrics
(single-stream decode tok/s, aggregate tok/s, TTFT p50/p95), and identifies the
knee: the largest --parallel whose TTFT p95 @ concurrency=N stays <= the gate while
aggregate throughput keeps climbing. Stdlib only."""

from __future__ import annotations
import argparse
import glob
import json
import os
import re
import time


def _percentile(values, pct):
    xs = sorted(v for v in values if isinstance(v, (int, float)))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = max(0, min(len(xs) - 1, int(round((pct / 100.0) * (len(xs) - 1)))))
    return xs[k]


def reduce_probe(probe):
    agg = (probe or {}).get("aggregate") or {}
    ttfts = [r.get("ttft_ms") for r in (probe.get("runs_detail") or []) if r.get("ok")]
    return {
        "single_stream_decode_tps": agg.get("decode_tps_median"),
        "decode_tps_mean": agg.get("decode_tps_mean"),
        "aggregate_tps": agg.get("aggregate_decode_tps"),
        "ttft_p50_ms": _percentile(ttfts, 50),
        "ttft_p95_ms": agg.get("ttft_ms_p95"),
        "ttft_mean_ms": agg.get("ttft_ms_mean"),
        "ok_runs": agg.get("ok_runs") or 0,
        "total_runs": agg.get("runs") or 0,
        "success_rate": agg.get("success_rate"),
        "wall_s": agg.get("wall_s"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--indir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--box", default="halo-b")
    ap.add_argument("--host", default="")
    ap.add_argument("--served-id", default="")
    ap.add_argument("--ttft-gate-ms", type=float, default=2000.0)
    args = ap.parse_args()

    points = []
    for path in sorted(glob.glob(os.path.join(args.indir, "p*-c*.json"))):
        m = re.search(r"p(\d+)-c(\d+)\.json$", os.path.basename(path))
        if not m:
            continue
        par, conc = int(m.group(1)), int(m.group(2))
        try:
            with open(path, "r", encoding="utf-8") as fh:
                probe = json.load(fh)
        except (OSError, ValueError):
            continue
        rec = {"parallel": par, "concurrency": conc}
        rec.update(reduce_probe(probe))
        rec["is_operating_point"] = conc == par  # server-slot-matched load
        points.append(rec)
    points.sort(key=lambda r: (r["parallel"], r["concurrency"]))

    # Operating points = concurrency == parallel (the slot-matched load per N).
    ops = {p["parallel"]: p for p in points if p["is_operating_point"]}
    gate = args.ttft_gate_ms
    # Knee = the LARGEST --parallel whose slot-matched TTFT p95 still holds the gate
    # (task definition: "largest --parallel whose TTFT p95 stays <= ~2 s"). The
    # first --parallel that breaks the gate is recorded as the break point.
    gated_ns = [
        n
        for n in sorted(ops)
        if isinstance(ops[n].get("ttft_p95_ms"), (int, float))
        and ops[n]["ttft_p95_ms"] <= gate
        and ops[n].get("aggregate_tps")
    ]
    ungated_ns = [
        n
        for n in sorted(ops)
        if isinstance(ops[n].get("ttft_p95_ms"), (int, float))
        and ops[n]["ttft_p95_ms"] > gate
    ]
    sweet = gated_ns[-1] if gated_ns else None
    break_n = ungated_ns[0] if ungated_ns else None
    ceil_n = max(ops) if ops else None
    observations = []
    rationale = ""
    if sweet is not None:
        s = ops[sweet]
        base = ops[min(ops)]
        agg_gain = (
            (s["aggregate_tps"] / base["aggregate_tps"] - 1.0) * 100.0
            if base.get("aggregate_tps")
            else None
        )
        rationale = (
            "--parallel %d is the largest slot count whose TTFT p95 @ c%d (%s ms) holds the "
            "<= %.0f ms interactive gate (%s tok/s aggregate, ~%s tok/s per stream)."
            % (
                sweet,
                sweet,
                _fmt(s.get("ttft_p95_ms")),
                gate,
                _fmt(s.get("aggregate_tps")),
                _fmt(s.get("single_stream_decode_tps")),
            )
        )
        if break_n is not None:
            rationale += (
                " --parallel %d is where it breaks (TTFT p95 %s ms > gate)."
                % (break_n, _fmt(ops[break_n].get("ttft_p95_ms")))
            )
        # Honest nuance: is the aggregate actually climbing across the gated region?
        if agg_gain is not None and agg_gain < 5.0 and ceil_n is not None:
            observations.append(
                "Aggregate is nearly flat from --parallel %d to %d (%s -> %s tok/s, +%.1f%%): the "
                "MXFP4 120B is memory-bandwidth-bound, so concurrent streams split bandwidth "
                "(~%s tok/s each at c%d). Real aggregate gain only appears at --parallel %d "
                "(%s tok/s @ c%d) but that violates the latency gate (p95 %s ms)."
                % (
                    min(ops),
                    sweet,
                    _fmt(base.get("aggregate_tps")),
                    _fmt(s.get("aggregate_tps")),
                    agg_gain,
                    _fmt(s.get("single_stream_decode_tps")),
                    sweet,
                    ceil_n,
                    _fmt(ops[ceil_n].get("aggregate_tps")),
                    ceil_n,
                    _fmt(ops[ceil_n].get("ttft_p95_ms")),
                )
            )
    else:
        rationale = "No operating point met the TTFT p95 gate; see table."
    # Single-stream (c1) decode vs --parallel: does raising slots hurt the 1-user path?
    c1 = {p["parallel"]: p for p in points if p["concurrency"] == 1}
    if 1 in c1 and c1:
        hi = max(c1)
        d1, dhi = c1[1].get("single_stream_decode_tps"), c1[hi].get(
            "single_stream_decode_tps"
        )
        if (
            isinstance(d1, (int, float))
            and isinstance(dhi, (int, float))
            and dhi < d1 * 0.9
        ):
            observations.append(
                "Single-stream decode @ c1 falls from %s tok/s (--parallel 1) to %s tok/s "
                "(--parallel %d): raising --parallel adds llama.cpp slot/batch overhead that "
                "penalizes even a lone request, so over-provisioning slots hurts the "
                "latency-critical single-user path." % (_fmt(d1), _fmt(dhi), hi)
            )

    rollup = {
        "schema": "ttft-sweetspot/v1",
        "box": args.box,
        "host": args.host,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": "gpt-oss-120b MXFP4 GGUF (%s)"
        % (args.served_id or "ggml-org/gpt-oss-120b-GGUF"),
        "server": "llama.cpp llama-server ROCm (-ngl 999 resident, --no-mmap, --jinja)",
        "served_model_id": args.served_id,
        "shape": {
            "max_tokens": 128,
            "prompt_tokens": 256,
            "api": "openai",
            "ttft_gate_ms": gate,
            "tokrate_deadline_s": 120,
            "probe": "perf/tokrate_probe.py --api openai",
        },
        "parallels_tested": sorted({p["parallel"] for p in points}),
        "points": points,
        "operating_points": [ops[n] for n in sorted(ops)],
        "sweet_spot": {
            "parallel": sweet,
            "ttft_gate_ms": gate,
            "gate_break_parallel": break_n,
            "throughput_ceiling_parallel": ceil_n,
            "rationale": rationale,
            "observations": observations,
        },
        "known_reference": {
            "note": "Prior known points on the winning config (for cross-check).",
            "parallel_1_single_tps": 52.7,
            "parallel_1_ttft_ms_c1": 85,
            "parallel_1_ttft_p95_ms_c8": 17800,
            "parallel_8_aggregate_tps_c8": 95.2,
            "parallel_8_ttft_p95_ms_c8": 3000,
        },
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rollup, fh, indent=2, sort_keys=True)
        fh.write("\n")

    # Human table to stdout.
    print(
        "== TTFT sweet-spot sweep (box=%s, model=gpt-oss-120b MXFP4 GGUF, llama.cpp resident) =="
        % args.box
    )
    hdr = "%-9s %-6s %14s %12s %10s %10s %10s %8s" % (
        "parallel",
        "conc",
        "single_dec_tps",
        "agg_tps",
        "ttftp50",
        "ttftp95",
        "ttftmean",
        "ok/tot",
    )
    print(hdr)
    for p in points:
        star = "  <-- op" if p["is_operating_point"] else ""
        print(
            "%-9d %-6d %14s %12s %10s %10s %10s %8s%s"
            % (
                p["parallel"],
                p["concurrency"],
                _fmt(p.get("single_stream_decode_tps")),
                _fmt(p.get("aggregate_tps")),
                _fmt(p.get("ttft_p50_ms")),
                _fmt(p.get("ttft_p95_ms")),
                _fmt(p.get("ttft_mean_ms")),
                "%d/%d" % (p.get("ok_runs", 0), p.get("total_runs", 0)),
                star,
            )
        )
    print("\nSWEET SPOT: --parallel %s" % sweet)
    print("RATIONALE: %s" % rationale)


def _fmt(x):
    if isinstance(x, (int, float)):
        return "%.1f" % x
    return "-"


if __name__ == "__main__":
    main()
