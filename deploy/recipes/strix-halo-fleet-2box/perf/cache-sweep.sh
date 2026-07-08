#!/usr/bin/env bash
#
# cache-sweep.sh -- semantic-cache parameter sweep + data recording for the report.
#
# For each similarity_threshold value, it edits the RENDERED gateway config in place
# (global.stores.semantic_cache.similarity_threshold), lets the router hot-reload via
# fsnotify (same-inode write -- like the fleet agent), then drives a warm->replay
# workload of (base, paraphrase, distractor) triples through the router and records:
#   - true_hit_rate  : paraphrases (same meaning) served from cache  -> coverage/speed
#   - false_hit_rate : DISTINCT questions wrongly served a cached answer -> correctness risk
#   - ttft_miss_ms / ttft_hit_ms : the latency payoff of a hit (miss goes to the LLM)
#
# A cache HIT returns almost instantly (tens of ms); a MISS calls the backend LLM
# (>1000ms through the router), so TTFT below HIT_MS is used as the hit signal.
#
# Env (all optional):
#   ROUTER_URL        router OpenAI listener      (default http://localhost:8899/v1)
#   GATEWAY_CONFIG    rendered config to edit     (default ${FLEET_STATE_DIR}/gateway/config.yaml)
#   ROUTER_CONFIG_URL config-hash endpoint        (default http://localhost:8080/config/hash)
#   THRESHOLDS        values to sweep             (default "0.50 0.70 0.85 0.92 0.95")
#   HIT_MS            TTFT below this = cache hit  (default 300)
#   OUT               CSV output path             (default cache-sweep-<box>.csv here)
#   RESTORE           1 => restore original config at end (default 1)
#
# Usage (stack up):  bash cache-sweep.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=/dev/null
source "${RECIPE_DIR}/fleet_common.sh"
PY_BIN="$(fleet_pybin)"

ROUTER_URL="${ROUTER_URL:-http://localhost:8899/v1}"
GATEWAY_CONFIG="${GATEWAY_CONFIG:-${FLEET_STATE_DIR}/gateway/config.yaml}"
ROUTER_CONFIG_URL="${ROUTER_CONFIG_URL:-http://localhost:8080/config/hash}"
THRESHOLDS="${THRESHOLDS:-0.50 0.70 0.85 0.92 0.95}"
HIT_MS="${HIT_MS:-300}"
RESTORE="${RESTORE:-1}"
BOX="${BOX:-$(hostname 2>/dev/null || echo box)}"
OUT="${OUT:-${SCRIPT_DIR}/cache-sweep-${BOX}.csv}"

[[ -f "${GATEWAY_CONFIG}" ]] || { echo "ERROR: gateway config not found: ${GATEWAY_CONFIG}" >&2; exit 1; }
if ! curl -fsS "${ROUTER_CONFIG_URL}" >/dev/null 2>&1; then
  echo "ERROR: router not answering at ${ROUTER_CONFIG_URL}; bring the gateway up first." >&2
  exit 1
fi
cp -f "${GATEWAY_CONFIG}" "${GATEWAY_CONFIG}.cachebak"

echo "==> [cache-sweep] box=${BOX}  thresholds='${THRESHOLDS}'  out=${OUT}"
"${PY_BIN}" - "${GATEWAY_CONFIG}" "${ROUTER_URL}" "${ROUTER_CONFIG_URL}" "${OUT}" "${HIT_MS}" "${THRESHOLDS}" <<'PYEOF'
import json, statistics, sys, time, urllib.request, urllib.error

CFG, ROUTER, CONFIGH, OUT, HIT_MS, THRESHOLDS = sys.argv[1:7]
HIT_MS = float(HIT_MS)
CHAT = ROUTER.rstrip("/") + "/chat/completions"

# (base, paraphrase = same meaning -> SHOULD hit, distractor = different answer -> should NOT hit)
CASES = [
    ("What is the capital of France?", "Which city is France's capital?", "What is the capital of Germany?"),
    ("Explain what an API is.", "Briefly describe what an API is.", "Explain what an IP address is."),
    ("What is 2 plus 2?", "Compute two plus two.", "What is 2 times 3?"),
    ("How does TCP differ from UDP?", "Difference between TCP and UDP?", "How does HTTP differ from HTTPS?"),
    ("Give a short summary of relativity.", "Summarize the theory of relativity briefly.", "Give a short summary of quantum mechanics."),
    ("What is the capital of Japan?", "Which city is Japan's capital?", "What is the capital of China?"),
]


def set_threshold(t):
    """Set global.stores.semantic_cache.similarity_threshold in place (same inode -> hot-reload)."""
    lines = open(CFG, "r", encoding="utf-8").read().split("\n")
    out, ins, ind0, done = [], False, None, False
    for l in lines:
        s = l.strip()
        if s == "semantic_cache:":
            ins, ind0 = True, len(l) - len(l.lstrip())
            out.append(l); continue
        if ins:
            ind = len(l) - len(l.lstrip())
            if s and ind <= ind0:
                if not done:
                    out.append(" " * (ind0 + 4) + "similarity_threshold: %s" % t); done = True
                ins = False
            elif s.startswith("similarity_threshold:"):
                out.append(" " * ind + "similarity_threshold: %s" % t); done = True; continue
        out.append(l)
    if ins and not done:
        out.append(" " * (ind0 + 4) + "similarity_threshold: %s" % t)
    with open(CFG, "w", encoding="utf-8") as fh:   # truncate-write same path -> same inode
        fh.write("\n".join(out))


def chash():
    try:
        return urllib.request.urlopen(CONFIGH, timeout=5).read()
    except Exception:
        return b""


def ask(q):
    # Returns (ttft_ms, hit_bool, similarity). A cache HIT is detected from the
    # router's x-vsr-cache-hit response header (set on the immediate cached
    # response in utils/http/response.go CreateCacheHitResponse) -- deterministic,
    # unlike a latency guess, because a hit still pays the embedding cost.
    body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": q}], "max_tokens": 16}).encode()
    req = urllib.request.Request(CHAT, data=body, headers={"Content-Type": "application/json", "x-vsr-debug": "true"})
    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        resp.read()
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in (list(e.headers.items()) if e.headers else [])}
        try:
            e.read()
        except Exception:
            pass
    except Exception:
        return 999999.0, False, None
    ttft = (time.perf_counter() - t0) * 1000.0
    hit = str(hdrs.get("x-vsr-cache-hit", "")).lower() == "true"
    sim = hdrs.get("x-vsr-cache-similarity")
    return ttft, hit, (float(sim) if sim else None)


with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("threshold,true_hit_rate,false_hit_rate,ttft_miss_ms,ttft_hit_ms\n")

for t in THRESHOLDS.split():
    h0 = chash(); set_threshold(t)
    for _ in range(40):                       # wait for the hot-reload to land
        if chash() != h0:
            break
        time.sleep(0.5)
    time.sleep(3)                             # settle
    miss, hits, tp, fp = [], [], 0, 0
    for i, (base, para, dist) in enumerate(CASES):
        salt = " (run %s.%d)" % (t, i)        # per-threshold namespace -> base is always a fresh miss
        tp0, _, _ = ask(base + salt)          # populate (first-ever = miss)
        miss.append(tp0)
        thit, hit, _ = ask(para + salt)       # paraphrase SHOULD hit (semantic match)
        if hit:
            tp += 1; hits.append(thit)
        _, dhit, _ = ask(dist + salt)         # distractor should NOT hit -> a hit here is WRONG
        if dhit:
            fp += 1
    n = len(CASES)
    row = "%.2f,%.2f,%.2f,%.0f,%s" % (
        float(t), tp / n, fp / n, statistics.mean(miss),
        "%.0f" % statistics.mean(hits) if hits else "-")
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(row + "\n")
    print("threshold=%.2f  true_hit=%.0f%%  false_hit=%.0f%%  miss=%.0fms  hit=%sms" % (
        float(t), tp / n * 100, fp / n * 100, statistics.mean(miss),
        "%.0f" % statistics.mean(hits) if hits else "-"))
PYEOF

if [[ "${RESTORE}" == "1" ]]; then
  cp -f "${GATEWAY_CONFIG}.cachebak" "${GATEWAY_CONFIG}"     # restore + let it reload back
  echo "==> [cache-sweep] restored original config"
fi
echo "==> [cache-sweep] done -> ${OUT}"
cat "${OUT}"
