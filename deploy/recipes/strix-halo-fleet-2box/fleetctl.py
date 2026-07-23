"""Tiny CLI the shell scripts call so they need no jq (stdlib only).

Reads CCP_URL and FLEET_TOKEN from the environment. Subcommands:

  set-desired <file>          POST a new desired config (admin "edit once")
  desired-hash                print the current desired-config sha256
  desired-version             print the current desired version
  status                      print a human fleet convergence view
  audit                       print the central audit log
  wait-converged --boxes a,b [--timeout N]
                              exit 0 when every listed box reports the desired
                              version AND hash; exit 1 on timeout

See docs/agent/plans/pl-0040-edge-fleet-config-control-plane.md.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import fleet_lib


def _env():
    url = os.environ.get("CCP_URL")
    token = os.environ.get("FLEET_TOKEN")
    if not url or not token:
        print("CCP_URL and FLEET_TOKEN must be set", file=sys.stderr)
        raise SystemExit(2)
    return url.rstrip("/"), token


def _desired(url, token):
    status, obj = fleet_lib.http_get_json(url + "/fleet/desired", token=token)
    if status != 200:
        raise SystemExit("GET /fleet/desired -> %d" % status)
    return obj


def cmd_set_desired(args):
    url, token = _env()
    with open(args.file, "r", encoding="utf-8") as fh:
        config_text = fh.read()
    status, obj = fleet_lib.http_post_json(
        url + "/fleet/desired", {"config": config_text}, token=token
    )
    if status != 200:
        raise SystemExit("set-desired -> %d %s" % (status, obj))
    print("%s %s" % (obj.get("version", "?"), obj.get("sha256", "")))


def cmd_desired_hash(_args):
    url, token = _env()
    print(_desired(url, token).get("sha256", ""))


def cmd_desired_version(_args):
    url, token = _env()
    print(_desired(url, token).get("version", ""))


def cmd_status(_args):
    url, token = _env()
    status, obj = fleet_lib.http_get_json(url + "/fleet/status", token=token)
    if status != 200:
        raise SystemExit("status -> %d" % status)
    print(
        "desired_version=%s audit_count=%s"
        % (obj.get("desired_version", ""), obj.get("audit_count", 0))
    )
    for box_id, rec in sorted(obj.get("boxes", {}).items()):
        print(
            "  %-10s version=%-4s result=%-12s hash=%s"
            % (
                box_id,
                rec.get("version", ""),
                rec.get("result", ""),
                rec.get("hash", "")[:12],
            )
        )


def cmd_audit(_args):
    url, token = _env()
    status, obj = fleet_lib.http_get_json(url + "/fleet/audit", token=token)
    if status != 200:
        raise SystemExit("audit -> %d" % status)
    for rec in obj.get("audit", []):
        print(
            "%s %-10s %-4s %-12s %s"
            % (
                rec.get("ts", ""),
                rec.get("box_id", ""),
                rec.get("version", ""),
                rec.get("result", ""),
                rec.get("reason", ""),
            )
        )


def cmd_wait_converged(args):
    url, token = _env()
    boxes = [b.strip() for b in args.boxes.split(",") if b.strip()]
    deadline = time.time() + args.timeout
    last = ""
    while time.time() < deadline:
        desired = _desired(url, token)
        want_v, want_h = desired.get("version", ""), desired.get("sha256", "")
        status, obj = fleet_lib.http_get_json(url + "/fleet/status", token=token)
        reported = obj.get("boxes", {}) if status == 200 else {}
        ok = True
        summary = []
        for b in boxes:
            rec = reported.get(b)
            good = (
                bool(rec) and rec.get("version") == want_v and rec.get("hash") == want_h
            )
            ok = ok and good
            summary.append("%s=%s" % (b, "ok" if good else "..."))
        last = "desired=%s [%s]" % (want_v, " ".join(summary))
        if ok:
            print("converged: " + last)
            return 0
        time.sleep(1.0)
    print("TIMEOUT waiting for convergence: " + last, file=sys.stderr)
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="fleetctl")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("set-desired")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_set_desired)
    sub.add_parser("desired-hash").set_defaults(fn=cmd_desired_hash)
    sub.add_parser("desired-version").set_defaults(fn=cmd_desired_version)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)
    wc = sub.add_parser("wait-converged")
    wc.add_argument("--boxes", required=True)
    wc.add_argument("--timeout", type=float, default=60.0)
    wc.set_defaults(fn=cmd_wait_converged)
    args = p.parse_args(argv)
    rc = args.fn(args)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
