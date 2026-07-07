#!/usr/bin/env python3
"""Offline, hardware-free verifier for the Strix Halo perf harness.

The mirror of the recipe's control-plane [`verify_local.py`](../verify_local.py):
it stands up in-process mock backends and exercises the REAL probe / rewrite /
aggregation code paths, asserting the whole perf pipeline end-to-end with **no
ROCm, no Docker, no live gateway**. This is the CI-grade proof that Test 1
(overhead-bench) and Test 2 (server-bench) are wired correctly; a hardware run
then only supplies the real numbers.

What it checks (each an assertion):
  1. tokrate_probe — Ollama dialect: decode/prefill tok/s from server timings.
  2. tokrate_probe — OpenAI dialect: usage tokens + client-timed decode + TTFT.
  3. tokrate_probe — auto dialect + concurrency fan-out.
  4. tokrate_probe — unreachable backend degrades to ok_runs=0 / exit 1.
  5. resource_sampler — byte parsing, timeseries summarize (peak VRAM/GTT), and
     a crash-free snapshot when rocm-smi/docker are absent.
  6. repoint_backend — in-place (same-inode) backend rewrite of the right block,
     leaving sibling cards untouched; missing alias -> exit 1.
  7. perf_metrics — fleet aggregation: per-tier mean drop, fleet-safe (worst-box)
     max-usable model, fastest server per box, skipped-server handling, markdown.

Usage:
  python3 verify_perf_local.py         # prints "N/N checks passed", exit 0/1
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import perf_metrics  # noqa: E402
import repoint_backend  # noqa: E402
import resource_sampler  # noqa: E402
import tokrate_probe  # noqa: E402

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
    """Serves a canned Ollama NDJSON stream and an OpenAI SSE stream."""

    def log_message(self, *_a):
        pass

    def do_POST(self):  # noqa: N802 (http.server API)
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.endswith("/api/generate"):
            lines = [
                {"response": "Hello", "done": False},
                {"response": " world", "done": False},
                {"done": True, "eval_count": 120, "eval_duration": 1_000_000_000,
                 "prompt_eval_count": 40, "prompt_eval_duration": 500_000_000},
            ]
            body = "".join(json.dumps(o) + "\n" for o in lines).encode()
        else:  # /v1/chat/completions
            evs = [
                {"choices": [{"delta": {"content": "Hello"}}]},
                {"choices": [{"delta": {"content": " world"}}]},
                {"choices": [{"delta": {}}], "usage": {"completion_tokens": 50, "prompt_tokens": 20}},
            ]
            body = ("".join("data: " + json.dumps(e) + "\n\n" for e in evs) + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_backend():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _MockBackend)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def verify_tokrate(base, tmp):
    rc = tokrate_probe.main(["--backend-url", base, "--api", "ollama", "--model", "m",
                             "--runs", "2", "--warmup", "0", "--max-tokens", "8",
                             "--out", os.path.join(tmp, "oll.json")])
    oll = json.load(open(os.path.join(tmp, "oll.json"), encoding="utf-8"))
    check("1. ollama exit 0 + decode_tps==120 + prefill==80",
          rc == 0 and abs(oll["aggregate"]["decode_tps_median"] - 120.0) < 1e-6
          and abs(oll["runs_detail"][0]["prefill_tps"] - 80.0) < 1e-6)

    rc = tokrate_probe.main(["--backend-url", base + "/v1", "--api", "openai", "--model", "m",
                             "--runs", "2", "--warmup", "0", "--max-tokens", "8",
                             "--out", os.path.join(tmp, "oai.json")])
    oai = json.load(open(os.path.join(tmp, "oai.json"), encoding="utf-8"))
    check("2. openai exit 0 + completion_tokens==50 + decode_tps>0 + ttft set",
          rc == 0 and oai["runs_detail"][0]["completion_tokens"] == 50
          and (oai["aggregate"]["decode_tps_median"] or 0) > 0
          and oai["runs_detail"][0]["ttft_ms"] is not None)

    rc = tokrate_probe.main(["--backend-url", base + "/v1", "--model", "m", "--runs", "1",
                             "--concurrency", "3", "--warmup", "0", "--out", os.path.join(tmp, "cc.json")])
    cc = json.load(open(os.path.join(tmp, "cc.json"), encoding="utf-8"))
    check("3. auto->openai + concurrency 3 -> ok_runs==3",
          cc["api"] == "openai" and cc["aggregate"]["ok_runs"] == 3)

    rc_err = tokrate_probe.main(["--backend-url", "http://127.0.0.1:1/", "--api", "ollama",
                                 "--model", "m", "--runs", "1", "--warmup", "0",
                                 "--out", os.path.join(tmp, "err.json")])
    err = json.load(open(os.path.join(tmp, "err.json"), encoding="utf-8"))
    check("4. unreachable backend -> ok_runs==0 + exit 1",
          rc_err == 1 and err["aggregate"]["ok_runs"] == 0)


def verify_sampler(tmp):
    nd = os.path.join(tmp, "s.ndjson")
    with open(nd, "w", encoding="utf-8") as fh:
        for used in (10, 20, 30):
            fh.write(json.dumps({
                "t": 1, "gpu": {"vram_used_b": used, "gtt_used_b": used * 2},
                "host": {"mem_total_b": 1000, "mem_available_b": 100, "mem_used_b": 900},
                "containers": {"vllm-sr-router-container": {"cpu_pct": used, "mem_used_b": used}},
            }) + "\n")
    summ = resource_sampler.summarize(nd)
    snap = resource_sampler.one_sample()
    check("5. sampler parse + summarize (peak vram 30 / gtt 60 / mean 20) + snapshot ok",
          abs(resource_sampler._parse_bytes("1.5GiB") - 1.5 * 1024**3) < 1
          and summ["peak_vram_used_b"] == 30 and summ["gpu"]["gtt_used_b"]["max"] == 60
          and summ["containers"]["vllm-sr-router-container"]["mem_used_b"]["mean"] == 20
          and set(snap) == {"t", "gpu", "host", "containers"})


def verify_repoint(tmp):
    cfg = os.path.join(tmp, "config.yaml")
    open(cfg, "w", encoding="utf-8").write(
        "providers:\n"
        "    models:\n"
        "        - name: google/gemini-2.5-flash-lite\n"
        "          backend_refs:\n"
        "            - name: ollama_local\n"
        "              endpoint: ollama:11434\n"
        "              protocol: http\n"
        "          external_model_ids:\n"
        "            vllm: qwen2.5:7b\n"
        "        - name: openai/gpt5.4\n"
        "          backend_refs:\n"
        "            - name: ollama_local\n"
        "              endpoint: ollama:11434\n"
        "          external_model_ids:\n"
        "            vllm: qwen3:14b\n"
        "routing:\n"
        "    modelCards:\n"
        "        - name: google/gemini-2.5-flash-lite\n"
        "          quality_score: 0.68\n"
    )
    ino = os.stat(cfg).st_ino
    rc = repoint_backend.main(["--config", cfg, "--alias", "google/gemini-2.5-flash-lite",
                               "--endpoint", "llama-server:8080", "--model", "qwen2.5-7b"])
    txt = open(cfg, encoding="utf-8").read()
    rc_missing = repoint_backend.main(["--config", cfg, "--alias", "no/such",
                                       "--endpoint", "x:1", "--model", "y"])
    check("6. repoint in-place (inode kept), only target block, missing alias -> exit 1",
          rc == 0 and "endpoint: llama-server:8080" in txt and "vllm: qwen2.5-7b" in txt
          and txt.count("endpoint: ollama:11434") == 1 and "vllm: qwen3:14b" in txt
          and os.stat(cfg).st_ino == ino and rc_missing == 1)


def verify_aggregate(tmp):
    bundle = os.path.join(tmp, "run-bundle")
    os.makedirs(bundle)
    json.dump({"box": "halo-a", "unified_mem_total_b": 128 * 1024**3,
               "stack_footprint": {"stack_container_mem_total_b": 4 * 1024**3},
               "tiers": [{"tag": "qwen2.5:7b", "throughput_drop_pct_contention": 10.0,
                          "throughput_drop_pct_end_to_end": 18.0}],
               "max_usable_tag": "qwen2.5:32b", "first_unusable_tag": "llama3.1:70b"},
              open(os.path.join(bundle, "overhead-halo-a.json"), "w", encoding="utf-8"))
    json.dump({"box": "halo-b", "unified_mem_total_b": 128 * 1024**3,
               "stack_footprint": {"stack_container_mem_total_b": 5 * 1024**3},
               "tiers": [{"tag": "qwen2.5:7b", "throughput_drop_pct_contention": 20.0,
                          "throughput_drop_pct_end_to_end": 30.0}],
               "max_usable_tag": "qwen2.5:14b", "first_unusable_tag": "qwen2.5:32b"},
              open(os.path.join(bundle, "overhead-halo-b.json"), "w", encoding="utf-8"))
    json.dump({"box": "halo-a", "common_base_model": "qwen2.5-7b",
               "servers": [
                   {"server": "ollama", "status": "measured", "quant": "Q4_0", "direct_decode_tps": 40.0, "direct_ttft_ms": 100},
                   {"server": "llamacpp", "status": "measured", "quant": "Q4_K_M", "direct_decode_tps": 55.0, "direct_ttft_ms": 80, "decode_tps_vs_ollama_pct": 37.5},
                   {"server": "vllm", "status": "skipped", "reason": "bring-up failed", "quant": "fp16"}]},
              open(os.path.join(bundle, "server-halo-a.json"), "w", encoding="utf-8"))
    m = perf_metrics.build(bundle)
    md = perf_metrics.to_markdown(m)
    check("7. fleet aggregate (mean drop 15, worst-box max-usable 14b, fastest llamacpp, skip kept) + md",
          m["overhead"]["boxes"] == ["halo-a", "halo-b"]
          and abs(m["overhead"]["per_tier_drop"][0]["mean_drop_pct_contention"] - 15.0) < 1e-6
          and m["overhead"]["fleet_max_usable_tag"] == "qwen2.5:14b"
          and m["servers"]["per_box"]["halo-a"]["fastest_server"] == "llamacpp"
          and any(s["status"] == "skipped" for s in m["servers"]["per_box"]["halo-a"]["servers"])
          and "Test 1" in md and "Test 2" in md)


def main():
    tmp = tempfile.mkdtemp(prefix="perf-verify-")
    srv, base = _start_backend()
    try:
        verify_tokrate(base, tmp)
        verify_sampler(tmp)
        verify_repoint(tmp)
        verify_aggregate(tmp)
    finally:
        srv.shutdown()
    total = _PASS + len(_FAIL)
    print("\n%d/%d checks passed" % (_PASS, total))
    if _FAIL:
        print("FAILURES: " + ", ".join(_FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
