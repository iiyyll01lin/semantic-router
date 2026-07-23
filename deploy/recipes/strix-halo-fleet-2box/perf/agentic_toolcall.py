#!/usr/bin/env python3
"""Score structured-JSON or native OpenAI tool calls for the Strix Halo profile.

The stdlib-only probe reads an answer-keyed task set and presents the same tool
catalog on every request. Legacy Ollama behavior remains prompt-driven through
``/api/generate``. OpenAI-compatible servers can use either that prompt contract or
native ``tools`` / ``tool_choice`` through streaming ``/chat/completions``.

The report retains server prompt/decode counts and durations from Ollama, OpenAI
prompt/cached-token usage, and client-streamed TTFT. ``--warm-runs`` repeats each
identical request and reports cold/warm groups without changing the historical
default of one request per task. Tasks may expect no call, one call, or parallel
calls. Scoring includes:

  * json_valid   -- a JSON object with a "name" key was recovered from the response
  * name_correct -- the selected tool matches the expected tool
  * args_correct -- every expected argument passes its check (extra keys are allowed)
  * step_correct -- json_valid AND name_correct AND args_correct
  * failure_rate -- 1 - json_valid_rate (responses without a structured verdict)

Argument checks (per expected key):
  equals        string equal, case-insensitive, trimmed
  contains      expected substring present, case-insensitive
  contains_all  every expected substring present (value is a list), case-insensitive
  equals_number numeric equality after coercing "600"/"600.0"/600 -> float

It NEVER serves/deploys/mutates config -- it only sends inference requests. Transport
and the --no-think / --num-gpu / --no-use-mmap forced-residency flags mirror
quant-quality.py so a thinking model (Gemma) answers with the JSON directly and a
big model stays VRAM-resident on the 96 GiB carveout.

Examples:
  python3 agentic_toolcall.py --api ollama --backend-url http://localhost:11434 \
      --models gemma4:26b-a4b-it-q8_0 qwen3-coder:30b \
      --no-think --num-predict 512 --num-gpu 999 --no-use-mmap \
      --out agentic-gemma-vs-qwen.json

  python3 agentic_toolcall.py --api openai --tool-mode native \
      --backend-url http://localhost:8000/v1 --models auto --warm-runs 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error

from agentic_toolcall_eval import (
    aggregate_trials,
    build_trial_record,
    extract_tool_call,
    extract_tool_calls,
    normalize_tool_calls,
    score_task,
    score_task_calls,
)
from agentic_toolcall_support import (
    openai_tools,
    parameter_schema,
    stream_ollama,
    stream_openai,
)

__all__ = [
    "aggregate_trials",
    "build_trial_record",
    "extract_tool_call",
    "extract_tool_calls",
    "normalize_tool_calls",
    "score_task",
    "score_task_calls",
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATASET = os.path.join(SCRIPT_DIR, "data", "agentic-toolcall-tasks.json")

SYSTEM_PREAMBLE = (
    "You are a function-calling assistant. Select only tools needed for the user's "
    "request. For one call, return ONLY "
    '{"name":"<tool_name>","arguments":{...}}. For parallel calls, return ONLY a '
    "JSON array of those objects. If no tool should run, return ONLY "
    '{"name":"none","arguments":{}}. Use exact tool and argument names. Do not '
    "include prose, comments, or markdown fences."
)


def build_prompt(catalog_text, query):
    return (
        f"{SYSTEM_PREAMBLE}\n\nAvailable tools:\n{catalog_text}\n\n"
        f"User request: {query}\n\nJSON tool call:"
    )


def render_catalog(tools):
    lines = []
    for t in tools:
        schema = parameter_schema(t.get("parameters"))
        properties = schema.get("properties") or {}
        params = ", ".join(
            f"{name} ({(prop or {}).get('type', 'any')})"
            for name, prop in properties.items()
        )
        lines.append(
            f"- {t['name']}: {t.get('description', '')} | parameters: {params}"
        )
    return "\n".join(lines)


def build_options(args):
    opts = {"temperature": 0, "num_predict": args.num_predict}
    if args.num_ctx and args.num_ctx > 0:
        opts["num_ctx"] = args.num_ctx
    if args.num_gpu is not None and args.num_gpu >= 0:
        opts["num_gpu"] = args.num_gpu
    if args.use_mmap is not None:
        opts["use_mmap"] = bool(args.use_mmap)
    return opts


def run_probe_request(args, model, task, prompt, native_catalog, opts):
    if args.api == "ollama":
        return stream_ollama(
            args.backend_url,
            model,
            prompt,
            opts,
            args.timeout,
            args.think,
        )
    if args.tool_mode == "native":
        messages = [
            {
                "role": "system",
                "content": (
                    "Use native tools when needed. Do not invent tool results, and "
                    "do not call a tool when the request requires no action."
                ),
            },
            {"role": "user", "content": task["query"]},
        ]
        return stream_openai(
            args.backend_url,
            model,
            messages,
            args.num_predict,
            args.timeout,
            api_key=args.api_key,
            tools=native_catalog,
            tool_choice=args.tool_choice,
        )
    return stream_openai(
        args.backend_url,
        model,
        [{"role": "user", "content": prompt}],
        args.num_predict,
        args.timeout,
        api_key=args.api_key,
    )


def build_parser():
    p = argparse.ArgumentParser(prog="agentic_toolcall", description=__doc__)
    p.add_argument(
        "--models", nargs="+", required=True, help="backend model tags to score"
    )
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--backend-url", default="http://localhost:11434")
    p.add_argument(
        "--api",
        choices=["ollama", "openai"],
        default="ollama",
        help="Ollama native generate or OpenAI-compatible chat completions",
    )
    p.add_argument("--api-key", default="")
    p.add_argument(
        "--tool-mode",
        choices=["prompt", "native"],
        default="prompt",
        help="prompt preserves legacy JSON mode; native sends OpenAI tool schemas",
    )
    p.add_argument(
        "--tool-choice",
        choices=["auto", "required", "none"],
        default="auto",
        help="OpenAI tool_choice used only with --tool-mode native",
    )
    p.add_argument(
        "--warm-runs",
        type=int,
        default=0,
        help=(
            "repeat each identical request this many times after its cold exposure; "
            "0 preserves the historical one-request-per-task behavior"
        ),
    )
    p.add_argument("--num-ctx", type=int, default=4096)
    p.add_argument(
        "--num-predict",
        type=int,
        default=512,
        help="max answer tokens; a JSON tool call is short, but leave room for a "
        "thinking model whose reasoning precedes the JSON",
    )
    p.add_argument(
        "--num-gpu",
        type=int,
        default=-1,
        help=">=0 forces options.num_gpu (Ollama GPU layers); -1 = server default",
    )
    mm = p.add_mutually_exclusive_group()
    mm.add_argument("--use-mmap", dest="use_mmap", action="store_true", default=None)
    mm.add_argument("--no-use-mmap", dest="use_mmap", action="store_false")
    tk = p.add_mutually_exclusive_group()
    tk.add_argument(
        "--think",
        dest="think",
        action="store_true",
        help="send Ollama think:true (allow native reasoning)",
    )
    tk.add_argument(
        "--no-think",
        dest="think",
        action="store_false",
        help="send Ollama think:false to disable native reasoning (gemma etc.)",
    )
    p.set_defaults(think=None)
    p.add_argument("--limit", type=int, default=0, help="0 = all tasks")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--out", default="", help="write metrics JSON here")
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if args.tool_mode == "native" and args.api != "openai":
        p.error("--tool-mode native requires --api openai")
    if args.warm_runs < 0:
        p.error("--warm-runs must be >= 0")

    try:
        with open(args.dataset, encoding="utf-8") as fh:
            spec = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read dataset {args.dataset}: {exc}", file=sys.stderr)
        return 1
    tools = spec.get("tools", [])
    tasks = spec.get("tasks", [])
    if args.limit and args.limit > 0:
        tasks = tasks[: args.limit]
    if not tasks:
        print(f"ERROR: empty task set {args.dataset}", file=sys.stderr)
        return 1
    catalog_text = render_catalog(tools)
    native_catalog = openai_tools(tools)

    opts = build_options(args)
    print(
        f"==> [agentic-toolcall] dataset={os.path.basename(args.dataset)} "
        f"n_tasks={len(tasks)} n_tools={len(tools)} api={args.api} "
        f"tool_mode={args.tool_mode} warm_runs={args.warm_runs} "
        f"think={args.think} opts={opts}"
    )

    per_model = {}
    for model in args.models:
        t_model = time.perf_counter()
        trials = []
        for task in tasks:
            prompt = build_prompt(catalog_text, task["query"])
            for repeat_index in range(1 + args.warm_runs):
                cache_state = "cold" if repeat_index == 0 else "warm"
                error = None
                metrics = {}
                try:
                    metrics = run_probe_request(
                        args,
                        model,
                        task,
                        prompt,
                        native_catalog,
                        opts,
                    )
                except (
                    urllib.error.URLError,
                    urllib.error.HTTPError,
                    OSError,
                    ValueError,
                ) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                trials.append(
                    build_trial_record(
                        task,
                        metrics,
                        cache_state,
                        repeat_index,
                        args.tool_mode,
                        error,
                    )
                )
        model_summary = aggregate_trials(trials)
        model_summary.update(
            {
                "n_tasks": len(tasks),
                "wall_s_total": round(time.perf_counter() - t_model, 1),
                "per_task": trials,
            }
        )
        per_model[model] = model_summary
        m = per_model[model]
        print(
            f"  {model:<40} step={100 * m['step_correct_rate']:.1f}% "
            f"json={100 * m['json_valid_rate']:.1f}% "
            f"name={100 * m['name_correct_rate']:.1f}% "
            f"args={100 * m['args_correct_rate']:.1f}% "
            f"({m['wall_s_total']:.0f}s)"
        )

    report = {
        "schema": "agentic-toolcall/v2",
        "dataset": os.path.basename(args.dataset),
        "n_tasks": len(tasks),
        "n_tools": len(tools),
        "n_requests_per_model": len(tasks) * (1 + args.warm_runs),
        "backend_url": args.backend_url,
        "api": args.api,
        "shape": {
            "num_ctx": args.num_ctx,
            "num_predict": args.num_predict,
            "num_gpu": args.num_gpu,
            "use_mmap": args.use_mmap,
            "think": args.think,
            "tool_mode": args.tool_mode,
            "tool_choice": args.tool_choice,
            "warm_runs": args.warm_runs,
        },
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
