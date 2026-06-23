#!/usr/bin/env python3
"""PII-masking demo for the Strix Halo PoC classification API.

This calls the router's classification API endpoint ``POST /api/v1/classify/pii``
on port ``:8080`` (NOT the routing listener ``:8899``) with
``options.mask_entities: true`` and prints the returned ``masked_text`` plus the
per-entity ``masked_value`` placeholders.

This is the DATA-GOVERNANCE path (Orion / Sentinel alignment): it transforms
content by replacing sensitive spans with ``[<TYPE>_<index>]`` placeholders. It
does NOT route or block. The routing-path ``security_guard`` decision (the
``pii`` signal on listener ``:8899``) only DENIES a request via ``fast_response``
and never masks. There is no inline PII masking in the routing path and no
dashboard UI for masking; this endpoint is the only place masking lives.

It uses only the Python standard library (urllib), so no third-party packages
are required.

Usage:
    python pii_mask_demo.py [--base-url http://localhost:8080] [--text "..."]
    # via the dashboard proxy:
    python pii_mask_demo.py --base-url http://localhost:8700/api/router
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

PII_PATH = "/api/v1/classify/pii"

DEFAULT_TEXT = (
    "My name is Jane Doe, my SSN is 123-45-6789 and my email is "
    "jane.doe@example.com."
)


def post_pii(base_url, text):
    """POST one text to the PII classify endpoint. Returns the parsed JSON."""
    url = base_url.rstrip("/") + PII_PATH
    payload = {
        "text": text,
        "options": {
            "mask_entities": True,
            "reveal_entity_text": True,
            "return_positions": True,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def summarize(text, result):
    print("=" * 72)
    print("PII-masking demo (data-governance path, /api/v1/classify/pii)")
    print("  input text   : %s" % text)
    print("  has_pii      : %s" % result.get("has_pii"))
    print("  recommendation: %s" % result.get("security_recommendation"))
    print("  masked_text  : %s" % result.get("masked_text", "(none)"))
    entities = result.get("entities") or []
    if entities:
        print("  entities:")
        for ent in entities:
            print(
                "    %-16s value=%-24s -> masked_value=%s"
                % (
                    ent.get("type", "?"),
                    ent.get("value", "?"),
                    ent.get("masked_value", "?"),
                )
            )
    else:
        print("  entities: (none detected)")


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://localhost:8080",
        help=(
            "classification API base URL (default: http://localhost:8080). "
            "For the dashboard proxy use http://localhost:8700/api/router"
        ),
    )
    parser.add_argument(
        "--text",
        default=DEFAULT_TEXT,
        help="text to scan and mask (default: a sample with PERSON/SSN/EMAIL)",
    )
    args = parser.parse_args(argv[1:])

    try:
        result = post_pii(args.base_url, args.text)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        print(
            "CONNECTION ERROR: could not reach %s%s (%s).\n"
            "Is the router running? The classification API is on :8080 "
            "(not the :8899 listener)." % (args.base_url, PII_PATH, reason)
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print("UNEXPECTED ERROR: %s" % exc)
        return 1

    summarize(args.text, result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
