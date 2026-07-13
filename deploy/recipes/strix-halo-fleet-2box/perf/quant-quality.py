#!/usr/bin/env python3
"""quant-quality.py -- score a model's multiple-choice accuracy so we can chart
OUTPUT QUALITY vs quantization for the Strix Halo quant-frontier study.

Stdlib only (urllib/json/re/argparse), matching the rest of perf/ (offline-first). It
reads a frozen, answer-keyed MCQ set (default perf/data/quant-quality-mmlu.json) and,
for each --models tag, sends every question to the backend, extracts the model's
"Answer: [letter]" choice, and scores it against the key. Output is a
route-accuracy-style JSON (overall + per-model + per-category accuracy).

It NEVER serves/deploys/mutates config -- it only sends inference requests. Transport
mirrors tokrate_probe.py; the answer regex mirrors bench/router_reason_bench.py.

Dataset schema (a JSON list):
  {"question": str, "options": [str, ...], "answer": "A".."J", "category": str}

Usage:
  # accuracy of a quant curve (small, short answers -> a few minutes each):
  python3 quant-quality.py --api ollama --backend-url http://localhost:11434 \
      --models llama3.1:70b-instruct-q4_K_M llama3.1:70b-instruct-q8_0 --limit 50 \
      --out quant-quality-halo-b.json
  # force full VRAM residency (mirrors maxmodel-sweep NUM_GPU/USE_MMAP):
  python3 quant-quality.py --models gpt-oss:120b-vram --num-gpu 999 --no-use-mmap ...
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# Mirror bench/router_reason_bench.py's extractor (tolerant of "Answer: X" / "(X)").
ANSWER_PATTERN = re.compile(r"(?:answer(?:\s+is)?:?\s*)\(?([A-J])\b", re.IGNORECASE)
_LETTERS = "ABCDEFGHIJ"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATASET = os.path.join(SCRIPT_DIR, "data", "quant-quality-mmlu.json")


def build_prompt(q):
    """Lettered MCQ prompt that constrains the model to a single-letter answer."""
    lines = [str(q["question"]).strip(), ""]
    for i, opt in enumerate(q["options"]):
        if i >= len(_LETTERS):
            break
        lines.append("%s. %s" % (_LETTERS[i], opt))
    lines.append("")
    lines.append("Answer with ONLY the letter of the correct option, in the exact "
                 "format 'Answer: X'.")
    return "\n".join(lines)


def extract_letter(text):
    if not text:
        return None
    m = ANSWER_PATTERN.search(text)
    if m:
        return m.group(1).upper()
    cands = re.findall(r"\b([A-J])\b", text)  # fallback: last standalone letter
    return cands[-1].upper() if cands else None


def _post(url, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted local URL)
        return json.loads(resp.read().decode("utf-8", "replace"))


def ask_ollama(base, model, prompt, opts, timeout):
    out = _post(
        base.rstrip("/") + "/api/generate",
        {"model": model, "prompt": prompt, "stream": False, "options": opts},
        timeout,
    )
    return out.get("response") or ""


def ask_openai(base, model, prompt, opts, timeout):
    out = _post(
        base.rstrip("/") + "/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": prompt}],
         "temperature": 0, "max_tokens": opts.get("num_predict", 16)},
        timeout,
    )
    try:
        return out["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def build_options(args):
    opts = {"temperature": 0, "num_predict": args.num_predict}
    if args.num_ctx and args.num_ctx > 0:
        opts["num_ctx"] = args.num_ctx
    if args.num_gpu is not None and args.num_gpu >= 0:
        opts["num_gpu"] = args.num_gpu
    if args.use_mmap is not None:
        opts["use_mmap"] = bool(args.use_mmap)
    return opts


def main(argv=None):
    p = argparse.ArgumentParser(prog="quant-quality", description=__doc__)
    p.add_argument("--models", nargs="+", required=True, help="backend model tags to score")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--backend-url", default="http://localhost:11434")
    p.add_argument("--api", choices=["ollama", "openai"], default="ollama")
    p.add_argument("--num-ctx", type=int, default=4096)
    p.add_argument("--num-predict", type=int, default=12, help="short: we only need the letter")
    p.add_argument("--num-gpu", type=int, default=-1,
                   help=">=0 forces options.num_gpu (Ollama GPU layers); -1 = server default")
    mm = p.add_mutually_exclusive_group()
    mm.add_argument("--use-mmap", dest="use_mmap", action="store_true", default=None)
    mm.add_argument("--no-use-mmap", dest="use_mmap", action="store_false")
    p.add_argument("--limit", type=int, default=0, help="0 = all questions")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--out", default="", help="write metrics JSON here")
    args = p.parse_args(argv)

    try:
        with open(args.dataset, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print("ERROR: cannot read dataset %s: %s" % (args.dataset, exc), file=sys.stderr)
        return 1
    if args.limit and args.limit > 0:
        data = data[: args.limit]
    if not data:
        print("ERROR: empty dataset %s" % args.dataset, file=sys.stderr)
        return 1

    opts = build_options(args)
    print("==> [quant-quality] dataset=%s n=%d api=%s opts=%s"
          % (os.path.basename(args.dataset), len(data), args.api, opts))

    per_model = {}
    for model in args.models:
        correct = 0
        cats = {}
        t0 = time.perf_counter()
        for q in data:
            prompt = build_prompt(q)
            try:
                text = (ask_ollama if args.api == "ollama" else ask_openai)(
                    args.backend_url, model, prompt, opts, args.timeout)
                pred = extract_letter(text)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
                pred = None
            gold = str(q.get("answer", "")).strip().upper()[:1]
            ok = pred is not None and pred == gold
            correct += 1 if ok else 0
            c = cats.setdefault(q.get("category", "?"), {"n": 0, "correct": 0})
            c["n"] += 1
            c["correct"] += 1 if ok else 0
        n = len(data)
        per_model[model] = {
            "n": n,
            "correct": correct,
            "accuracy": round(correct / n, 4) if n else None,
            "wall_s": round(time.perf_counter() - t0, 1),
            "per_category": {
                k: {"n": v["n"], "correct": v["correct"],
                    "accuracy": round(v["correct"] / v["n"], 4) if v["n"] else None}
                for k, v in sorted(cats.items())
            },
            "options": opts,
        }
        acc = per_model[model]["accuracy"]
        print("  %-40s accuracy=%s  (%d/%d, %.0fs)"
              % (model, "n/a" if acc is None else "%.1f%%" % (100 * acc),
                 correct, n, per_model[model]["wall_s"]))

    report = {
        "schema": "quant-quality/v1",
        "dataset": os.path.basename(args.dataset),
        "dataset_n": len(data),
        "backend_url": args.backend_url,
        "api": args.api,
        "shape": {"num_ctx": args.num_ctx, "num_predict": args.num_predict,
                  "num_gpu": args.num_gpu, "use_mmap": args.use_mmap},
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
