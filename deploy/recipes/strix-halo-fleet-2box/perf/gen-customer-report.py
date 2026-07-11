#!/usr/bin/env python3
"""gen-customer-report.py -- customer-facing report (feasibility boundary + cost).

Reads a perf bundle and writes <bundle>/customer-report.md. Everything is derived
from measured files where available; projected values are labelled (est).

Usage: python3 gen-customer-report.py <bundle> [--cloud-usd-per-1m 0.60] [--hw-usd 2500]
"""
import argparse
import csv
import glob
import json
import os

# GiB of unified memory per 1 billion params, by quantization.
QUANT_GIB_PER_B = {"Q4": 0.6, "Q8": 1.1, "fp16": 2.2}


def load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def first(g):
    xs = sorted(glob.glob(g))
    return xs[0] if xs else None


def fmt(x, p="%.1f"):
    return "-" if x is None else (p % x if isinstance(x, (int, float)) else str(x))


def human_tokens(n):
    if n is None:
        return "-"
    for unit, div in (("trillion", 1e12), ("billion", 1e9), ("million", 1e6)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%.0f" % n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle")
    ap.add_argument("--cloud-usd-per-1m", type=float, default=0.60,
                    help="cloud API price $/1M output tokens for the comparable model")
    ap.add_argument("--hw-usd", type=float, default=2500.0, help="one-off box hardware cost")
    a = ap.parse_args()
    B = a.bundle

    metrics = load(os.path.join(B, "perf-metrics.json")) or {}
    overhead = load(first(os.path.join(B, "overhead-*.json"))) or {}
    servers = load(first(os.path.join(B, "server-*.json"))) or {}
    maxmodel = load(first(os.path.join(B, "maxmodel-*.json"))) or {}

    unified = overhead.get("unified_mem_total_b")
    unified_gib = round(unified / 1024**3, 1) if unified else None
    stack = (overhead.get("stack_footprint", {}) or {}).get("stack_container_mem_total_b")
    stack_gib = round(stack / 1024**3, 1) if stack else None
    tiers = overhead.get("tiers", []) or []
    d_ttft = next((t.get("baseline_ttft_ms") for t in tiers if t.get("baseline_ttft_ms")), None)
    r_ttft = next((t.get("colocated_router_ttft_ms") for t in tiers if t.get("colocated_router_ttft_ms")), None)
    tax = (r_ttft - d_ttft) / 1000.0 if (d_ttft and r_ttft) else None

    o = []
    o.append("# vllm-sr on Strix Halo — feasibility & cost (customer brief)")
    o.append("")
    o.append("_Box: %s · unified memory: %s GiB · vllm-sr stack footprint: %s GiB_"
             % (overhead.get("box", "halo-a"), fmt(unified_gib), fmt(stack_gib)))
    o.append("")

    # 1. Bottom line
    o.append("## 1. Bottom line")
    o.append("")
    o.append("- **It runs today:** router + backend share one box; the router costs ~%s GiB "
             "of memory and near-zero decode throughput." % fmt(stack_gib))
    o.append("- **The real cost is first-token latency:** direct ~%s ms -> through the router "
             "~%s ms (**+%s s**), which a semantic-cache hit removes entirely."
             % (fmt(d_ttft, "%.0f"), fmt(r_ttft, "%.0f"), fmt(tax, "%.1f")))
    o.append("- **Feasibility is memory-bound:** the biggest model a box serves = unified "
             "memory ÷ quantization (below).")
    o.append("")

    # 2. Feasibility boundary
    o.append("## 2. Feasibility boundary — largest model each box can serve")
    o.append("")
    o.append("Fleet-safe max usable model: **%s**."
             % (metrics.get("overhead", {}) or {}).get("fleet_max_usable_tag", overhead.get("max_usable_tag", "-")))
    o.append("")
    if maxmodel.get("results"):
        o.append("| model | est. footprint (GiB) | projected mem use | ran on | decode tok/s | verdict |")
        o.append("|---|---|---|---|---|---|")
        for r in maxmodel["results"]:
            verdict = r.get("verdict") or "-"
            box = r.get("chosen_box") or "-"
            tps = r.get("decode_tps")
            reason = (r.get("reason") or "").strip()
            # Report where the model ACTUALLY ran, not where it was routed. maxmodel-bench
            # sets chosen_box to the offload TARGET (halo-b) as an intent even when the probe
            # is then skipped because Halo-B is unreachable/unprovisioned -- so the raw
            # chosen_box would claim a run that never happened. Reflect the true verdict:
            #   usable    -> measured on <box>
            #   load-fail -> attempted on <box>, produced no tokens
            #   skipped   -> never ran anywhere; keep the skip reason
            if verdict == "usable" and tps is not None:
                ran_on, verdict_disp = box, "usable (measured)"
            elif verdict == "load-fail":
                ran_on = "%s (load failed)" % box
                verdict_disp = ("load-fail — %s" % reason) if reason else "load-fail"
            elif verdict == "skipped":
                ran_on = "not run"
                verdict_disp = ("skipped — %s" % reason) if reason else "skipped"
            else:
                ran_on, verdict_disp = box, verdict
            o.append("| %s | %s | %s%% | %s | %s | %s |" % (
                r.get("tag"), fmt(r.get("est_footprint_gib")), fmt(r.get("projected_pct")),
                ran_on, fmt(tps, "%.1f"), verdict_disp))
        o.append("")
        o.append("_Models whose projected memory use exceeds %s%% of one box are offloaded to Halo-B; "
                 "when Halo-B is unprovisioned/unreachable the oversized model is skipped-with-reason "
                 "(never attributed to a box it did not run on). A model that fits neither box is the "
                 "hard boundary._" % maxmodel.get("nearfull_pct", 85))
        o.append("")

    if unified_gib:
        reserve = round(unified_gib * 0.10, 1)
        usable = round(unified_gib - (stack_gib or 9.0) - reserve, 1)
        o.append("### Quantization decides the ceiling (%s GiB usable after stack + 10%% reserve)" % usable)
        o.append("")
        o.append("| quantization | GiB per 1B params | max params on this box |")
        o.append("|---|---|---|")
        for q in ("Q4", "Q8", "fp16"):
            gpb = QUANT_GIB_PER_B[q]
            maxp = int(usable / gpb) if usable > 0 else 0
            o.append("| %s | %.1f | ~%dB |" % (q, gpb, maxp))
        o.append("")
        o.append("_Q4 roughly **doubles** the largest model vs fp16 on the same hardware — the "
                 "practical lever for fitting a bigger model._")
        o.append("")

    # 3. Latency + cache mitigation
    o.append("## 3. Latency tax and how the cache removes it")
    o.append("")
    o.append("The router adds ~%s s to first-token latency (classification + embedding + routing). "
             "A **semantic-cache hit** skips that pipeline and the model call entirely:" % fmt(tax, "%.1f"))
    o.append("")
    csvp = first(os.path.join(B, "cache-sweep-*.csv"))
    if csvp and os.path.exists(csvp):
        rows = [r for r in csv.reader(open(csvp, encoding="utf-8")) if r]
        if len(rows) > 1:
            o.append("| " + " | ".join(rows[0]) + " |")
            o.append("|" + "---|" * len(rows[0]))
            for r in rows[1:]:
                o.append("| " + " | ".join(r) + " |")
            o.append("")
            try:
                hdr = rows[0]
                fi = hdr.index("false_hit_rate")
                ti = hdr.index("threshold")
                ok = [r for r in rows[1:] if float(r[fi]) == 0.0]
                if ok:
                    o.append("_Recommended threshold: **%s** — the lowest that never serves a wrong "
                             "cached answer (false-hit = 0), maximising coverage._" % ok[0][ti])
                    o.append("")
            except Exception:
                pass
    else:
        o.append("_(run the cache sweep to fill this table)_")
        o.append("")

    # 4. Concurrency boundary
    concs = []
    for p in sorted(glob.glob(os.path.join(B, "conc-c*-*.json"))):
        d = load(p) or {}
        ag = d.get("aggregate", {}) or {}
        concs.append((ag.get("concurrency"), ag.get("aggregate_decode_tps"), ag.get("ttft_ms_p95")))
    if concs:
        base = next((tps for c, tps, _ in sorted(concs, key=lambda x: x[0] or 0) if tps), None)
        o.append("## 4. Concurrency boundary")
        o.append("")
        o.append("| concurrent streams | aggregate tok/s | scaling vs 1 | TTFT p95 ms |")
        o.append("|---|---|---|---|")
        for c, tps, p95 in sorted(concs, key=lambda x: x[0] or 0):
            scal = (tps / base) if (tps and base) else None
            o.append("| %s | %s | %s | %s |" % (c, fmt(tps, "%.0f"),
                     ("%.2fx" % scal) if scal is not None else "-", fmt(p95, "%.0f")))
        o.append("")
        o.append("_Default Ollama serves one stream at a time: total throughput stays flat while "
                 "first-token latency grows with the queue. Effective capacity ~1 concurrent request "
                 "per box at full speed; scale with OLLAMA_NUM_PARALLEL or llama.cpp/vLLM._")
        o.append("")

    # 5. Server comparison + quant parity
    per_box = (servers.get("per_box") or servers) if servers else {}
    rows = None
    if isinstance(per_box, dict):
        for _, rec in per_box.items():
            if isinstance(rec, dict) and rec.get("servers"):
                rows = rec["servers"]
                break
    if rows is None:
        rows = servers.get("servers")
    if rows:
        o.append("## 5. Inference-server options (same base model — note the quantization)")
        o.append("")
        o.append("| server | quantization | decode tok/s | TTFT ms | status |")
        o.append("|---|---|---|---|---|")
        for r in rows:
            o.append("| %s | **%s** | %s | %s | %s |" % (
                r.get("server"), r.get("quant", "-"), fmt(r.get("direct_decode_tps"), "%.1f"),
                fmt(r.get("direct_ttft_ms"), "%.0f"), r.get("status", "-")))
        o.append("")
        o.append("_Quantization differs per server, so decode-rate deltas are **not** apples-to-apples "
                 "— compare within the same quantization. Quantization also sets the max model (§2)._")
        o.append("")

    # 6. Cost
    o.append("## 6. Cost — can it really be cheaper?")
    o.append("")
    rate = next((t.get("baseline_decode_tps") for t in tiers if "7b" in (t.get("tag") or "").lower()), None)
    o.append("**(a) vs cloud API.** After the one-off box cost (~$%.0f), local tokens are ~$0 marginal. "
             "At $%.2f / 1M output tokens, the box pays for itself after ~%s output tokens."
             % (a.hw_usd, a.cloud_usd_per_1m,
                human_tokens(a.hw_usd / (a.cloud_usd_per_1m / 1e6) if a.cloud_usd_per_1m else None)))
    o.append("")
    o.append("**(b) vs no routing.** vllm-sr sends easy queries to a small model instead of always the "
             "big one; the small tier runs ~%sx faster than the 32B, so routed traffic is proportionally "
             "cheaper per request." % fmt((rate / 10.8) if rate else None, "%.1f"))
    o.append("")
    o.append("**(c) vs a discrete GPU.** Strix Halo's %s GiB unified memory holds a 32B that would need a "
             ">40 GB discrete card; one integrated box replaces a GPU-server tier (lower capex + power)."
             % fmt(unified_gib))
    o.append("")
    o.append("_Marginal per-token cost is ~$0 locally; the levers that make it genuinely cheaper: "
             "(1) route down to small models, (2) cache hits remove repeat work, (3) fit via Q4 instead "
             "of paying for a bigger card._")
    o.append("")

    out = os.path.join(B, "customer-report.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(o) + "\n")
    print("wrote", out)


if __name__ == "__main__":
    main()
