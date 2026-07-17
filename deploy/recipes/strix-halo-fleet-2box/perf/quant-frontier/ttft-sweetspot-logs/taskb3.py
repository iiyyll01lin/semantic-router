#!/usr/bin/env python3
"""Task B (llama.cpp-specific), robust variant.

Repoints the router's default_model (qwen/qwen3.5-rocm -> fast_qa) at the live
llama-server (llama.cpp resident gpt-oss-120b, --parallel 8), enables route-local
semantic caching @ 0.92, then measures repeat-query TTFT: miss -> exact-repeat-hit
-> semantic-0.92-hit, THROUGH the router path. Differs from the stock
bestcfg_matrix cache-overlay in two deliberate ways so a *fast* backend is measured
correctly:
  * max_tokens large enough that gpt-oss-120b (a reasoning model) emits real
    content, not just reasoning_content -> a cacheable answer.
  * a realistic inter-arrival DELAY before the repeat so the router's post-response
    cache store completes (the stock overlay fires repeats back-to-back, which a
    ~0.6 s llama.cpp response can beat -> spurious 0% hit; the ollama/qwen path only
    "worked" because its 25 s cold-load hid the race).

Reuses the vetted config-edit mechanics from bestcfg_matrix.py + repoint_backend.py.
try/finally ALWAYS restores the original runtime-config (same inode -> hot-reload).
Stdlib only.
"""

import json
import statistics
import sys
import time
import urllib.error
import urllib.request

PERF = "/home/test001/gemma-bench/strix-halo-fleet-2box/perf"
sys.path.insert(0, PERF)
import bestcfg_matrix as b  # noqa: E402
import repoint_backend as rp  # noqa: E402

CFG = "/tmp/vllm-sr-fleet/gateway/.vllm-sr/runtime-config.yaml"
CHAT = "http://localhost:8899/v1/chat/completions"
HASH_URL = "http://localhost:8080/config/hash"
CTR = "vllm-sr-router-container"
ALIAS = "qwen/qwen3.5-rocm"
ENDPOINT = "llama-server:8080"
MODEL = "ggml-org/gpt-oss-120b-GGUF"
THRESHOLD = "0.92"
MAX_TOKENS = 64
DELAY = 3.0
OUT = "/home/test001/ttft-sweetspot/cache-llamacpp.json"

CASES = [
    ("What is the capital of France?", "Which city is France's capital?"),
    ("Explain what an API is.", "Briefly describe what an API is."),
    ("How does TCP differ from UDP?", "What is the difference between TCP and UDP?"),
]


def ask(question):
    """One router chat request; return (ttft_ms, hit_bool, similarity, selected_model)."""
    body = json.dumps(
        {
            "model": "auto",
            "messages": [{"role": "user", "content": question}],
            "max_tokens": MAX_TOKENS,
        }
    ).encode()
    req = urllib.request.Request(
        CHAT,
        data=body,
        headers={"Content-Type": "application/json", "x-vsr-debug": "true"},
    )
    t0 = time.perf_counter()
    hdrs = {}
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        resp.read()
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in (list(e.headers.items()) if e.headers else [])}
        try:
            e.read()
        except OSError:
            pass
    except (urllib.error.URLError, OSError) as e:
        return None, False, None, "ERR:%s" % e
    ttft = (time.perf_counter() - t0) * 1000.0
    hit = str(hdrs.get("x-vsr-cache-hit", "")).lower() == "true"
    sim = hdrs.get("x-vsr-cache-similarity")
    return (
        ttft,
        hit,
        (float(sim) if sim else None),
        hdrs.get("x-vsr-selected-model", "?"),
    )


def hash_now():
    try:
        return urllib.request.urlopen(HASH_URL, timeout=10).read().decode()
    except OSError:
        return "?"


def main():
    orig = open(CFG, "r", encoding="utf-8").read()
    log = []
    try:
        # 1) repoint default_model -> llama-server, 2) enable cache @ threshold.
        lines, changed = rp.repoint(
            orig.splitlines(keepends=True), ALIAS, ENDPOINT, MODEL
        )
        if not changed:
            print("ERROR: repoint of %s failed" % ALIAS)
            return 1
        text = b.set_threshold(b.inject_cache_plugin("".join(lines)), THRESHOLD)
        b.apply_config(CFG, text, CTR, 45, 3)
        print(
            "applied: repoint %s->%s + cache@%s (hash %s)"
            % (ALIAS, ENDPOINT, THRESHOLD, hash_now())
        )

        miss, exact, sem, sel_models = [], [], [], set()
        exact_n = sem_n = 0
        for i, (base, para) in enumerate(CASES):
            q = base + " (taskb3 %d)" % i
            m, mh, _, msel = ask(q)  # populate (miss)
            sel_models.add(msel)
            time.sleep(DELAY)  # let post-response store finish
            te, he, se_, _ = ask(q)  # exact repeat -> expect hit
            time.sleep(0.4)
            tp, hp, sp, _ = ask(para + " (taskb3 %d)" % i)  # paraphrase -> semantic hit
            rec = {
                "case": i,
                "sel_model": msel,
                "miss_ms": m,
                "miss_was_hit": mh,
                "exact_ms": te,
                "exact_hit": he,
                "exact_sim": se_,
                "sem_ms": tp,
                "sem_hit": hp,
                "sem_sim": sp,
            }
            log.append(rec)
            print(json.dumps(rec))
            if m is not None and not mh:
                miss.append(m)
            if he and te is not None:
                exact.append(te)
                exact_n += 1
            if hp and tp is not None:
                sem.append(tp)
                sem_n += 1

        out = {
            "schema": "bestcfg-cache-overlay/v1",
            "server": "llamacpp",
            "cell_id": "llamacpp-resident-p8",
            "backend": "llama.cpp llama-server ROCm gpt-oss-120b MXFP4 (resident -ngl 999, --parallel 8)",
            "routed_via_alias": ALIAS,
            "selected_models_seen": sorted(sel_models),
            "threshold": THRESHOLD,
            "max_tokens": MAX_TOKENS,
            "repeat_delay_s": DELAY,
            "method_note": (
                "miss->exact-repeat-hit->semantic-0.92-hit through the router; "
                "default_model repointed at llama-server so misses are served by "
                "llama.cpp; %.1fs inter-arrival so the async cache store completes."
                % DELAY
            ),
            "ttft_miss_ms": statistics.mean(miss) if miss else None,
            "ttft_hit_exact_ms": statistics.mean(exact) if exact else None,
            "ttft_hit_semantic_ms": statistics.mean(sem) if sem else None,
            "ttft_saved_ms": (
                (statistics.mean(miss) - statistics.mean(exact))
                if (miss and exact)
                else None
            ),
            "exact_hit_rate": exact_n / len(CASES),
            "semantic_hit_rate": sem_n / len(CASES),
            "cases": len(CASES),
            "per_case": log,
        }
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("WROTE " + OUT)
        print(
            json.dumps(
                {
                    k: out[k]
                    for k in (
                        "ttft_miss_ms",
                        "ttft_hit_exact_ms",
                        "ttft_hit_semantic_ms",
                        "ttft_saved_ms",
                        "exact_hit_rate",
                        "semantic_hit_rate",
                    )
                },
                indent=2,
            )
        )
    finally:
        b.apply_config(CFG, orig, CTR, 45, 3)  # ALWAYS restore original config
        inj = "- type: semantic-cache" in open(CFG, "r", encoding="utf-8").read()
        print(
            "RESTORED original config; hash=%s cache_injection_present=%s"
            % (hash_now(), inj)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
