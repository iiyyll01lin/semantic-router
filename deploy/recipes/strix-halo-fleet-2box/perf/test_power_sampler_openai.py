#!/usr/bin/env python3
"""Offline, hardware-free self-test for power_sampler.py's dialects.

Proves the OpenAI-compatible (llama.cpp / vLLM) perf-per-watt path and the
unchanged Ollama path WITHOUT any ROCm/GPU/live backend:

  * a localhost http.server stands in for the inference server, serving a canned
    OpenAI SSE stream (with and without server usage) and an Ollama NDJSON stream;
  * ``read_socket_power`` is monkeypatched (12 W idle -> 110 W under load) so the
    rocm-smi rail is simulated, not faked into the JSON.

It exercises the REAL decode + summarize + JSON code paths and asserts:
  1. openai: exit 0, api/model set, decode_tps from usage.completion_tokens,
     full schema (idle_w/load_w_mean/load_w_peak/decode_tps/tok_per_watt_*).
  2. openai without server usage: token count falls back to streamed chunks.
  3. openai --no-mmap is ignored gracefully (shape.use_mmap == "n/a", exit 0).
  4. ollama: unchanged -- exit 0, decode_tps from eval_count/eval_duration, and
     the original back-compat keys (idle_w_mean/load_w_max/decode_tps_median).
  5. unreachable backend -> exit 1 (no decode rate captured).

Usage:
  python3 test_power_sampler_openai.py    # prints "N/N checks passed", exit 0/1

No real hardware numbers are produced; power is a simulated constant.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import power_sampler  # noqa: E402

_PASS = 0
_FAIL = []


def check(name, cond):
    global _PASS
    if cond:
        _PASS += 1
        print("[PASS]", name)
    else:
        _FAIL.append(name)
        print("[FAIL]", name)


class _MockBackend(http.server.BaseHTTPRequestHandler):
    """Canned OpenAI SSE + Ollama NDJSON so decode_once has a real wire to read."""

    def log_message(self, *_a):  # keep test output clean
        pass

    def do_POST(self):  # noqa: N802 (http.server API)
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.endswith("/api/generate"):
            # eval_count/eval_duration -> 30 tok / 1.0 s => decode_tps == 30.
            lines = [{"response": "hi", "done": False}] * 3 + [
                {
                    "done": True,
                    "eval_count": 30,
                    "eval_duration": 1_000_000_000,
                    "prompt_eval_count": 40,
                    "prompt_eval_duration": 500_000_000,
                },
            ]
            body = "".join(json.dumps(o) + "\n" for o in lines).encode()
            ctype = "application/x-ndjson"
        elif self.path.startswith("/nousage"):
            # 15 content chunks, NO usage chunk -> fallback counts chunks == 15.
            evs = [{"choices": [{"delta": {"content": "hi "}}]} for _ in range(15)]
            body = (
                "".join("data: " + json.dumps(e) + "\n\n" for e in evs)
                + "data: [DONE]\n\n"
            ).encode()
            ctype = "text/event-stream"
        else:  # /chat/completions with server usage
            # 20 content chunks but usage says 25 -> decode uses the usage number.
            evs = [{"choices": [{"delta": {"content": "hi "}}]} for _ in range(20)]
            evs.append(
                {
                    "choices": [{"delta": {}}],
                    "usage": {"completion_tokens": 25, "prompt_tokens": 10},
                }
            )
            body = (
                "".join("data: " + json.dumps(e) + "\n\n" for e in evs)
                + "data: [DONE]\n\n"
            ).encode()
            ctype = "text/event-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_backend():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockBackend)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# --- Simulated rocm-smi rail: 12 W idle, 110 W once a decode has started. ---
_STATE = {"load": False}
_REAL_OAI = power_sampler.openai_generate
_REAL_OLL = power_sampler.ollama_generate


def _wrap(fn):
    def inner(*a, **k):
        _STATE["load"] = True  # a decode is in flight -> report load power
        return fn(*a, **k)

    return inner


def _install_power_mock():
    power_sampler.openai_generate = _wrap(_REAL_OAI)
    power_sampler.ollama_generate = _wrap(_REAL_OLL)
    power_sampler.read_socket_power = lambda: 110.0 if _STATE["load"] else 12.0


_FAST = [
    "--idle-secs",
    "1",
    "--sample-interval",
    "0.1",
    "--runs",
    "1",
    "--max-tokens",
    "32",
]

_SCHEMA_KEYS = [
    "idle_w",
    "load_w_mean",
    "load_w_peak",
    "decode_tps",
    "tok_per_watt_load",
    "tok_per_watt_net_idle",
    "api",
    "model",
]


def _run(tmp, name, argv):
    _STATE["load"] = False  # reset to idle before each run
    path = os.path.join(tmp, name)
    rc = power_sampler.main(argv + ["--out", path])
    data = json.load(open(path, encoding="utf-8"))
    return rc, data


def main():
    tmp = tempfile.mkdtemp(prefix="pw-verify-")
    _install_power_mock()
    srv, base = _start_backend()
    try:
        rc, d = _run(
            tmp,
            "oai.json",
            ["--api", "openai", "--backend-url", base, "--model", "m"] + _FAST,
        )
        check(
            "1. openai exit 0 + schema + api/model + decode_tps from usage(25)",
            rc == 0
            and all(k in d for k in _SCHEMA_KEYS)
            and d["api"] == "openai"
            and d["model"] == "m"
            and d["runs_detail"][0]["tokens"] == 25
            and (d["decode_tps"] or 0) > 0
            and (d["tok_per_watt_load"] or 0) > 0
            and (d["tok_per_watt_net_idle"] or 0) > 0
            and abs(d["idle_w"] - 12.0) < 1e-6
            and abs(d["load_w_mean"] - 110.0) < 1e-6,
        )

        rc, d = _run(
            tmp,
            "oai_nousage.json",
            ["--api", "openai", "--backend-url", base + "/nousage", "--model", "m"]
            + _FAST,
        )
        check(
            "2. openai no server usage -> token count falls back to chunks(15)",
            rc == 0
            and d["runs_detail"][0]["tokens"] == 15
            and (d["decode_tps"] or 0) > 0,
        )

        rc, d = _run(
            tmp,
            "oai_nommap.json",
            ["--api", "openai", "--backend-url", base, "--model", "m", "--no-mmap"]
            + _FAST,
        )
        check(
            "3. openai --no-mmap ignored gracefully (shape.use_mmap == n/a, exit 0)",
            rc == 0 and d["shape"]["use_mmap"] == "n/a",
        )

        rc, d = _run(
            tmp,
            "oll.json",
            ["--api", "ollama", "--backend-url", base, "--model", "m"] + _FAST,
        )
        check(
            "4. ollama unchanged -> exit 0, decode_tps==30, back-compat keys present",
            rc == 0
            and abs((d["decode_tps"] or 0) - 30.0) < 1e-6
            and abs((d["decode_tps_median"] or 0) - 30.0) < 1e-6
            and "idle_w_mean" in d
            and "load_w_max" in d
            and d["runs_detail"][0]["eval_count"] == 30
            and d["shape"]["use_mmap"] == "default",
        )

        # Unreachable backend: warmup fails and main() returns 1 without writing
        # an --out file (preserved behavior), so assert on the exit code only.
        _STATE["load"] = False
        rc = power_sampler.main(
            ["--api", "openai", "--backend-url", "http://127.0.0.1:1", "--model", "m"]
            + _FAST
            + ["--out", os.path.join(tmp, "err.json")]
        )
        check(
            "5. unreachable backend -> no decode rate -> exit 1",
            rc == 1 and not os.path.exists(os.path.join(tmp, "err.json")),
        )
    finally:
        srv.shutdown()
        power_sampler.openai_generate = _REAL_OAI
        power_sampler.ollama_generate = _REAL_OLL

    total = _PASS + len(_FAIL)
    print("\n%d/%d checks passed" % (_PASS, total))
    if _FAIL:
        print("FAILURES: " + ", ".join(_FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
