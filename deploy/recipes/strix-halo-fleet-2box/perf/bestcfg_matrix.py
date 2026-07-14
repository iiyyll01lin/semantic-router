#!/usr/bin/env python3
"""Scoring / rollup / mock helpers for the Strix Halo best-config MATRIX benchmark.

This is the pure-python brain behind [`bestcfg-matrix.sh`](bestcfg-matrix.sh): the
bash driver does the docker / ROCm / probe orchestration, and every decision that
is worth unit-testing lives HERE so the whole pipeline can be verified offline with
no ROCm, no Docker, no hardware (see [`verify_perf_local.py`](verify_perf_local.py)
and the driver's ``SELFTEST=1`` path).

The matrix is a "whole configuration combination" benchmark: instead of stitching
the best single number from several unrelated runs (the old perf-report §11.3
approach), every candidate config is run end-to-end as ONE profile over three
backend axes -- ``server {ollama, llamacpp} x residency {resident, auto} x
NUM_PARALLEL {1, 8}`` = 8 cells -- each measured with the SAME probes, then a single
fixed rule picks the winner. Semantic cache is an OVERLAY on each server's winning
cell (it only changes repeat-query TTFT, not decode/throughput), so it never
re-multiplies the 8 cells.

Subcommands:
  assemble       reduce a work dir of per-cell probe/res/power files into the rollup
                 JSON + printed comparison table (winner rule applied here).
  cache-overlay  measure repeat-query TTFT with semantic cache off vs on through the
                 router path (reuses the cache-sweep.sh config-edit mechanics).
  mock-serve     start an in-process mock backend/router (ollama + openai dialects,
                 /api/ps, /health, cache headers) for the SELFTEST path.

Winner rule (fixed, from the plan):
  1. Cell must be ``loaded`` AND single-stream decode >= OOM_MIN_TPS (usable floor).
  2. Primary: highest c8 AGGREGATE tok/s among cells whose TTFT p95 @ c8 <= gate (2 s).
  3. Tie-break: tok/s per W  ->  TTFT p50 @ c1  ->  single-stream decode tok/s.
  4. Cache overlay does NOT enter the backend ranking; it is reported separately as
     "repeat-query TTFT saved".

Stdlib only (json/urllib/http.server/subprocess/statistics), matching the rest of
this recipe. This module NEVER serves production traffic, deploys, or mutates a live
config on its own -- ``cache-overlay`` edits only the config path it is told to and
restores it, exactly like cache-sweep.sh.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import subprocess  # noqa: S404 (fixed, local diagnostic/reload commands only)
import sys
import time
import urllib.error
import urllib.request

GIB = 1024 ** 3

# The fixed 8-cell backend matrix. Order groups by server then residency so the
# driver reloads the 120B as few times as possible (R3 in the plan).
SERVERS = ("ollama", "llamacpp")
RESIDENCIES = ("resident", "auto")
PARALLELS = (1, 8)


def cell_id(server, residency, parallel):
    return "%s-%s-p%s" % (server, residency, parallel)


def matrix_cells():
    """Yield (server, residency, parallel) for all 8 cells in reload-friendly order."""
    for server in SERVERS:
        for residency in RESIDENCIES:
            for parallel in PARALLELS:
                yield server, residency, parallel


# --------------------------------------------------------------------------- #
# Probe reduction + cell classification
# --------------------------------------------------------------------------- #
def _percentile(values, pct):
    """Nearest-rank percentile (stdlib only; mirrors tokrate_probe._percentile)."""
    xs = sorted(v for v in values if isinstance(v, (int, float)))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = max(0, min(len(xs) - 1, int(round((pct / 100.0) * (len(xs) - 1)))))
    return xs[k]


def reduce_probe(probe):
    """Reduce one tokrate_probe.py JSON to the handful of fields the matrix scores.

    Adds a ``ttft_ms_p50`` (median) computed from ``runs_detail`` because
    tokrate_probe only emits mean/p95, and the tie-break rule wants p50.
    Returns None-filled dict when the probe is missing/empty (a failed cell).
    """
    probe = probe or {}
    agg = probe.get("aggregate") or {}
    ttfts = [r.get("ttft_ms") for r in (probe.get("runs_detail") or []) if r.get("ok")]
    return {
        "decode_tps_median": agg.get("decode_tps_median"),
        "aggregate_decode_tps": agg.get("aggregate_decode_tps"),
        "ttft_ms_mean": agg.get("ttft_ms_mean"),
        "ttft_ms_p50": _percentile(ttfts, 50),
        "ttft_ms_p95": agg.get("ttft_ms_p95"),
        "success_rate": agg.get("success_rate"),
        "ok_runs": agg.get("ok_runs") or 0,
        "concurrency": (probe.get("shape") or {}).get("concurrency"),
    }


def reduce_resource(res):
    """Peak VRAM / GTT / system-RAM (residency evidence) from a resource summary."""
    res = res or {}
    gpu = res.get("gpu") or {}
    host = res.get("host") or {}
    peak_vram = res.get("peak_vram_used_b") or (gpu.get("vram_used_b") or {}).get("max")
    peak_gtt = res.get("peak_gtt_used_b") or (gpu.get("gtt_used_b") or {}).get("max")
    peak_sys = (host.get("mem_used_b") or {}).get("max")
    return {
        "peak_vram_used_b": peak_vram,
        "peak_gtt_used_b": peak_gtt,
        "peak_sys_used_b": peak_sys,
        "peak_vram_used_gib": round(peak_vram / GIB, 2) if peak_vram else None,
        "peak_gtt_used_gib": round(peak_gtt / GIB, 2) if peak_gtt else None,
        "peak_sys_used_gib": round(peak_sys / GIB, 2) if peak_sys else None,
    }


def reduce_power(power):
    """Normalize a power_sampler.py JSON to the CONTRACT keys the matrix needs.

    The contract (implemented by the sibling power_sampler.py work) is:
      idle_w, load_w_mean, load_w_peak, decode_tps, tok_per_watt_load,
      tok_per_watt_net_idle, api, model.
    We also accept the ORIGINAL power-sampler/v1 key names (idle_w_mean /
    load_w_max / decode_tps_median) so this keeps working whichever landed first.
    """
    if not power:
        return None
    return {
        "idle_w": power.get("idle_w", power.get("idle_w_mean")),
        "load_w_mean": power.get("load_w_mean"),
        "load_w_peak": power.get("load_w_peak", power.get("load_w_max")),
        "decode_tps": power.get("decode_tps", power.get("decode_tps_median")),
        "tok_per_watt_load": power.get("tok_per_watt_load"),
        "tok_per_watt_net_idle": power.get("tok_per_watt_net_idle"),
        "api": power.get("api"),
        "model": power.get("model"),
    }


def classify(load_result, c1, floor):
    """Return (status, reason) for a cell.

    load_result comes from the driver ('ok' | 'load-fail' | 'skipped') and is
    authoritative for bring-up failures; on a healthy load we downgrade to
    'unusable' when single-stream decode is below the usable floor, or 'load-fail'
    when the backend produced no tokens at all.
    """
    if load_result == "skipped":
        return "skipped", None
    if load_result == "load-fail":
        return "load-fail", None
    # load_result == "ok": judge by the probe.
    ok_runs = (c1 or {}).get("ok_runs") or 0
    tps = (c1 or {}).get("decode_tps_median")
    if not ok_runs or tps is None:
        return "load-fail", "loaded but produced no decode tokens (probe empty)"
    if tps < floor:
        return "unusable", "single-stream decode %.2f tok/s < floor %g tok/s" % (tps, floor)
    return "loaded", None


def build_cell(work, load_meta, floor):
    """Assemble one cell record from its load-meta + probe/res/power sidecar files."""
    cid = load_meta.get("cell_id") or cell_id(
        load_meta.get("server"), load_meta.get("residency"), load_meta.get("num_parallel"))

    def _load(suffix):
        path = os.path.join(work, cid + suffix)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    c1 = reduce_probe(_load(".c1.json"))
    c8 = reduce_probe(_load(".c8.json"))
    res = reduce_resource(_load(".res.json"))
    # Authoritative GPU-layer residency fraction (ollama /api/ps size_vram/size),
    # captured by the driver at load time -- residency evidence beyond peak bytes.
    res["gpu_resident_frac"] = load_meta.get("gpu_resident_frac")
    power = reduce_power(_load(".power.json"))
    status, auto_reason = classify(load_meta.get("load_result", "ok"), c1, floor)
    reason = load_meta.get("reason") or auto_reason
    return {
        "cell_id": cid,
        "server": load_meta.get("server"),
        "residency": load_meta.get("residency"),
        "num_parallel": load_meta.get("num_parallel"),
        "model": load_meta.get("model"),
        "status": status,
        "reason": reason,
        "c1": c1,
        "c8": c8,
        "resource": res,
        "power": power,
    }


# --------------------------------------------------------------------------- #
# Winner rule
# --------------------------------------------------------------------------- #
def _neg(x):
    """Sort key helper: None -> -inf so missing metrics sort last on a desc sort."""
    return x if isinstance(x, (int, float)) else float("-inf")


def _pos(x):
    """Sort key helper: None -> +inf so missing metrics sort last on an asc sort."""
    return x if isinstance(x, (int, float)) else float("inf")


def _rank_key(cell):
    """(agg_c8 desc, tok/W desc, ttft_p50 asc, single-stream desc). Python sorts
    ascending, so negate the 'desc' metrics."""
    agg_c8 = (cell.get("c8") or {}).get("aggregate_decode_tps")
    tpw = (cell.get("power") or {}).get("tok_per_watt_load") if cell.get("power") else None
    ttft_p50 = (cell.get("c1") or {}).get("ttft_ms_p50")
    single = (cell.get("c1") or {}).get("decode_tps_median")
    return (-_neg(agg_c8), -_neg(tpw), _pos(ttft_p50), -_neg(single))


def _gate_ok(cell, ttft_gate_ms):
    p95 = (cell.get("c8") or {}).get("ttft_ms_p95")
    return isinstance(p95, (int, float)) and p95 <= ttft_gate_ms


def _pick(cells, ttft_gate_ms):
    """Apply the fixed winner rule to a list of eligible (loaded) cells.

    Returns (winner_cell_or_None, gate_met_bool). Prefer cells meeting the TTFT
    p95 gate; only if NONE meet it do we fall back to the best-by-throughput cell
    and flag gate_met=False so the caller/report can say so honestly.
    """
    if not cells:
        return None, False
    gated = [c for c in cells if _gate_ok(c, ttft_gate_ms)]
    if gated:
        return sorted(gated, key=_rank_key)[0], True
    return sorted(cells, key=_rank_key)[0], False


def score(cells, floor, ttft_gate_ms):
    """Rank cells and pick the overall + per-server winners under the fixed rule."""
    eligible = [c for c in cells if c.get("status") == "loaded"]
    ranked = sorted(eligible, key=_rank_key)
    overall, overall_gate = _pick(eligible, ttft_gate_ms)
    per_server = {}
    for server in SERVERS:
        srv_cells = [c for c in eligible if c.get("server") == server]
        win, gate = _pick(srv_cells, ttft_gate_ms)
        per_server[server] = {
            "cell_id": win["cell_id"] if win else None,
            "ttft_gate_met": gate if win else None,
        }
    return {
        "winner_cell_id": overall["cell_id"] if overall else None,
        "winner_ttft_gate_met": overall_gate if overall else None,
        "per_server_winner": per_server,
        "ranked_cell_ids": [c["cell_id"] for c in ranked],
        "eligible_count": len(eligible),
    }


# --------------------------------------------------------------------------- #
# Rollup + comparison table
# --------------------------------------------------------------------------- #
def _fmt(x, spec):
    return (spec % x) if isinstance(x, (int, float)) else "-"


def recommended_config(rollup):
    """One-line, customer-facing conclusion for the winning combined config."""
    cells = {c["cell_id"]: c for c in rollup.get("cells", [])}
    wid = (rollup.get("scoring") or {}).get("winner_cell_id")
    win = cells.get(wid)
    if not win:
        return "No usable configuration cleared the floor on this run."
    agg = _fmt((win.get("c8") or {}).get("aggregate_decode_tps"), "%.1f")
    tpw = _fmt((win.get("power") or {}).get("tok_per_watt_load") if win.get("power") else None, "%.3f")
    ttft = _fmt((win.get("c8") or {}).get("ttft_ms_p95"), "%.0f")
    overlay = (rollup.get("cache_overlay") or {}).get(win.get("server")) or {}
    saved = overlay.get("ttft_saved_ms")
    cache_bit = " (+cache: repeat TTFT %s -> %s ms)" % (
        _fmt(overlay.get("ttft_miss_ms"), "%.0f"), _fmt(overlay.get("ttft_hit_exact_ms"), "%.0f")
    ) if saved is not None else ""
    gate = "" if (rollup.get("scoring") or {}).get("winner_ttft_gate_met") else \
        " [NOTE: no cell met the TTFT p95<=%gms gate; winner is best-throughput]" % \
        (rollup.get("shape") or {}).get("ttft_gate_ms", 2000)
    return ("%s on Halo-B: %s + %s + NUM_PARALLEL=%s -> %s tok/s aggregate @ c8, "
            "%s tok/s/W, TTFT p95 %s ms%s%s" % (
                win.get("model"), win.get("server"), win.get("residency"),
                win.get("num_parallel"), agg, tpw, ttft, cache_bit, gate))


def build_rollup(cells, cache_overlay, box, shape):
    scoring = score(cells, shape["oom_min_tps"], shape["ttft_gate_ms"])
    rollup = {
        "schema": "bestcfg-matrix/v1",
        "box": box,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_flagship": shape.get("model_flagship"),
        "shape": shape,
        "cells": cells,
        "cache_overlay": cache_overlay or {},
        "scoring": scoring,
    }
    rollup["recommended_config"] = recommended_config(rollup)
    return rollup


def render_table(rollup):
    """Human-readable comparison table (printed to stdout by the driver)."""
    cells = {c["cell_id"]: c for c in rollup.get("cells", [])}
    scoring = rollup.get("scoring") or {}
    wid = scoring.get("winner_cell_id")
    lines = []
    lines.append("== best-config matrix (box=%s, model=%s) =="
                 % (rollup.get("box"), rollup.get("model_flagship")))
    hdr = "%-22s %-10s %8s %10s %10s %9s %9s %s" % (
        "cell", "status", "c1_tps", "c8_agg", "ttft95_c8", "tok/W", "vram_gib", "note")
    lines.append(hdr)
    for cid in _display_order(rollup):
        c = cells.get(cid)
        if not c:
            continue
        c1 = c.get("c1") or {}
        c8 = c.get("c8") or {}
        power = c.get("power") or {}
        star = " *WIN" if cid == wid else ""
        note = (c.get("reason") or "")
        lines.append("%-22s %-10s %8s %10s %10s %9s %9s %s%s" % (
            cid, c.get("status"),
            _fmt(c1.get("decode_tps_median"), "%.1f"),
            _fmt(c8.get("aggregate_decode_tps"), "%.1f"),
            _fmt(c8.get("ttft_ms_p95"), "%.0f"),
            _fmt(power.get("tok_per_watt_load"), "%.3f"),
            _fmt((c.get("resource") or {}).get("peak_vram_used_gib"), "%.1f"),
            note, star))
    # Cache overlay summary.
    for server, ov in sorted((rollup.get("cache_overlay") or {}).items()):
        lines.append("  cache overlay [%s winner %s]: repeat TTFT miss %s ms -> exact-hit %s ms "
                     "(saved %s ms), semantic-0.92 hit %s ms" % (
                         server, ov.get("cell_id"),
                         _fmt(ov.get("ttft_miss_ms"), "%.0f"),
                         _fmt(ov.get("ttft_hit_exact_ms"), "%.0f"),
                         _fmt(ov.get("ttft_saved_ms"), "%.0f"),
                         _fmt(ov.get("ttft_hit_semantic_ms"), "%.0f")))
    lines.append("CONCLUSION: " + rollup.get("recommended_config", ""))
    return "\n".join(lines) + "\n"


def _display_order(rollup):
    """Rank order first (winner on top), then any non-eligible cells after."""
    scoring = rollup.get("scoring") or {}
    ranked = list(scoring.get("ranked_cell_ids") or [])
    all_ids = [c["cell_id"] for c in rollup.get("cells", [])]
    tail = [cid for cid in all_ids if cid not in ranked]
    return ranked + tail


# --------------------------------------------------------------------------- #
# assemble subcommand
# --------------------------------------------------------------------------- #
def assemble(work, box, shape, out_path):
    cells = []
    for meta_path in sorted(glob.glob(os.path.join(work, "*.load.json"))):
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        cells.append(build_cell(work, meta, shape["oom_min_tps"]))
    # Merge any cache-overlay files.
    cache_overlay = {}
    for ov_path in sorted(glob.glob(os.path.join(work, "cache-*.json"))):
        try:
            with open(ov_path, "r", encoding="utf-8") as fh:
                ov = json.load(fh)
        except (OSError, ValueError):
            continue
        if ov.get("server"):
            cache_overlay[ov["server"]] = ov
    rollup = build_rollup(cells, cache_overlay, box, shape)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(rollup, fh, indent=2, sort_keys=True)
            fh.write("\n")
    print(render_table(rollup))
    return rollup


# --------------------------------------------------------------------------- #
# Cache overlay (reuses cache-sweep.sh config-edit mechanics)
# --------------------------------------------------------------------------- #
def _indent_of(line):
    return len(line) - len(line.lstrip())


def inject_cache_plugin(text):
    """Add a semantic-cache plugin (enabled: true) to every decision's plugins
    list -- identical to cache-sweep.sh so route-local caching actually runs."""
    if "- type: semantic-cache" in text:
        return text
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        if line.strip() == "plugins:":
            item = _indent_of(line)
            j = i + 1
            while j < len(lines):
                s = lines[j].strip()
                if s.startswith("- "):
                    item = _indent_of(lines[j]); break
                if s:
                    break
                j += 1
            out.append(" " * item + "- type: semantic-cache")
            out.append(" " * (item + 2) + "configuration:")
            out.append(" " * (item + 4) + "enabled: true")
        i += 1
    return "\n".join(out)


def set_threshold(text, t):
    """Set stores.semantic_cache.similarity_threshold in place (cache-sweep copy)."""
    out, ins, ind0, child, done = [], False, None, None, False
    for line in text.split("\n"):
        s = line.strip()
        if s == "semantic_cache:":
            ins, ind0, child, done = True, _indent_of(line), None, False
            out.append(line); continue
        if ins:
            ind = _indent_of(line)
            if s and ind <= ind0:
                if not done:
                    ci = child if child is not None else ind0 + 2
                    out.append(" " * ci + "similarity_threshold: %s" % t); done = True
                ins = False
            else:
                if s and child is None:
                    child = ind
                if s.startswith("similarity_threshold:"):
                    out.append(" " * ind + "similarity_threshold: %s" % t); done = True; continue
        out.append(line)
    if ins and not done:
        ci = child if child is not None else ind0 + 2
        out.append(" " * ci + "similarity_threshold: %s" % t)
    return "\n".join(out)


def _reloaded_since(container, epoch):
    try:
        r = subprocess.run(["docker", "logs", "--since", str(int(epoch)), container],
                           capture_output=True, text=True, timeout=20, check=False)
        return '"config_reloaded"' in (r.stdout + r.stderr)
    except (OSError, subprocess.SubprocessError):
        return False


def _docker_has(container):
    try:
        r = subprocess.run(["docker", "inspect", container],
                           capture_output=True, timeout=15, check=False)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def apply_config(cfg_path, text, container, reload_timeout, reload_settle):
    """Truncate-write the SAME inode (fsnotify fires) and wait for reload."""
    since = time.time()
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if reload_timeout > 0 and _docker_has(container):
        deadline = time.time() + reload_timeout
        while time.time() < deadline:
            if _reloaded_since(container, since):
                break
            time.sleep(1)
    elif reload_timeout > 0:
        time.sleep(reload_timeout)
    time.sleep(reload_settle)


def ask_router(chat_url, question, phase, timeout=180):
    """Send one router chat request; return (ttft_ms, hit_bool, similarity).

    Hit is read from the router's x-vsr-cache-hit header (cache-sweep.sh). We also
    send an x-mock-cache header carrying the current phase; the REAL router ignores
    unknown headers, while the SELFTEST mock uses it to return deterministic
    hits/misses without a live embedding model.
    """
    body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": question}],
                       "max_tokens": 16}).encode()
    req = urllib.request.Request(chat_url, data=body, headers={
        "Content-Type": "application/json", "x-vsr-debug": "true", "x-mock-cache": phase})
    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 (trusted local URL)
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        resp.read()
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in (list(e.headers.items()) if e.headers else [])}
        try:
            e.read()
        except OSError:
            pass
    except (urllib.error.URLError, OSError):
        return None, False, None
    ttft = (time.perf_counter() - t0) * 1000.0
    hit = str(hdrs.get("x-vsr-cache-hit", "")).lower() == "true"
    sim = hdrs.get("x-vsr-cache-similarity")
    return ttft, hit, (float(sim) if sim else None)


# (base, semantic-paraphrase = should hit at 0.92). Small, self-contained set.
_CACHE_CASES = [
    ("What is the capital of France?", "Which city is France's capital?"),
    ("Explain what an API is.", "Briefly describe what an API is."),
    ("How does TCP differ from UDP?", "Difference between TCP and UDP?"),
]


def measure_cache_overlay(router_url, server, cell_id_, cfg_path, container,
                          threshold, reload_timeout, reload_settle):
    """Measure repeat-query TTFT with semantic cache OFF vs ON on one server's
    winning cell. Edits only cfg_path and restores it. Returns the overlay dict."""
    chat = router_url.rstrip("/") + "/chat/completions"
    orig = None
    if cfg_path and os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as fh:
            orig = fh.read()

    # Phase OFF: cache disabled -> a repeated exact query is still a MISS.
    if orig is not None:
        apply_config(cfg_path, orig, container, reload_timeout, reload_settle)
    miss = []
    for base, _ in _CACHE_CASES:
        q = base + " (overlay-off %s)" % cell_id_
        ask_router(chat, q, "off")            # populate (miss)
        ttft, _, _ = ask_router(chat, q, "off")  # repeat -> still miss (cache off)
        if ttft is not None:
            miss.append(ttft)

    # Phase ON: enable route-local cache @ threshold -> repeat exact hits, paraphrase hits.
    if orig is not None:
        text = set_threshold(inject_cache_plugin(orig), threshold)
        apply_config(cfg_path, text, container, reload_timeout, reload_settle)
    exact_hits, sem_hits, exact_n, sem_n = [], [], 0, 0
    for base, para in _CACHE_CASES:
        q = base + " (overlay-on %s)" % cell_id_
        m, _, _ = ask_router(chat, q, "on")       # first = miss (populate)
        te, he, _ = ask_router(chat, q, "on")     # exact repeat -> hit
        if he and te is not None:
            exact_hits.append(te); exact_n += 1
        tp, hp, _ = ask_router(chat, para + " (overlay-on %s)" % cell_id_, "on")  # paraphrase
        if hp and tp is not None:
            sem_hits.append(tp); sem_n += 1

    if orig is not None:
        apply_config(cfg_path, orig, container, reload_timeout, reload_settle)  # restore

    miss_ms = statistics.mean(miss) if miss else None
    exact_ms = statistics.mean(exact_hits) if exact_hits else None
    sem_ms = statistics.mean(sem_hits) if sem_hits else None
    return {
        "schema": "bestcfg-cache-overlay/v1",
        "server": server,
        "cell_id": cell_id_,
        "threshold": threshold,
        "ttft_miss_ms": miss_ms,
        "ttft_hit_exact_ms": exact_ms,
        "ttft_hit_semantic_ms": sem_ms,
        "ttft_saved_ms": (miss_ms - exact_ms) if (miss_ms and exact_ms) else None,
        "exact_hit_rate": exact_n / len(_CACHE_CASES),
        "semantic_hit_rate": sem_n / len(_CACHE_CASES),
        "cases": len(_CACHE_CASES),
    }


# --------------------------------------------------------------------------- #
# Mock backend/router for the SELFTEST path
# --------------------------------------------------------------------------- #
def _mock_handler_class():
    import http.server

    class _Mock(http.server.BaseHTTPRequestHandler):
        """Serves ollama + openai dialects, /api/ps, /health, and a cache-aware
        router chat endpoint (x-mock-cache header decides hit/miss on repeats)."""

        seen = set()

        def log_message(self, *_a):  # silence
            pass

        def _json(self, obj, extra_headers=None):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            if self.path.endswith("/api/tags"):
                self._json({"models": [{"name": "gpt-oss:120b"}, {"name": "gpt-oss:120b-vram"}]})
            elif self.path.endswith("/api/ps"):
                self._json({"models": [{"name": "gpt-oss:120b-vram",
                                        "size": 62 * GIB, "size_vram": 62 * GIB}]})
            elif self.path.endswith("/health") or self.path.endswith("/v1/models"):
                self._json({"status": "ok", "data": [{"id": "gpt-oss-120b"}]})
            else:
                self._json({"ok": True})

        def do_POST(self):  # noqa: N802 (http.server API)
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            phase = (self.headers.get("x-mock-cache") or "").lower()
            if self.path.endswith("/api/generate"):
                lines = [
                    {"response": "Hello", "done": False},
                    {"response": " world", "done": False},
                    {"done": True, "eval_count": 128, "eval_duration": 1_000_000_000,
                     "prompt_eval_count": 40, "prompt_eval_duration": 500_000_000},
                ]
                body = "".join(json.dumps(o) + "\n" for o in lines).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # /v1/chat/completions -- decide if this is a cache probe (router path).
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {}
            content = ""
            for m in payload.get("messages", []):
                content += str(m.get("content", ""))
            headers = {}
            if phase:  # router cache probe
                key = content
                if phase == "on" and key in self.seen:
                    headers["x-vsr-cache-hit"] = "true"
                    headers["x-vsr-cache-similarity"] = "0.98"
                self.seen.add(key)
            evs = [
                {"choices": [{"delta": {"content": "Hello"}}]},
                {"choices": [{"delta": {"content": " world"}}]},
                {"choices": [{"delta": {}}], "usage": {"completion_tokens": 64, "prompt_tokens": 20}},
            ]
            body = ("".join("data: " + json.dumps(e) + "\n\n" for e in evs) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

    return _Mock


def mock_serve(host, port, portfile):
    import http.server
    srv = http.server.HTTPServer((host, port), _mock_handler_class())
    if portfile:
        with open(portfile, "w", encoding="utf-8") as fh:
            fh.write(str(srv.server_address[1]))
    print("mock backend on http://%s:%d" % (host, srv.server_address[1]), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(prog="bestcfg_matrix", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("assemble", help="reduce a work dir into the rollup JSON + table")
    pa.add_argument("--work", required=True)
    pa.add_argument("--out", default="")
    pa.add_argument("--box", default="box")
    pa.add_argument("--model-flagship", default="gpt-oss:120b")
    pa.add_argument("--oom-min-tps", type=float, default=3.0)
    pa.add_argument("--ttft-gate-ms", type=float, default=2000.0)
    pa.add_argument("--max-tokens", type=int, default=128)
    pa.add_argument("--prompt-tokens", type=int, default=256)
    pa.add_argument("--concurrency-hi", type=int, default=8)

    pc = sub.add_parser("cache-overlay", help="measure cache off/on repeat TTFT (router path)")
    pc.add_argument("--router-url", required=True)
    pc.add_argument("--server", required=True)
    pc.add_argument("--cell-id", required=True)
    pc.add_argument("--config", default="")
    pc.add_argument("--container", default="vllm-sr-router-container")
    pc.add_argument("--threshold", default="0.92")
    pc.add_argument("--reload-timeout", type=float, default=45.0)
    pc.add_argument("--reload-settle", type=float, default=3.0)
    pc.add_argument("--out", default="")

    pm = sub.add_parser("mock-serve", help="start the SELFTEST mock backend/router")
    pm.add_argument("--host", default="127.0.0.1")
    pm.add_argument("--port", type=int, default=0)
    pm.add_argument("--portfile", default="")

    args = p.parse_args(argv)

    if args.cmd == "assemble":
        shape = {
            "max_tokens": args.max_tokens,
            "prompt_tokens": args.prompt_tokens,
            "concurrency_lo": 1,
            "concurrency_hi": args.concurrency_hi,
            "oom_min_tps": args.oom_min_tps,
            "ttft_gate_ms": args.ttft_gate_ms,
            "model_flagship": args.model_flagship,
        }
        assemble(args.work, args.box, shape, args.out)
        return 0

    if args.cmd == "cache-overlay":
        ov = measure_cache_overlay(
            args.router_url, args.server, args.cell_id, args.config, args.container,
            args.threshold, args.reload_timeout, args.reload_settle)
        text = json.dumps(ov, indent=2, sort_keys=True)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        print(text)
        return 0

    if args.cmd == "mock-serve":
        return mock_serve(args.host, args.port, args.portfile)

    return 2


if __name__ == "__main__":
    sys.exit(main())
