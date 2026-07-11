#!/usr/bin/env bash
#
# cache-sweep.sh -- semantic-cache parameter sweep + data recording for the report.
#
# For each similarity_threshold value, it edits the RENDERED gateway config in
# place, lets the router hot-reload, then drives a warm->replay workload of
# (base, paraphrase, distractor) triples through the router and records:
#   - true_hit_rate  : paraphrases (same meaning) served from cache  -> coverage/speed
#   - false_hit_rate : DISTINCT questions wrongly served a cached answer -> correctness risk
#   - ttft_miss_ms / ttft_hit_ms : the latency payoff of a hit (miss goes to the LLM)
#
# A cache HIT is detected deterministically from the router's x-vsr-cache-hit
# response header (utils/http/response.go CreateCacheHitResponse), not a latency
# guess, because a hit still pays the embedding cost.
#
# TWO things are required for hits to register, both handled here:
#   1) TARGET THE FILE THE ROUTER ACTUALLY WATCHES. start-router.sh launches the
#      router with -config=<gateway>/.vllm-sr/runtime-config.yaml (the compiled
#      runtime config), and its fsnotify watch is on THAT file's directory. The
#      human-authored config.yaml is a separate source file; /config/hash hashes
#      config.yaml, so editing it changes the hash but NEVER reloads the router.
#      We therefore edit runtime-config.yaml (same inode) and confirm the reload
#      from the router's own "config_reloaded" log line.
#   2) ENABLE ROUTE-LOCAL CACHING. With routing decisions configured, the global
#      stores.semantic_cache toggle is ignored -- the cache only runs when the
#      MATCHED decision carries a semantic-cache plugin (IsCacheEnabledForDecision
#      / semanticCacheEnabledForScope). We inject `- type: semantic-cache
#      (enabled: true)` into every decision's plugins list; the effective
#      threshold then falls back to the global similarity_threshold we sweep.
#
# Env (all optional):
#   ROUTER_URL        router OpenAI listener      (default http://localhost:8899/v1)
#   GATEWAY_CONFIG    config the router watches    (default <gateway>/.vllm-sr/runtime-config.yaml)
#   ROUTER_CONFIG_URL router liveness endpoint     (default http://localhost:8080/config/hash)
#   ROUTER_CONTAINER  router container for reload detection (default vllm-sr-router-container)
#   THRESHOLDS        values to sweep             (default "0.50 0.70 0.85 0.92 0.95")
#   HIT_MS            (accepted for compat; hit is header-based) (default 300)
#   RELOAD_TIMEOUT    max seconds to wait per reload (default 45)
#   RELOAD_SETTLE     seconds to settle after reload (default 3)
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
ROUTER_CONFIG_URL="${ROUTER_CONFIG_URL:-http://localhost:8080/config/hash}"
ROUTER_CONTAINER="${ROUTER_CONTAINER:-vllm-sr-router-container}"
THRESHOLDS="${THRESHOLDS:-0.50 0.70 0.85 0.92 0.95}"
HIT_MS="${HIT_MS:-300}"
RELOAD_TIMEOUT="${RELOAD_TIMEOUT:-45}"
RELOAD_SETTLE="${RELOAD_SETTLE:-3}"
RESTORE="${RESTORE:-1}"
BOX="${BOX:-$(hostname 2>/dev/null || echo box)}"
OUT="${OUT:-${SCRIPT_DIR}/cache-sweep-${BOX}.csv}"

# Resolve the config the router actually loads + watches (see header note #1).
GATEWAY_DIR="${GATEWAY_DIR:-${FLEET_STATE_DIR}/gateway}"
if [[ -z "${GATEWAY_CONFIG:-}" ]]; then
  if [[ -f "${GATEWAY_DIR}/.vllm-sr/runtime-config.yaml" ]]; then
    GATEWAY_CONFIG="${GATEWAY_DIR}/.vllm-sr/runtime-config.yaml"
  else
    GATEWAY_CONFIG="${GATEWAY_DIR}/config.yaml"
  fi
fi

[[ -f "${GATEWAY_CONFIG}" ]] || { echo "ERROR: gateway config not found: ${GATEWAY_CONFIG}" >&2; exit 1; }
if ! curl -fsS "${ROUTER_CONFIG_URL}" >/dev/null 2>&1; then
  echo "ERROR: router not answering at ${ROUTER_CONFIG_URL}; bring the gateway up first." >&2
  exit 1
fi
cp -f "${GATEWAY_CONFIG}" "${GATEWAY_CONFIG}.cachebak"

echo "==> [cache-sweep] box=${BOX}  config=${GATEWAY_CONFIG}  thresholds='${THRESHOLDS}'  out=${OUT}"
"${PY_BIN}" - "${GATEWAY_CONFIG}" "${ROUTER_URL}" "${OUT}" "${HIT_MS}" "${THRESHOLDS}" \
  "${ROUTER_CONTAINER}" "${RELOAD_TIMEOUT}" "${RELOAD_SETTLE}" "${RESTORE}" <<'PYEOF'
import json, statistics, subprocess, sys, time, urllib.request, urllib.error

CFG, ROUTER, OUT, HIT_MS, THRESHOLDS, CTR, RELOAD_TIMEOUT, RELOAD_SETTLE, RESTORE = sys.argv[1:10]
HIT_MS = float(HIT_MS)  # accepted for compatibility; hit detection is header-based
RELOAD_TIMEOUT = float(RELOAD_TIMEOUT)
RELOAD_SETTLE = float(RELOAD_SETTLE)
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

ORIG = open(CFG, "r", encoding="utf-8").read()


def indent_of(line):
    return len(line) - len(line.lstrip())


def inject_cache_plugin(text):
    """Enable route-local semantic caching by adding a semantic-cache plugin
    (enabled: true) to every decision's plugins list. Idempotent."""
    if "- type: semantic-cache" in text:
        return text
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        if line.strip() == "plugins:":
            item = indent_of(line)
            j = i + 1
            while j < len(lines):
                s = lines[j].strip()
                if s.startswith("- "):
                    item = indent_of(lines[j]); break
                if s:
                    break
                j += 1
            out.append(" " * item + "- type: semantic-cache")
            out.append(" " * (item + 2) + "configuration:")
            out.append(" " * (item + 4) + "enabled: true")
        i += 1
    return "\n".join(out)


def set_threshold(text, t):
    """Set stores.semantic_cache.similarity_threshold in place, detecting the
    block's child indent (runtime config uses a 2-space step, config.yaml a 4)."""
    out, ins, ind0, child, done = [], False, None, None, False
    for line in text.split("\n"):
        s = line.strip()
        if s == "semantic_cache:":
            ins, ind0, child, done = True, indent_of(line), None, False
            out.append(line); continue
        if ins:
            ind = indent_of(line)
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


_DOCKER = None


def docker_ok():
    global _DOCKER
    if _DOCKER is None:
        try:
            r = subprocess.run(["docker", "inspect", CTR], capture_output=True, timeout=15)
            _DOCKER = (r.returncode == 0)
        except Exception:
            _DOCKER = False
    return _DOCKER


def reloaded_since(epoch):
    try:
        r = subprocess.run(["docker", "logs", "--since", str(int(epoch)), CTR],
                           capture_output=True, text=True, timeout=20)
        return '"config_reloaded"' in (r.stdout + r.stderr)
    except Exception:
        return False


def apply_config(text):
    """Truncate-write the same path (same inode -> fsnotify fires) and wait for
    the router to finish reloading before returning."""
    since = time.time()
    with open(CFG, "w", encoding="utf-8") as fh:
        fh.write(text)
    if docker_ok():
        deadline = time.time() + RELOAD_TIMEOUT
        while time.time() < deadline:
            if reloaded_since(since):
                break
            time.sleep(1)
    else:
        time.sleep(RELOAD_TIMEOUT)  # no docker access: wait the full budget
    time.sleep(RELOAD_SETTLE)       # let the reloaded router re-init the cache


def ask(q):
    # Returns (ttft_ms, hit_bool, similarity) from the router's x-vsr-cache-hit
    # / x-vsr-cache-similarity response headers.
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
    # Enable route-local caching (once) and set this threshold in a single
    # write so the router reloads exactly once per step. The reload also clears
    # the in-memory cache, so every threshold starts from an empty cache.
    text = inject_cache_plugin(open(CFG, "r", encoding="utf-8").read())
    text = set_threshold(text, t)
    apply_config(text)
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

if RESTORE == "1":
    apply_config(ORIG)
    print("restored original config")
PYEOF

echo "==> [cache-sweep] done -> ${OUT}"
cat "${OUT}"
