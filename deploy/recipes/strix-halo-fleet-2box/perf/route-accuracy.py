#!/usr/bin/env python3
"""route-accuracy.py -- routing-accuracy baseline / guardrail for the live router.

Standalone (stdlib-only) counterpart of the k8s e2e tests
`e2e/testcases/domain_classify.go` (per-category domain accuracy, checks the
selected category) and `model_selection.go` (checks the selected
decision/model). Those tests require a Kubernetes cluster + port-forward and a
test-framework runner, and their assertions assume the older category-routing
config (header `x-vsr-selected-category`, model `MoM`). This box is a
Docker/offline decisions-config PoC where `MoM` 503s and category headers are
not emitted, and -- crucially -- questions that route to a CLOUD tier
(premium_legal, reasoning_deep, ...) return a bare Envoy 503 with NO x-vsr
headers, so the live `:8899` data-path cannot report routing for ~half the
corpus.

So we score against the router's own classification API on `:8080`
(`/api/v1/classify/intent`), which is the SAME decision engine the `:8899`
data-path uses but is upstream-independent (no 503). Equivalence was verified by
cross-checking locally-routed queries: intent.routing_decision /
recommended_model == live :8899 x-vsr-selected-decision / -model.

Reuses the e2e corpus verbatim: e2e/testcases/testdata/domain_classify_cases.json
(261 labeled MMLU-style cases across 14 domains).

Records, per expected category:
  - domain_accuracy : matched_signals.domains contains the expected domain label
  - decision distribution (routing_decision)  <- model_selection.go dimension
  - model distribution    (recommended_model) <- model_selection.go dimension
Writes a JSON baseline (default perf/route-accuracy-<box>.json) that step 3
(head-trim) re-runs and diffs to prove no routing regression.

Env / args:
  BASE_URL   router classification API base (default http://localhost:8080)
  OUT        JSON baseline output path (default perf/route-accuracy-<box>.json)
  CASES      path to the e2e corpus (default resolves repo e2e testdata)
  LABEL      tag stored in the JSON (e.g. "baseline" or "post-head-trim")
Usage: python3 route-accuracy.py [LABEL]
"""
import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
INTENT_URL = BASE_URL + "/api/v1/classify/intent"
BOX = os.environ.get("BOX", socket.gethostname() or "box")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# repo root = .../deploy/recipes/strix-halo-fleet-2box/perf -> up 4
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
CASES = os.environ.get("CASES", os.path.join(REPO_ROOT, "e2e/testcases/testdata/domain_classify_cases.json"))
LABEL = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LABEL", "baseline"))
OUT = os.environ.get("OUT", os.path.join(SCRIPT_DIR, "route-accuracy-%s.json" % BOX))


def norm(s):
    return str(s).strip().lower().replace("_", " ")


def classify_intent(text):
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(INTENT_URL, data=body, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=120)
    return json.load(resp)


def main():
    cases = json.load(open(CASES, encoding="utf-8"))
    print("route-accuracy: label=%s  base=%s  cases=%d  corpus=%s" % (LABEL, BASE_URL, len(cases), CASES))

    per_cat = defaultdict(lambda: {
        "n": 0, "domain_correct": 0,
        "decisions": defaultdict(int), "models": defaultdict(int),
        "errors": 0,
    })
    records = []
    overall_correct = 0
    overall_n = 0
    t0 = time.time()

    for i, c in enumerate(cases):
        expected = c["category"]
        q = c["question"]
        st = per_cat[expected]
        st["n"] += 1
        overall_n += 1
        try:
            d = classify_intent(q)
        except Exception as e:
            st["errors"] += 1
            records.append({"category": expected, "question": q, "error": str(e)[:200]})
            continue
        domains = [norm(x) for x in (d.get("matched_signals", {}).get("domains") or [])]
        decision = d.get("routing_decision")
        model = d.get("recommended_model")
        conf = d.get("classification", {}).get("confidence")
        correct = norm(expected) in domains
        if correct:
            st["domain_correct"] += 1
            overall_correct += 1
        st["decisions"][decision] += 1
        st["models"][model] += 1
        records.append({
            "category": expected, "question": q, "domain_correct": correct,
            "matched_domains": domains, "routing_decision": decision,
            "recommended_model": model, "confidence": conf,
        })
        if (i + 1) % 25 == 0:
            print("  %d/%d  (%.0fs)" % (i + 1, len(cases), time.time() - t0))

    # Build summary
    summary = {}
    for cat, st in sorted(per_cat.items()):
        n = st["n"]
        summary[cat] = {
            "n": n,
            "domain_accuracy": round(st["domain_correct"] / n, 4) if n else None,
            "domain_correct": st["domain_correct"],
            "errors": st["errors"],
            "decisions": dict(sorted(st["decisions"].items(), key=lambda kv: -kv[1])),
            "models": dict(sorted(st["models"].items(), key=lambda kv: -kv[1])),
        }
    overall_acc = round(overall_correct / overall_n, 4) if overall_n else None
    # Decision + model global distribution
    dec_dist = defaultdict(int)
    mod_dist = defaultdict(int)
    for r in records:
        if "routing_decision" in r:
            dec_dist[r["routing_decision"]] += 1
            mod_dist[r["recommended_model"]] += 1

    out = {
        "label": LABEL,
        "base_url": BASE_URL,
        "box": BOX,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "corpus": os.path.relpath(CASES, REPO_ROOT),
        "n_cases": overall_n,
        "overall_domain_accuracy": overall_acc,
        "decision_distribution": dict(sorted(dec_dist.items(), key=lambda kv: -kv[1])),
        "model_distribution": dict(sorted(mod_dist.items(), key=lambda kv: -kv[1])),
        "per_category": summary,
        "records": records,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)

    # Print readable summary
    print("\n==== ROUTING-ACCURACY BASELINE (%s) ====" % LABEL)
    print("overall domain accuracy: %.1f%%  (%d/%d)" % (overall_acc * 100, overall_correct, overall_n))
    print("\nper-category domain accuracy + dominant decision -> model:")
    print("  %-18s %6s  %-8s  %-26s %s" % ("category", "acc", "n", "top_decision", "top_model"))
    for cat, s in summary.items():
        top_dec = next(iter(s["decisions"]), "-")
        top_mod = next(iter(s["models"]), "-")
        print("  %-18s %5.0f%%  %-8d  %-26s %s" % (cat, (s["domain_accuracy"] or 0) * 100, s["n"], top_dec, top_mod))
    print("\ndecision distribution:", dict(list(out["decision_distribution"].items())))
    print("model distribution   :", dict(list(out["model_distribution"].items())))
    print("\nwrote %s" % OUT)


if __name__ == "__main__":
    main()
