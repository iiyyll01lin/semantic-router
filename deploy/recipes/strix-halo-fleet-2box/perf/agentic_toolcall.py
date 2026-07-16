#!/usr/bin/env python3
"""agentic_toolcall.py -- score a model's STRUCTURED-JSON / TOOL-CALL ability for the
Strix Halo "agentic" operating profile.

Stdlib only (urllib/json/re/argparse), matching the rest of perf/ (offline-first). It
reads a frozen, answer-keyed tool-call task set (default
perf/data/agentic-toolcall-tasks.json). Every task presents the SAME full tool
catalog plus one user request; the model must (1) select the correct tool, (2) fill
the required arguments, and (3) emit ONLY a single JSON object
``{"name": <tool>, "arguments": {...}}``. We stream /api/generate so the SAME run
captures the full response (for scoring) AND per-step latency (TTFT + decode tok/s
from Ollama's server-side timings), then score:

  * json_valid   -- a JSON object with a "name" key was recovered from the response
  * name_correct -- the selected tool matches the expected tool
  * args_correct -- every expected argument passes its check (extra keys are allowed)
  * step_correct -- json_valid AND name_correct AND args_correct
  * failure_rate -- 1 - json_valid_rate (responses we could not parse as a tool call)

Argument checks (per expected key):
  equals        string equal, case-insensitive, trimmed
  contains      expected substring present, case-insensitive
  contains_all  every expected substring present (value is a list), case-insensitive
  equals_number numeric equality after coercing "600"/"600.0"/600 -> float

It NEVER serves/deploys/mutates config -- it only sends inference requests. Transport
and the --no-think / --num-gpu / --no-use-mmap forced-residency flags mirror
quant-quality.py so a thinking model (Gemma) answers with the JSON directly and a
big model stays VRAM-resident on the 96 GiB carveout.

Usage:
  python3 agentic_toolcall.py --api ollama --backend-url http://localhost:11434 \
      --models gemma4:26b-a4b-it-q8_0 qwen3-coder:30b \
      --no-think --num-predict 512 --num-gpu 999 --no-use-mmap \
      --out agentic-gemma-vs-qwen.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATASET = os.path.join(SCRIPT_DIR, "data", "agentic-toolcall-tasks.json")

SYSTEM_PREAMBLE = (
    "You are a function-calling assistant. You can call exactly one of the tools "
    "listed below. For the user's request, respond with ONLY a single JSON object of "
    "the form {\"name\": \"<tool_name>\", \"arguments\": {<key>: <value>, ...}}. Use the "
    "exact tool name and the exact argument keys from the tool's parameters. Do not "
    "include any explanation, prose, comments, or markdown code fences -- output the "
    "raw JSON object and nothing else."
)


def build_prompt(catalog_text, query):
    return (
        "%s\n\nAvailable tools:\n%s\n\nUser request: %s\n\nJSON tool call:"
        % (SYSTEM_PREAMBLE, catalog_text, query)
    )


def render_catalog(tools):
    lines = []
    for t in tools:
        params = ", ".join(
            "%s (%s)" % (k, v) for k, v in (t.get("parameters") or {}).items()
        )
        lines.append(
            "- %s: %s | parameters: %s" % (t["name"], t.get("description", ""), params)
        )
    return "\n".join(lines)


def extract_tool_call(text):
    """Recover the model's tool call from a full (possibly reasoning-laden) response.

    Scans for balanced ``{...}`` spans, json.loads each, and returns the LAST one that
    parses AND carries a "name" key -- so a final verdict is scored even if a thinking
    model emits provisional JSON or reasoning first. Markdown fences are tolerated.
    Returns (obj_or_None, raw_span_or_None).
    """
    if not text:
        return None, None
    cleaned = text.replace("```json", "```")
    best = None
    best_span = None
    depth = 0
    start = -1
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    span = cleaned[start : i + 1]
                    try:
                        obj = json.loads(span)
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict) and "name" in obj:
                        best = obj
                        best_span = span
    return best, best_span


def _as_text(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def check_arg(check, expected, actual):
    if actual is None and check != "absent":
        return False
    if check == "equals":
        return _as_text(actual).strip().lower() == str(expected).strip().lower()
    if check == "contains":
        return str(expected).strip().lower() in _as_text(actual).lower()
    if check == "contains_all":
        hay = _as_text(actual).lower()
        return all(str(v).strip().lower() in hay for v in expected)
    if check == "equals_number":
        try:
            return abs(float(actual) - float(expected)) < 1e-6
        except (TypeError, ValueError):
            # tolerate "600 seconds" / "$100" style values: pull the first number
            m = re.search(r"-?\d+(?:\.\d+)?", _as_text(actual))
            return bool(m) and abs(float(m.group()) - float(expected)) < 1e-6
    return False


def score_task(task, obj):
    """Return (json_valid, name_ok, args_ok, arg_detail)."""
    if not isinstance(obj, dict) or "name" not in obj:
        return False, False, False, {}
    exp = task["expect"]
    name_ok = str(obj.get("name", "")).strip().lower() == str(exp["name"]).strip().lower()
    args = obj.get("arguments")
    if not isinstance(args, dict):
        args = obj.get("parameters") if isinstance(obj.get("parameters"), dict) else {}
    detail = {}
    all_ok = True
    for key, spec in exp.get("args", {}).items():
        # tolerate case-variant arg keys
        actual = args.get(key)
        if actual is None:
            for ak in args:
                if ak.lower() == key.lower():
                    actual = args[ak]
                    break
        ok = check_arg(spec["check"], spec["value"], actual)
        detail[key] = ok
        all_ok = all_ok and ok
    return True, name_ok, all_ok, detail


def _percentile(values, pct):
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = max(0, min(len(xs) - 1, int(round((pct / 100.0) * (len(xs) - 1)))))
    return xs[k]


def build_options(args):
    opts = {"temperature": 0, "num_predict": args.num_predict}
    if args.num_ctx and args.num_ctx > 0:
        opts["num_ctx"] = args.num_ctx
    if args.num_gpu is not None and args.num_gpu >= 0:
        opts["num_gpu"] = args.num_gpu
    if args.use_mmap is not None:
        opts["use_mmap"] = bool(args.use_mmap)
    return opts


def stream_ollama(base, model, prompt, opts, timeout, think=None):
    """One streaming /api/generate call. Returns (full_text, ttft_ms, decode_tps, wall_s)."""
    payload = {"model": model, "prompt": prompt, "stream": True, "options": opts}
    if think is not None:
        payload["think"] = bool(think)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/api/generate", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    t_first = None
    parts = []
    final = {}
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted local URL)
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            obj = json.loads(line)
            piece = obj.get("response")
            if piece:
                if t_first is None:
                    t_first = time.perf_counter()
                parts.append(piece)
            if obj.get("done"):
                final = obj
                break
    wall_s = time.perf_counter() - t0
    eval_count = final.get("eval_count")
    eval_dur = final.get("eval_duration")  # ns
    decode_tps = (eval_count / (eval_dur / 1e9)) if eval_count and eval_dur else None
    ttft_ms = (t_first - t0) * 1000.0 if t_first else None
    return "".join(parts), ttft_ms, decode_tps, wall_s


def main(argv=None):
    p = argparse.ArgumentParser(prog="agentic_toolcall", description=__doc__)
    p.add_argument("--models", nargs="+", required=True, help="backend model tags to score")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--backend-url", default="http://localhost:11434")
    p.add_argument("--api", choices=["ollama"], default="ollama",
                   help="only the Ollama native transport is supported (server decode timings)")
    p.add_argument("--num-ctx", type=int, default=4096)
    p.add_argument("--num-predict", type=int, default=512,
                   help="max answer tokens; a JSON tool call is short, but leave room for a "
                        "thinking model whose reasoning precedes the JSON")
    p.add_argument("--num-gpu", type=int, default=-1,
                   help=">=0 forces options.num_gpu (Ollama GPU layers); -1 = server default")
    mm = p.add_mutually_exclusive_group()
    mm.add_argument("--use-mmap", dest="use_mmap", action="store_true", default=None)
    mm.add_argument("--no-use-mmap", dest="use_mmap", action="store_false")
    tk = p.add_mutually_exclusive_group()
    tk.add_argument("--think", dest="think", action="store_true",
                    help="send Ollama think:true (allow native reasoning)")
    tk.add_argument("--no-think", dest="think", action="store_false",
                    help="send Ollama think:false to disable native reasoning (gemma etc.)")
    p.set_defaults(think=None)
    p.add_argument("--limit", type=int, default=0, help="0 = all tasks")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--out", default="", help="write metrics JSON here")
    args = p.parse_args(argv)

    try:
        with open(args.dataset, "r", encoding="utf-8") as fh:
            spec = json.load(fh)
    except (OSError, ValueError) as exc:
        print("ERROR: cannot read dataset %s: %s" % (args.dataset, exc), file=sys.stderr)
        return 1
    tools = spec.get("tools", [])
    tasks = spec.get("tasks", [])
    if args.limit and args.limit > 0:
        tasks = tasks[: args.limit]
    if not tasks:
        print("ERROR: empty task set %s" % args.dataset, file=sys.stderr)
        return 1
    catalog_text = render_catalog(tools)

    opts = build_options(args)
    print("==> [agentic-toolcall] dataset=%s n_tasks=%d n_tools=%d think=%s opts=%s"
          % (os.path.basename(args.dataset), len(tasks), len(tools), args.think, opts))

    per_model = {}
    for model in args.models:
        n = len(tasks)
        json_valid = name_ok_n = args_ok_n = step_ok_n = 0
        walls, decodes, ttfts = [], [], []
        per_task = []
        t_model = time.perf_counter()
        for task in tasks:
            prompt = build_prompt(catalog_text, task["query"])
            err = None
            try:
                text, ttft_ms, decode_tps, wall_s = stream_ollama(
                    args.backend_url, model, prompt, opts, args.timeout, args.think)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
                text, ttft_ms, decode_tps, wall_s = "", None, None, None
                err = "%s: %s" % (type(exc).__name__, exc)
            obj, span = extract_tool_call(text)
            jv, nok, aok, adetail = score_task(task, obj)
            step_ok = jv and nok and aok
            json_valid += 1 if jv else 0
            name_ok_n += 1 if nok else 0
            args_ok_n += 1 if aok else 0
            step_ok_n += 1 if step_ok else 0
            if wall_s is not None:
                walls.append(wall_s)
            if decode_tps is not None:
                decodes.append(decode_tps)
            if ttft_ms is not None:
                ttfts.append(ttft_ms)
            per_task.append({
                "id": task["id"],
                "json_valid": jv,
                "name_correct": nok,
                "args_correct": aok,
                "step_correct": step_ok,
                "arg_detail": adetail,
                "predicted_name": (obj or {}).get("name") if isinstance(obj, dict) else None,
                "wall_s": round(wall_s, 3) if wall_s is not None else None,
                "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
                "decode_tps": round(decode_tps, 2) if decode_tps is not None else None,
                "raw_span": (span[:400] if span else (text[:400] if text else None)),
                "error": err,
            })
        per_model[model] = {
            "n": n,
            "json_valid": json_valid,
            "name_correct": name_ok_n,
            "args_correct": args_ok_n,
            "step_correct": step_ok_n,
            "json_valid_rate": round(json_valid / n, 4) if n else None,
            "name_correct_rate": round(name_ok_n / n, 4) if n else None,
            "args_correct_rate": round(args_ok_n / n, 4) if n else None,
            "step_correct_rate": round(step_ok_n / n, 4) if n else None,
            "failure_rate": round(1 - json_valid / n, 4) if n else None,
            "latency": {
                "wall_s_mean": round(statistics.fmean(walls), 3) if walls else None,
                "wall_s_median": round(statistics.median(walls), 3) if walls else None,
                "decode_tps_mean": round(statistics.fmean(decodes), 2) if decodes else None,
                "ttft_ms_mean": round(statistics.fmean(ttfts), 1) if ttfts else None,
                "ttft_ms_p95": round(_percentile(ttfts, 95), 1) if ttfts else None,
            },
            "wall_s_total": round(time.perf_counter() - t_model, 1),
            "per_task": per_task,
        }
        m = per_model[model]
        print("  %-40s step=%s json=%s name=%s args=%s  (%.0fs)"
              % (model,
                 "%.1f%%" % (100 * m["step_correct_rate"]),
                 "%.1f%%" % (100 * m["json_valid_rate"]),
                 "%.1f%%" % (100 * m["name_correct_rate"]),
                 "%.1f%%" % (100 * m["args_correct_rate"]),
                 m["wall_s_total"]))

    report = {
        "schema": "agentic-toolcall/v1",
        "dataset": os.path.basename(args.dataset),
        "n_tasks": len(tasks),
        "n_tools": len(tools),
        "backend_url": args.backend_url,
        "api": args.api,
        "shape": {"num_ctx": args.num_ctx, "num_predict": args.num_predict,
                  "num_gpu": args.num_gpu, "use_mmap": args.use_mmap, "think": args.think},
        "per_model": per_model,
    }
    out_text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out_text + "\n")
    print(out_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
