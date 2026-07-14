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
  8. bestcfg_matrix — probe reduction (adds TTFT p50), cell classification
     (load-fail / unusable-below-floor / loaded).
  9. bestcfg_matrix — the fixed WINNER RULE: usable floor, c8-aggregate primary,
     TTFT p95 gate + fallback, tok/W -> TTFT p50 -> single-stream tie-break,
     per-server winners.
 10. bestcfg_matrix — assemble a work dir of per-cell files into the rollup JSON
     + comparison table + recommended-config line.
 11. bestcfg_matrix — cache overlay through a mock router: exact-repeat registers
     a hit (x-vsr-cache-hit) with a lower TTFT than a miss.

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

import bestcfg_matrix  # noqa: E402
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


def _matrix_cell(server, residency, parallel, status="loaded", c1_tps=36.0,
                 agg_c8=44.0, ttft_p95=800.0, ttft_p50=100.0, tpw=0.30):
    """Build a bestcfg_matrix cell dict shaped like build_cell() output."""
    return {
        "cell_id": bestcfg_matrix.cell_id(server, residency, parallel),
        "server": server, "residency": residency, "num_parallel": parallel,
        "model": "gpt-oss:120b-vram" if residency == "resident" else "gpt-oss:120b",
        "status": status, "reason": None,
        "c1": {"decode_tps_median": c1_tps, "aggregate_decode_tps": c1_tps,
               "ttft_ms_p50": ttft_p50, "ttft_ms_p95": ttft_p50, "ok_runs": 3},
        "c8": {"decode_tps_median": c1_tps, "aggregate_decode_tps": agg_c8,
               "ttft_ms_p95": ttft_p95, "ttft_ms_mean": ttft_p95, "ok_runs": 8},
        "power": {"tok_per_watt_load": tpw},
        "resource": {"peak_vram_used_gib": 61.6},
    }


def verify_matrix_logic():
    # 8. probe reduction + classification.
    probe = {"aggregate": {"decode_tps_median": 30.0, "aggregate_decode_tps": 120.0,
                           "ttft_ms_mean": 150.0, "ttft_ms_p95": 300.0,
                           "success_rate": 1.0, "ok_runs": 3},
             "runs_detail": [{"ok": True, "ttft_ms": 100.0}, {"ok": True, "ttft_ms": 200.0},
                             {"ok": True, "ttft_ms": 150.0}],
             "shape": {"concurrency": 1}}
    red = bestcfg_matrix.reduce_probe(probe)
    st_fail, _ = bestcfg_matrix.classify("load-fail", None, 3.0)
    st_unus, _ = bestcfg_matrix.classify("ok", {"ok_runs": 2, "decode_tps_median": 1.5}, 3.0)
    st_ok, _ = bestcfg_matrix.classify("ok", {"ok_runs": 2, "decode_tps_median": 36.0}, 3.0)
    st_empty, _ = bestcfg_matrix.classify("ok", {"ok_runs": 0, "decode_tps_median": None}, 3.0)
    check("8. probe reduce (p50=150, agg=120) + classify fail/unusable/loaded/empty",
          red["ttft_ms_p50"] == 150.0 and red["aggregate_decode_tps"] == 120.0
          and st_fail == "load-fail" and st_unus == "unusable"
          and st_ok == "loaded" and st_empty == "load-fail")

    # 9. winner rule. resident-p8 has the highest c8 aggregate AND clears the gate.
    cells = [
        _matrix_cell("ollama", "resident", 8, agg_c8=121.0, ttft_p95=826.0, tpw=0.36),
        _matrix_cell("ollama", "resident", 1, agg_c8=44.0, ttft_p95=20444.0, tpw=0.36),
        _matrix_cell("ollama", "auto", 8, status="unusable", c1_tps=0.2, agg_c8=4.0),
        _matrix_cell("ollama", "auto", 1, status="unusable", c1_tps=0.2, agg_c8=3.0),
        _matrix_cell("llamacpp", "resident", 8, agg_c8=90.0, ttft_p95=900.0, tpw=0.40),
        _matrix_cell("llamacpp", "resident", 1, agg_c8=40.0, ttft_p95=1500.0, tpw=0.40),
        _matrix_cell("llamacpp", "auto", 8, status="skipped"),
        _matrix_cell("llamacpp", "auto", 1, status="skipped"),
    ]
    sc = bestcfg_matrix.score(cells, 3.0, 2000.0)
    # Tie-break: two cells tie on c8-agg -> higher tok/W wins.
    tie = [
        _matrix_cell("ollama", "resident", 8, agg_c8=100.0, ttft_p95=800.0, tpw=0.30),
        _matrix_cell("llamacpp", "resident", 8, agg_c8=100.0, ttft_p95=800.0, tpw=0.50),
    ]
    sc_tie = bestcfg_matrix.score(tie, 3.0, 2000.0)
    # Gate fallback: NO cell clears a very strict gate -> still pick best-agg, flag it.
    sc_gate = bestcfg_matrix.score(cells, 3.0, 1.0)
    check("9. winner rule: c8-agg primary + gate, per-server winners, tok/W tie-break, gate fallback",
          sc["winner_cell_id"] == "ollama-resident-p8" and sc["winner_ttft_gate_met"] is True
          and sc["per_server_winner"]["llamacpp"]["cell_id"] == "llamacpp-resident-p8"
          and sc["eligible_count"] == 4
          and sc_tie["winner_cell_id"] == "llamacpp-resident-p8"
          and sc_gate["winner_cell_id"] == "ollama-resident-p8"
          and sc_gate["winner_ttft_gate_met"] is False)


def verify_matrix_assemble(tmp):
    work = os.path.join(tmp, "matrix-work")
    os.makedirs(work)

    def _probe(agg, decode, ttft95, conc):
        return {"aggregate": {"decode_tps_median": decode, "aggregate_decode_tps": agg,
                              "ttft_ms_mean": ttft95, "ttft_ms_p95": ttft95,
                              "success_rate": 1.0, "ok_runs": conc},
                "runs_detail": [{"ok": True, "ttft_ms": ttft95}], "shape": {"concurrency": conc}}

    def _write(cid, obj):
        with open(os.path.join(work, cid), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    # Winner cell: ollama resident p8 (highest c8 agg + clears the gate).
    _write("ollama-resident-p8.load.json", {"cell_id": "ollama-resident-p8", "server": "ollama",
           "residency": "resident", "num_parallel": 8, "model": "gpt-oss:120b-vram",
           "load_result": "ok", "reason": None, "gpu_resident_frac": 1.0})
    _write("ollama-resident-p8.c1.json", _probe(36.0, 36.0, 120.0, 1))
    _write("ollama-resident-p8.c8.json", _probe(121.0, 16.0, 826.0, 8))
    _write("ollama-resident-p8.res.json", {"peak_vram_used_b": 66 * 1024**3,
           "gpu": {"vram_used_b": {"max": 66 * 1024**3}}, "host": {"mem_used_b": {"max": 6 * 1024**3}}})
    _write("ollama-resident-p8.power.json", {"api": "ollama", "model": "gpt-oss:120b-vram",
           "idle_w": 10.0, "load_w_mean": 100.0, "load_w_peak": 110.0, "decode_tps": 36.5,
           "tok_per_watt_load": 0.365, "tok_per_watt_net_idle": 0.405})
    # A skipped llamacpp cell (load probe failed) -- must survive assembly.
    _write("llamacpp-resident-p8.load.json", {"cell_id": "llamacpp-resident-p8", "server": "llamacpp",
           "residency": "resident", "num_parallel": 8, "model": "gpt-oss-120b",
           "load_result": "skipped", "reason": "llama.cpp MXFP4 120B load probe failed"})
    # Cache overlay for the ollama winner.
    _write("cache-ollama.json", {"schema": "bestcfg-cache-overlay/v1", "server": "ollama",
           "cell_id": "ollama-resident-p8", "ttft_miss_ms": 1300.0, "ttft_hit_exact_ms": 2.0,
           "ttft_hit_semantic_ms": 800.0, "ttft_saved_ms": 1298.0,
           "exact_hit_rate": 1.0, "semantic_hit_rate": 1.0, "cases": 3})

    out = os.path.join(tmp, "rollup.json")
    shape = {"max_tokens": 128, "prompt_tokens": 256, "concurrency_lo": 1, "concurrency_hi": 8,
             "oom_min_tps": 3.0, "ttft_gate_ms": 2000.0, "model_flagship": "gpt-oss:120b"}
    rollup = bestcfg_matrix.assemble(work, "halo-b", shape, out)
    on_disk = json.load(open(out, encoding="utf-8"))
    table = bestcfg_matrix.render_table(rollup)
    check("10. assemble rollup: winner ollama-resident-p8, skipped cell kept, overlay + conclusion",
          rollup["scoring"]["winner_cell_id"] == "ollama-resident-p8"
          and on_disk["schema"] == "bestcfg-matrix/v1"
          and any(c["status"] == "skipped" for c in rollup["cells"])
          and rollup["cache_overlay"]["ollama"]["ttft_saved_ms"] == 1298.0
          and "NUM_PARALLEL=8" in rollup["recommended_config"]
          and "*WIN" in table)


def verify_matrix_cache_overlay(tmp):
    import http.server
    srv = http.server.HTTPServer(("127.0.0.1", 0), bestcfg_matrix._mock_handler_class())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    router = "http://127.0.0.1:%d/v1" % srv.server_address[1]
    try:
        ov = bestcfg_matrix.measure_cache_overlay(
            router, "ollama", "ollama-resident-p8", cfg_path="", container="none",
            threshold="0.92", reload_timeout=0.0, reload_settle=0.0)
    finally:
        srv.shutdown()
    check("11. cache overlay: exact-repeat hit registered, hit TTFT <= miss TTFT",
          ov["exact_hit_rate"] == 1.0
          and ov["ttft_miss_ms"] is not None and ov["ttft_hit_exact_ms"] is not None
          and ov["ttft_hit_exact_ms"] <= ov["ttft_miss_ms"] + 1e-6)


def main():
    tmp = tempfile.mkdtemp(prefix="perf-verify-")
    srv, base = _start_backend()
    try:
        verify_tokrate(base, tmp)
        verify_sampler(tmp)
        verify_repoint(tmp)
        verify_aggregate(tmp)
        verify_matrix_logic()
        verify_matrix_assemble(tmp)
        verify_matrix_cache_overlay(tmp)
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
