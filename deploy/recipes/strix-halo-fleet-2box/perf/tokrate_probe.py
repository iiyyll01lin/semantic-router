#!/usr/bin/env python3
"""Direct-backend token-throughput probe for the Strix Halo perf benchmarks.

Measures raw inference-server throughput by streaming a completion straight from
ONE backend (bypassing the router), so we can quantify decode/prefill tokens per
second and time-to-first-token (TTFT) with no routing/classification cost mixed
in. It speaks two dialects so the same probe covers every inference server in the
comparison (Test 2) and the co-location study (Test 1):

  * ``ollama``  -- POST /api/generate (streaming NDJSON). The final object carries
    server-side timings: eval_count / eval_duration (decode) and
    prompt_eval_count / prompt_eval_duration (prefill), both authoritative.
  * ``openai``  -- POST /v1/chat/completions (streaming SSE) with
    stream_options.include_usage. Used for llama.cpp (llama-server), Lemonade, and
    vLLM. decode tok/s is timed client-side (first-token -> last-token) because the
    OpenAI wire format has no server decode timing.

Stdlib only (urllib/json/threading) so it runs on a bare box with no pip installs,
matching the rest of this recipe. It NEVER serves, deploys, or mutates config --
it only sends requests and reports numbers.

Usage:
  # Ollama native (decode + prefill from server timings)
  python3 tokrate_probe.py --backend-url http://localhost:11434 --api ollama \
      --model qwen2.5:7b --max-tokens 128 --runs 3 --out ollama-7b.json

  # OpenAI-compatible server (llama.cpp / lemonade / vLLM)
  python3 tokrate_probe.py --backend-url http://localhost:8080/v1 --api openai \
      --model qwen2.5-7b --max-tokens 128 --runs 3 --concurrency 4

Exit code is 0 when at least one run succeeded, else 1 (so a caller can skip a
backend that never answered).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

# A deterministic filler used to grow the prompt toward a target token budget.
# Kept ASCII + generic so tokenization is stable across model families.
_FILLER = (
    "The quick brown fox jumps over the lazy dog while the engineer measures "
    "throughput and latency on a unified-memory accelerator. "
)


def build_prompt(approx_tokens: int) -> str:
    """Return a prompt of roughly ``approx_tokens`` tokens (~0.75 words/token)."""
    if approx_tokens <= 0:
        return "Write a short paragraph about semantic routing."
    target_words = max(4, int(approx_tokens * 0.75))
    words: list[str] = []
    while len(words) < target_words:
        words.extend(_FILLER.split())
    return " ".join(words[:target_words])


def _percentile(values, pct):
    """Nearest-rank percentile (stdlib only, no numpy)."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = max(0, min(len(xs) - 1, int(round((pct / 100.0) * (len(xs) - 1)))))
    return xs[k]


def _open_stream(url, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 (trusted local URL)


def run_ollama(base_url, model, prompt, max_tokens, think, timeout, num_ctx=0):
    """One streaming /api/generate call. Returns a per-run metrics dict."""
    url = base_url.rstrip("/") + "/api/generate"
    options = {"num_predict": max_tokens, "temperature": 0}
    # num_ctx>0 forces the KV-cache size: the lever the max-model sweep uses to push
    # a model's footprint PAST the VRAM carveout on purpose and observe the GTT
    # spill (the reliability boundary on unified-memory APUs). 0 = server default.
    if num_ctx and int(num_ctx) > 0:
        options["num_ctx"] = int(num_ctx)
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "think": bool(think),
        "options": options,
    }
    t0 = time.perf_counter()
    t_first = None
    final = {}
    with _open_stream(url, payload, timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            obj = json.loads(line)
            if t_first is None and obj.get("response"):
                t_first = time.perf_counter()
            if obj.get("done"):
                final = obj
                break
    t_end = time.perf_counter()

    eval_count = final.get("eval_count")
    eval_dur = final.get("eval_duration")  # nanoseconds
    prompt_count = final.get("prompt_eval_count")
    prompt_dur = final.get("prompt_eval_duration")  # nanoseconds
    decode_tps = (eval_count / (eval_dur / 1e9)) if eval_count and eval_dur else None
    prefill_tps = (
        prompt_count / (prompt_dur / 1e9) if prompt_count and prompt_dur else None
    )
    return {
        "ok": True,
        "api": "ollama",
        "decode_tps": decode_tps,
        "prefill_tps": prefill_tps,
        "ttft_ms": (t_first - t0) * 1000.0 if t_first else None,
        "completion_tokens": eval_count,
        "prompt_tokens": prompt_count,
        "wall_s": t_end - t0,
    }


def run_openai(base_url, model, prompt, max_tokens, extra_body, timeout):
    """One streaming /chat/completions call. Returns a per-run metrics dict."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if extra_body:
        payload.update(extra_body)
    t0 = time.perf_counter()
    t_first = None
    chunk_tokens = 0
    usage = {}
    with _open_stream(url, payload, timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            body = line[len("data:") :].strip()
            if body == "[DONE]":
                break
            obj = json.loads(body)
            if obj.get("usage"):
                usage = obj["usage"]
            for choice in obj.get("choices", []):
                delta = choice.get("delta", {}) or {}
                # Native thinking models (e.g. qwen3:14b through the router) stream
                # their decode work as delta.reasoning / delta.reasoning_content with
                # content="" (Ollama-style key is literally "reasoning"). Count any of
                # them as a decode token so a reasoning server is measured instead of
                # reading as zero tok/s. Non-reasoning servers still stream
                # delta.content, so this is a strict superset -- no regression.
                piece = (
                    delta.get("content")
                    or delta.get("reasoning")
                    or delta.get("reasoning_content")
                )
                if piece:
                    if t_first is None:
                        t_first = time.perf_counter()
                    chunk_tokens += 1
    t_end = time.perf_counter()

    completion_tokens = usage.get("completion_tokens") or chunk_tokens or None
    prompt_tokens = usage.get("prompt_tokens")
    # No server decode timing on the OpenAI wire: time it client-side.
    decode_window = (t_end - t_first) if t_first else None
    decode_tps = (
        completion_tokens / decode_window
        if completion_tokens and decode_window and decode_window > 0
        else None
    )
    return {
        "ok": True,
        "api": "openai",
        "decode_tps": decode_tps,
        "prefill_tps": None,  # not exposed by the OpenAI streaming format
        "ttft_ms": (t_first - t0) * 1000.0 if t_first else None,
        "completion_tokens": completion_tokens,
        "prompt_tokens": prompt_tokens,
        "wall_s": t_end - t0,
    }


def one_run(args, prompt):
    try:
        if args.api == "ollama":
            return run_ollama(
                args.backend_url, args.model, prompt, args.max_tokens, args.think,
                args.timeout, getattr(args, "num_ctx", 0)
            )
        return run_openai(
            args.backend_url, args.model, prompt, args.max_tokens, args.extra_body, args.timeout
        )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        return {"ok": False, "api": args.api, "error": "%s: %s" % (type(exc).__name__, exc)}


def aggregate(runs, wall_s, concurrency):
    ok = [r for r in runs if r.get("ok")]
    decode = [r["decode_tps"] for r in ok if r.get("decode_tps")]
    prefill = [r["prefill_tps"] for r in ok if r.get("prefill_tps")]
    ttft = [r["ttft_ms"] for r in ok if r.get("ttft_ms")]
    total_completion = sum(r.get("completion_tokens") or 0 for r in ok)
    return {
        "runs": len(runs),
        "ok_runs": len(ok),
        "success_rate": (len(ok) / len(runs)) if runs else None,
        "concurrency": concurrency,
        "decode_tps_mean": statistics.fmean(decode) if decode else None,
        "decode_tps_median": statistics.median(decode) if decode else None,
        "decode_tps_p95": _percentile(decode, 95),
        "prefill_tps_mean": statistics.fmean(prefill) if prefill else None,
        "ttft_ms_mean": statistics.fmean(ttft) if ttft else None,
        "ttft_ms_p95": _percentile(ttft, 95),
        # Aggregate decode throughput across all concurrent streams over the
        # measured wall time -- the number that actually saturates the device.
        "aggregate_decode_tps": (total_completion / wall_s) if wall_s and total_completion else None,
        "total_completion_tokens": total_completion,
        "wall_s": wall_s,
    }


def main(argv=None):
    p = argparse.ArgumentParser(prog="tokrate_probe", description=__doc__)
    p.add_argument("--backend-url", required=True, help="e.g. http://localhost:11434 or http://localhost:8080/v1")
    p.add_argument("--api", choices=["ollama", "openai", "auto"], default="auto")
    p.add_argument("--model", required=True, help="backend model tag/name")
    p.add_argument("--max-tokens", type=int, default=128, help="decode length target")
    p.add_argument("--num-ctx", type=int, default=0,
                   help="ollama: force options.num_ctx (KV-cache size); 0 = server default. "
                        "Used by maxmodel-sweep.sh to drive a footprint past the VRAM carveout.")
    p.add_argument("--prompt-tokens", type=int, default=256, help="approx prompt token budget")
    p.add_argument("--prompt", default="", help="explicit prompt (overrides --prompt-tokens)")
    p.add_argument("--runs", type=int, default=3, help="sequential batches of --concurrency streams")
    p.add_argument("--concurrency", type=int, default=1, help="parallel streams per batch")
    p.add_argument("--warmup", type=int, default=1, help="warmup runs (model load) not counted")
    p.add_argument("--think", action="store_true", help="allow thinking models to think (Ollama)")
    p.add_argument("--extra-body", default="", help="JSON merged into the OpenAI request body")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--label", default="", help="free-form label carried into the JSON")
    p.add_argument("--out", default="", help="write metrics JSON here")
    args = p.parse_args(argv)

    if args.api == "auto":
        args.api = "openai" if "/v1" in args.backend_url else "ollama"
    args.extra_body = json.loads(args.extra_body) if args.extra_body else {}
    prompt = args.prompt or build_prompt(args.prompt_tokens)

    # Warmup (loads weights into unified memory; not measured).
    for _ in range(max(0, args.warmup)):
        one_run(args, prompt)

    runs = []
    t0 = time.perf_counter()
    for _ in range(max(1, args.runs)):
        if args.concurrency <= 1:
            runs.append(one_run(args, prompt))
            continue
        batch = [None] * args.concurrency
        threads = []
        for i in range(args.concurrency):
            def _worker(idx=i):
                batch[idx] = one_run(args, prompt)
            th = threading.Thread(target=_worker)
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        runs.extend(batch)
    wall_s = time.perf_counter() - t0

    agg = aggregate(runs, wall_s, args.concurrency)
    out = {
        "schema": "tokrate-probe/v1",
        "backend_url": args.backend_url,
        "api": args.api,
        "model": args.model,
        "label": args.label,
        "shape": {
            "max_tokens": args.max_tokens,
            "prompt_tokens": args.prompt_tokens,
            "runs": args.runs,
            "concurrency": args.concurrency,
            "num_ctx": args.num_ctx,
        },
        "aggregate": agg,
        "runs_detail": runs,
    }
    text = json.dumps(out, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if agg["ok_runs"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
