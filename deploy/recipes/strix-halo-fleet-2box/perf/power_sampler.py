#!/usr/bin/env python3
"""Socket-power / perf-per-watt sampler for the Strix Halo perf benchmarks.

Strix Halo is a unified-memory APU with no discrete-GPU power rail, so the
meaningful energy number is the **socket graphics-package power** that `rocm-smi
--showpower` exposes (verified ~12 W idle, ~100-120 W under sustained decode).
This probe samples that rail ~1 Hz while driving a sustained decode against an
Ollama model, then reports idle W, mean/peak load W, decode tok/s, and the two
efficiency figures that matter:

  * ``tok_per_watt_load``      -- decode tok/s / mean load W (absolute).
  * ``tok_per_watt_net_idle``  -- decode tok/s / (load W - idle W) (dynamic).

It speaks two backend dialects (same as `tokrate_probe.py`) so the perf-per-watt
figure covers every inference server in the comparison, not just Ollama:

  * ``--api ollama`` (default) -- POST /api/generate; decode rate comes from the
    server-side ``eval_count`` / ``eval_duration`` (authoritative, no client-side
    timing noise). Existing behavior, byte-for-byte unchanged.
  * ``--api openai``  -- POST {backend-url}/chat/completions (streaming SSE) for
    llama.cpp (llama-server), Lemonade, vLLM. decode tok/s = completion tokens /
    decode-duration, timed client-side EXCLUDING TTFT (the OpenAI wire format has
    no server decode timing), preferring ``usage.completion_tokens`` when the
    server reports it, else counting streamed chunks. ``--no-mmap`` is an
    Ollama-only option and is ignored gracefully in openai mode.

Stdlib only (urllib/json/subprocess/threading/argparse) so it runs on a bare box
with no pip installs, matching the rest of this recipe. It NEVER serves, deploys,
or mutates config -- it only reads power and sends requests.

Notes for the 120B MoE on the 64 GiB-carveout box (Halo-B):
  * pass ``--no-mmap`` -- with mmap the ~68 GB load + CPU tensor overrides never
    finish inside client timeouts ("aborting load"); no-mmap loads in ~31 s.
  * leave ``--num-ctx 0`` (server default) -- pinning num_ctx also stalled the load.

Usage:
  # Halo-A dense models
  python3 power_sampler.py --model qwen2.5:7b  --max-tokens 128 --out pw-7b.json
  python3 power_sampler.py --model qwen2.5:32b --max-tokens 128 --out pw-32b.json

  # Halo-B 120B MoE (VRAM-resident, no-mmap; one long sustained run)
  python3 power_sampler.py --model gpt-oss:120b --no-mmap --runs 1 \
      --max-tokens 1400 --keep-alive 30m --out pw-120b.json

  # llama.cpp / any OpenAI-compatible server (llama-server ROCm)
  python3 power_sampler.py --api openai --backend-url http://localhost:8080/v1 \
      --model gpt-oss-120b --max-tokens 1400 --runs 1 --out pw-120b-llamacpp.json

  # "thinking" model (Gemma): the default filler prompt makes it EOS after a few
  # tokens (0.2 s window, 1 power sample -> junk watts). Keep think:false (default)
  # and pass a GENERATIVE --prompt-text so it decodes a long, sustained essay:
  python3 power_sampler.py --model gemma4:31b-vram --no-mmap --runs 1 \
      --max-tokens 768 --keep-alive 30m \
      --prompt-text "Write a detailed 1000-word explanation of how transformers work." \
      --out pw-gemma4_31b.json   # -> eval_count ~768, load_samples well above 5

Exit code is 0 when a decode rate AND power were captured, else 1 (so a caller
can skip a box/model that never answered or a box with no power meter).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# Optional per-request wall-clock deadline (seconds) from the environment, so a
# caller can bound a pathologically slow CPU-offload decode that trickles tokens
# for far longer than any usable measurement (the sustained phase would otherwise
# stream ~1400 tokens for hours without ever tripping the per-read socket timeout).
# 0/unset = disabled (default => unchanged behavior). On breach the generate raises
# TimeoutError, which the phase-2/3 handlers already treat as "warmup/run failed"
# and record a null power block -- exactly what an unusable cell deserves.
_CLIENT_DEADLINE_S = float(os.environ.get("POWER_DEADLINE", "0") or 0)

# rocm-smi prints e.g. "Current Socket Graphics Package Power (W): 109.0".
_POWER_RE = re.compile(r"Power \(W\):\s*([0-9.]+)")

# Deterministic ASCII filler (mirrors tokrate_probe.py) to grow the prompt.
_FILLER = (
    "The quick brown fox jumps over the lazy dog while the engineer measures "
    "throughput and latency on a unified-memory accelerator. "
)


def build_prompt(approx_tokens: int) -> str:
    """Return a prompt of roughly ``approx_tokens`` tokens (~0.75 words/token)."""
    target_words = max(4, int(approx_tokens * 0.75))
    words: list[str] = []
    while len(words) < target_words:
        words.extend(_FILLER.split())
    return " ".join(words[:target_words])


def read_socket_power():
    """Return socket graphics-package power in watts (float) or None.

    Prefers a line that mentions "Socket"; otherwise the first "Power (W):" line.
    Degrades to None when rocm-smi is absent so a caller can note "no meter".
    """
    try:
        out = (
            subprocess.run(
                ["rocm-smi", "--showpower"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            ).stdout
            or ""
        )
    except (OSError, subprocess.SubprocessError):
        return None
    best = None
    for line in out.splitlines():
        m = _POWER_RE.search(line)
        if not m:
            continue
        val = float(m.group(1))
        if "Socket" in line:
            return val
        if best is None:
            best = val
    return best


def sample_power(secs, interval=1.0):
    """Block for ``secs`` seconds sampling socket power; return a list of watts."""
    vals = []
    end = time.time() + secs
    while time.time() < end:
        w = read_socket_power()
        if w is not None:
            vals.append(w)
        time.sleep(interval)
    return vals


def ollama_generate(
    base_url,
    model,
    prompt,
    num_predict,
    think=False,
    num_ctx=0,
    use_mmap=None,
    keep_alive=None,
    timeout=1800,
):
    """One streaming /api/generate call. Returns (eval_count, eval_ns, wall_s)."""
    url = base_url.rstrip("/") + "/api/generate"
    options = {"num_predict": num_predict, "temperature": 0}
    if num_ctx and int(num_ctx) > 0:
        options["num_ctx"] = int(num_ctx)
    if use_mmap is not None:
        options["use_mmap"] = bool(use_mmap)
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "think": bool(think),
        "options": options,
    }
    if keep_alive:
        payload["keep_alive"] = keep_alive
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    if _CLIENT_DEADLINE_S:
        timeout = min(timeout, _CLIENT_DEADLINE_S)
    t0 = time.perf_counter()
    final = {}
    with urllib.request.urlopen(
        req, timeout=timeout
    ) as resp:  # noqa: S310 (trusted local URL)
        for raw in resp:
            if _CLIENT_DEADLINE_S and (time.perf_counter() - t0) > _CLIENT_DEADLINE_S:
                raise TimeoutError(
                    "client deadline %.0fs exceeded" % _CLIENT_DEADLINE_S
                )
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("done"):
                final = obj
                break
    wall = time.perf_counter() - t0
    return final.get("eval_count"), final.get("eval_duration"), wall


def openai_generate(base_url, model, prompt, max_tokens, timeout=1800):
    """One streaming /chat/completions call (OpenAI dialect).

    Returns (completion_tokens, decode_s, wall_s) where ``decode_s`` is the
    client-timed decode window EXCLUDING time-to-first-token, so decode tok/s =
    completion_tokens / decode_s measures steady-state generation only. Token
    count prefers server-reported ``usage.completion_tokens`` (stream_options
    include_usage), else falls back to counting streamed content chunks -- the
    same approach as tokrate_probe.py for cross-tool consistency.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    if _CLIENT_DEADLINE_S:
        timeout = min(timeout, _CLIENT_DEADLINE_S)
    t0 = time.perf_counter()
    t_first = None
    chunk_tokens = 0
    usage = {}
    with urllib.request.urlopen(
        req, timeout=timeout
    ) as resp:  # noqa: S310 (trusted local URL)
        for raw in resp:
            if _CLIENT_DEADLINE_S and (time.perf_counter() - t0) > _CLIENT_DEADLINE_S:
                raise TimeoutError(
                    "client deadline %.0fs exceeded" % _CLIENT_DEADLINE_S
                )
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
                # Count content OR reasoning deltas so a native-thinking server is
                # measured instead of reading as zero tok/s (mirrors tokrate_probe).
                piece = (
                    delta.get("content")
                    or delta.get("reasoning")
                    or delta.get("reasoning_content")
                )
                if piece:
                    if t_first is None:
                        t_first = time.perf_counter()
                    chunk_tokens += 1
    wall = time.perf_counter() - t0
    completion_tokens = usage.get("completion_tokens") or chunk_tokens or None
    decode_s = (time.perf_counter() - t_first) if t_first else None
    return completion_tokens, decode_s, wall


def decode_once(
    api,
    backend_url,
    model,
    prompt,
    num_predict,
    think=False,
    num_ctx=0,
    use_mmap=None,
    keep_alive=None,
):
    """Drive one decode via the selected dialect; return a unified run dict.

    Keys: tokens, decode_s, wall_s, decode_tps (all dialects). The Ollama path
    additionally keeps eval_count / eval_duration_ns for backward compatibility
    with existing consumers of runs_detail.
    """
    if api == "openai":
        toks, decode_s, wall = openai_generate(backend_url, model, prompt, num_predict)
        tps = (toks / decode_s) if (toks and decode_s and decode_s > 0) else None
        return {
            "tokens": toks,
            "decode_s": decode_s,
            "wall_s": wall,
            "decode_tps": tps,
            "completion_tokens": toks,
        }
    ec, ed, wall = ollama_generate(
        backend_url, model, prompt, num_predict, think, num_ctx, use_mmap, keep_alive
    )
    decode_s = (ed / 1e9) if ed else None
    tps = (ec / decode_s) if (ec and decode_s) else None
    return {
        "tokens": ec,
        "decode_s": decode_s,
        "wall_s": wall,
        "decode_tps": tps,
        "eval_count": ec,
        "eval_duration_ns": ed,
    }


def summarize(idle_w, load_w, decode_tps_vals):
    """Roll idle/load power + decode rates into the perf-per-watt figures."""
    idle_mean = statistics.fmean(idle_w) if idle_w else None
    load_mean = statistics.fmean(load_w) if load_w else None
    load_max = max(load_w) if load_w else None
    decode_median = statistics.median(decode_tps_vals) if decode_tps_vals else None
    tpw_load = (decode_median / load_mean) if (decode_median and load_mean) else None
    tpw_net = (
        decode_median / (load_mean - idle_mean)
        if (decode_median and load_mean and idle_mean and load_mean > idle_mean)
        else None
    )
    return {
        # Canonical keys consumed by bestcfg-matrix.sh (schema contract).
        "idle_w": idle_mean,
        "load_w_mean": load_mean,
        "load_w_peak": load_max,
        "decode_tps": decode_median,
        "tok_per_watt_load": tpw_load,
        "tok_per_watt_net_idle": tpw_net,
        # Original keys kept for backward compatibility with existing consumers.
        "idle_w_mean": idle_mean,
        "idle_samples": len(idle_w),
        "load_w_max": load_max,
        "load_samples": len(load_w),
        "decode_tps_median": decode_median,
        "decode_tps_runs": decode_tps_vals,
    }


def main(argv=None):
    p = argparse.ArgumentParser(prog="power_sampler", description=__doc__)
    p.add_argument(
        "--api",
        choices=["ollama", "openai"],
        default="ollama",
        help="backend dialect: ollama (/api/generate, default) or "
        "openai ({backend-url}/chat/completions for llama.cpp/vLLM)",
    )
    p.add_argument(
        "--backend-url",
        "--base-url",
        dest="backend_url",
        default="http://localhost:11434",
        help="backend base URL (default localhost:11434 for ollama; pass e.g. "
        "http://localhost:8080/v1 for an OpenAI-compatible server). "
        "--base-url is a backward-compatible alias.",
    )
    p.add_argument(
        "--model", required=True, help="backend model tag/name, e.g. qwen2.5:7b"
    )
    p.add_argument("--idle-secs", type=int, default=15, help="idle power window (s)")
    p.add_argument("--max-tokens", type=int, default=600, help="decode length per run")
    p.add_argument("--runs", type=int, default=3, help="sustained decode runs")
    p.add_argument(
        "--prompt-tokens", type=int, default=256, help="approx prompt token budget"
    )
    p.add_argument(
        "--prompt-text",
        default="",
        help="use this EXACT prompt instead of the synthetic filler. Give a "
        "GENERATIVE instruction (e.g. 'Write a detailed 1000-word "
        "explanation of how transformers work.') so a 'thinking' model "
        "(gemma) keeps decoding to num_predict instead of hitting EOS "
        "after a few tokens -- required for a stable multi-sample "
        "wattage. Empty = filler prompt (default, unchanged).",
    )
    p.add_argument(
        "--num-ctx",
        type=int,
        default=0,
        help="ollama options.num_ctx; 0 = server default (recommended for 120B)",
    )
    p.add_argument(
        "--no-mmap",
        action="store_true",
        help="ollama-only: send options.use_mmap=false (much faster load for 120B w/ "
        "CPU tensor overrides). Ignored gracefully in --api openai mode.",
    )
    p.add_argument(
        "--keep-alive", default="30m", help="ollama keep_alive for the model"
    )
    p.add_argument(
        "--think", action="store_true", help="allow reasoning models to think"
    )
    p.add_argument(
        "--sample-interval", type=float, default=1.0, help="power sample period (s)"
    )
    p.add_argument("--out", default="", help="write metrics JSON here")
    args = p.parse_args(argv)

    # --no-mmap is an Ollama load knob; it has no meaning for an OpenAI-compatible
    # server, so drop it silently in openai mode rather than erroring.
    use_mmap = False if (args.no_mmap and args.api == "ollama") else None
    # A custom generative prompt (--prompt-text) keeps a thinking model decoding to
    # num_predict; otherwise fall back to the synthetic filler (default, unchanged).
    prompt = args.prompt_text.strip() or build_prompt(args.prompt_tokens)

    print("== phase 1: idle power (%ds) ==" % args.idle_secs, flush=True)
    idle_w = sample_power(args.idle_secs, args.sample_interval)
    print(
        "   idle samples=%d mean=%.1f W"
        % (len(idle_w), statistics.fmean(idle_w) if idle_w else -1),
        flush=True,
    )

    print("== phase 2: warmup (load %s via %s) ==" % (args.model, args.api), flush=True)
    t_warm = time.perf_counter()
    try:
        warm = decode_once(
            args.api,
            args.backend_url,
            args.model,
            prompt,
            16,
            args.think,
            args.num_ctx,
            use_mmap,
            args.keep_alive,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        print("   warmup failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    print(
        "   warmup done in %.1fs (tokens=%s)"
        % (time.perf_counter() - t_warm, warm.get("tokens")),
        flush=True,
    )

    print("== phase 3: sustained decode + power sampling ==", flush=True)
    load = []
    stop = {"go": True}

    def _sampler():
        while stop["go"]:
            w = read_socket_power()
            if w is not None:
                load.append(w)
            time.sleep(args.sample_interval)

    th = threading.Thread(target=_sampler, daemon=True)
    th.start()

    runs = []
    t_start = time.time()
    for i in range(max(1, args.runs)):
        try:
            r = decode_once(
                args.api,
                args.backend_url,
                args.model,
                prompt,
                args.max_tokens,
                args.think,
                args.num_ctx,
                use_mmap,
                args.keep_alive,
            )
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            OSError,
            ValueError,
        ) as exc:
            print(
                "   run %d failed: %s: %s" % (i + 1, type(exc).__name__, exc),
                file=sys.stderr,
            )
            continue
        runs.append(r)
        print(
            "   run %d: tokens=%s decode_tps=%.2f wall=%.1fs"
            % (
                i + 1,
                r.get("tokens"),
                r.get("decode_tps") or -1,
                r.get("wall_s") or -1,
            ),
            flush=True,
        )
    decode_window_s = time.time() - t_start
    stop["go"] = False
    th.join(timeout=3)

    decode_tps_vals = [r["decode_tps"] for r in runs if r["decode_tps"]]
    summ = summarize(idle_w, load, decode_tps_vals)
    total_tokens = sum((r.get("tokens") or 0) for r in runs)
    total_decode_s = sum((r.get("decode_s") or 0) for r in runs)

    out = {
        "schema": "power-sampler/v1",
        "api": args.api,
        "backend_url": args.backend_url,
        "model": args.model,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_decode_tokens": total_tokens,
        "total_decode_s": total_decode_s,
        "decode_window_s": decode_window_s,
        "shape": {
            "max_tokens": args.max_tokens,
            "runs": args.runs,
            "prompt_tokens": args.prompt_tokens,
            "num_ctx": args.num_ctx,
            "think": args.think,
            "prompt_text": (args.prompt_text.strip() or None),
            # use_mmap only applies to the ollama load path; "n/a" for openai.
            "use_mmap": (
                "n/a"
                if args.api == "openai"
                else (False if args.no_mmap else "default")
            ),
            "keep_alive": args.keep_alive,
        },
        "runs_detail": runs,
    }
    out.update(summ)
    text = json.dumps(out, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    print(
        "\n== SUMMARY: api=%s idle=%.1fW load=%.1fW tok/s=%.2f tok/s-per-W=%.4f =="
        % (
            args.api,
            summ["idle_w"] or -1,
            summ["load_w_mean"] or -1,
            summ["decode_tps"] or -1,
            summ["tok_per_watt_load"] or -1,
        )
    )
    # Contract: exit 0 only when BOTH a decode rate AND power were captured.
    return 0 if (summ["decode_tps"] and summ["load_w_mean"]) else 1


if __name__ == "__main__":
    sys.exit(main())
