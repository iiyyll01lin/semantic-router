#!/usr/bin/env python3
"""profiles-summarize.py -- assemble the Strix Halo OPERATING-PROFILES targeted-
measurement summary (agentic tool-call, multiagent concurrency, EXAONE/Phi-4 quality
completion) into one markdown table set.

Every number is read DIRECTLY from the measured JSON under --indir (default the
sibling quant-frontier/). Missing runs are rendered as ``pending / skip-with-reason``
rather than fabricated -- so the summary is honest whether the detached driver
(profiles-measure.sh) has finished or is still running. Stdlib only.

Inputs (all optional; absent -> marked pending):
  agentic-<label>.json            schema agentic-toolcall/v1
  conc-<label>-c<N>.json          schema tokrate-probe/v1  (N in 1,2,4,8)
  quality-candidate-<label>.json  schema quant-quality/v1  (exaone4_0_32b, phi4_reasoning_plus)

Usage:
  python3 profiles-summarize.py [--indir DIR] [--out summary.md]
"""
from __future__ import annotations

import argparse
import glob
import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INDIR = os.path.join(SCRIPT_DIR, "quant-frontier")

AGENTIC_MODELS = [
    ("gemma4_26b-a4b-it-q8_0", "gemma4:26b-a4b-it-q8_0 (balanced default)"),
    ("qwen3_coder_30b", "qwen3-coder:30b (agentic-speed candidate)"),
    ("gemma4_31b-it-qat", "gemma4:31b-it-qat (quality option)"),
]
CONC_MODELS = [
    ("gemma4_26b", "gemma4:26b (Q4 / throughput)"),
    ("gemma4_26b-a4b-it-q8_0", "gemma4:26b-a4b-it-q8_0 (Q8 / balanced)"),
    ("qwen3_coder_30b", "qwen3-coder:30b (agentic-speed)"),
]
QUALITY_MODELS = [
    ("exaone4_0_32b", "EXAONE 4.0 32B (research-only / non-commercial)"),
    ("phi4_reasoning_plus", "Phi-4 reasoning plus"),
]


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def pct(x):
    return "n/a" if x is None else "%.1f%%" % (100 * x)


def num(x, fmt="%.1f"):
    return "n/a" if x is None else fmt % x


def agentic_section(indir):
    lines = ["## Agentic / tool-calling micro-benchmark",
             "",
             "Frozen 15-task structured-JSON / tool-call set (`data/agentic-toolcall-tasks.json`), "
             "scored by `agentic_toolcall.py`: the model must select the right tool and fill its "
             "arguments as a single JSON object. `step` = valid JSON AND correct tool AND correct "
             "args. Forced-resident (`num_gpu=999`, `use_mmap=false`). Small indicative probe (n=15).",
             "",
             "| Model | step correct | JSON valid | tool-name | args | failure | wall/step | decode tok/s |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    any_found = False
    for label, disp in AGENTIC_MODELS:
        rep = load(os.path.join(indir, "agentic-%s.json" % label))
        if not rep:
            lines.append("| %s | _pending_ | | | | | | |" % disp)
            continue
        any_found = True
        pm = rep.get("per_model", {})
        m = next(iter(pm.values()), {})
        lat = m.get("latency", {})
        lines.append("| %s | %s (%d/%d) | %s | %s | %s | %s | %ss | %s |" % (
            disp, pct(m.get("step_correct_rate")), m.get("step_correct", 0), m.get("n", 0),
            pct(m.get("json_valid_rate")), pct(m.get("name_correct_rate")),
            pct(m.get("args_correct_rate")), pct(m.get("failure_rate")),
            num(lat.get("wall_s_mean"), "%.2f"), num(lat.get("decode_tps_mean"), "%.1f")))
    if not any_found:
        lines.append("")
        lines.append("_All agentic runs pending (detached driver still running or not yet reached)._")
    return lines


def conc_section(indir):
    lines = ["## Multiagent / concurrency sweep",
             "",
             "`tokrate_probe.py` at client concurrency c1/c2/c4/c8, `OLLAMA_NUM_PARALLEL=8`, "
             "forced-resident (`num_gpu=999`, `use_mmap=false`), `max_tokens=128`, `prompt_tokens=256`, "
             "`num_ctx=4096`. Aggregate = total decode tok/s across streams; per-stream = mean single-stream "
             "decode tok/s; TTFT p50/p95 in ms."]
    for label, disp in CONC_MODELS:
        lines += ["", "**%s**" % disp, "",
                  "| c | aggregate tok/s | per-stream tok/s | TTFT p50 (ms) | TTFT p95 (ms) | success |",
                  "| ---: | ---: | ---: | ---: | ---: | ---: |"]
        found = False
        for c in (1, 2, 4, 8):
            rep = load(os.path.join(indir, "conc-%s-c%d.json" % (label, c)))
            if not rep:
                lines.append("| %d | _pending_ | | | | |" % c)
                continue
            found = True
            agg = rep.get("aggregate", {})
            lines.append("| %d | %s | %s | %s | %s | %s |" % (
                c, num(agg.get("aggregate_decode_tps")), num(agg.get("decode_tps_mean")),
                num(agg.get("ttft_ms_median")), num(agg.get("ttft_ms_p95")),
                pct(agg.get("success_rate"))))
        if not found:
            lines.append("")
            lines.append("_Pending._")
    return lines


def quality_section(indir):
    lines = ["## Quality-only completion (EXAONE 4.0 32B, Phi-4 reasoning plus)",
             "",
             "Completes the two candidate-sweep rows that timed out under the capped runner. "
             "`quant-quality.py --no-think --num-predict 2048 --num-gpu 999 --no-use-mmap --limit 42` "
             "on the frozen 42Q MMLU-Pro slice (same set as the Gemma frontier; indicative, ±~7 pp). "
             "EXAONE is **research-only / non-commercial** and never a default.",
             "",
             "| Model | accuracy | correct/n | wall (s) | status |",
             "| --- | ---: | ---: | ---: | --- |"]
    for label, disp in QUALITY_MODELS:
        rep = load(os.path.join(indir, "quality-candidate-%s.json" % label))
        if not rep:
            lines.append("| %s | _pending_ | | | pending / see skips |" % disp)
            continue
        pm = rep.get("per_model", {})
        m = next(iter(pm.values()), {})
        acc = m.get("accuracy")
        status = "measured" if acc is not None and m.get("correct") is not None else "no-result"
        lines.append("| %s | %s | %s/%s | %s | %s |" % (
            disp, pct(acc), m.get("correct", "?"), m.get("n", "?"),
            num(m.get("wall_s"), "%.0f"), status))
    return lines


def provenance(indir):
    lines = ["## Provenance", "",
             "Source JSON (read directly; numbers never hand-edited):", ""]
    pats = ["agentic-*.json", "conc-*.json", "quality-candidate-exaone4_0_32b.json",
            "quality-candidate-phi4_reasoning_plus.json"]
    found = []
    for pat in pats:
        found += sorted(glob.glob(os.path.join(indir, pat)))
    if not found:
        lines.append("- _(none present yet -- detached run in progress)_")
    for p in found:
        rep = load(p)
        gen = (rep or {}).get("generated_utc") or (rep or {}).get("dataset") or ""
        lines.append("- `%s`%s" % (os.path.basename(p), (" (%s)" % gen if gen else "")))
    return lines


def main(argv=None):
    p = argparse.ArgumentParser(prog="profiles-summarize", description=__doc__)
    p.add_argument("--indir", default=DEFAULT_INDIR)
    p.add_argument("--out", default=os.path.join(DEFAULT_INDIR, "profiles-summary-halo-b.md"))
    args = p.parse_args(argv)

    out = ["# Strix Halo operating-profile targeted measurements (Halo-B)",
           "",
           "Targeted follow-ups for the operating-profile matrix (see "
           "[`../../docs/hardware-limits.md` §3.1](../../docs/hardware-limits.md)). Generated by "
           "`profiles-summarize.py` reading the measured JSON in this directory; any run not yet "
           "present is marked _pending_ (never fabricated). Companion to the frontier baseline in "
           "`candidate-summary-halo-b.md`.",
           ""]
    out += agentic_section(args.indir) + [""]
    out += conc_section(args.indir) + [""]
    out += quality_section(args.indir) + [""]
    out += provenance(args.indir) + [""]
    text = "\n".join(out)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print("wrote %s (%d bytes)" % (args.out, len(text)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
