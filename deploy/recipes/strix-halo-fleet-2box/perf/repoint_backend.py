#!/usr/bin/env python3
"""Repoint one model card's backend in a rendered gateway config, in place.

Used by server-bench.sh's optional through-router path: it rewrites the
``endpoint`` and ``external_model_ids.vllm`` of a single model card so the router
sends that alias to a different inference server, then relies on the router's
fsnotify hot-reload to pick it up.

CRITICAL: it truncate-writes the SAME file/inode (open ``"w"``), never a
temp-file rename -- the real vllm-sr router bind-mounts the config as a single
file and pins the inode, so an atomic rename would swap in an inode the container
never sees and no hot-reload would fire (see fleet_agent._write_config and the
recipe README's reload-mechanism note). Stdlib only (no PyYAML dependency).

The two ``- name: <alias>`` occurrences in a canonical config (one under
``providers.models`` with backends, one under ``routing.modelCards`` without) are
disambiguated by requiring the block to actually contain an ``endpoint:``.

Usage:
  python3 repoint_backend.py --config gateway/config.yaml \
      --alias google/gemini-2.5-flash-lite --endpoint llama-server:8080 --model qwen2.5-7b
"""

from __future__ import annotations

import argparse
import re
import sys


def repoint(lines, alias, endpoint, model):
    """Return (new_lines, changed_bool). Edits the first backend-bearing block."""
    name_re = re.compile(r"^(\s*)-\s+name:\s*" + re.escape(alias) + r"\s*$")
    next_item_re = re.compile(r"^\s*-\s+name:")
    n = len(lines)
    for i, line in enumerate(lines):
        m = name_re.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        # Extent of this list item: until the next same-or-shallower '- name:'.
        j = i + 1
        has_endpoint = False
        while j < n:
            lj = lines[j]
            if (
                lj.strip()
                and next_item_re.match(lj)
                and (len(lj) - len(lj.lstrip())) <= indent
            ):
                break
            if re.match(r"^\s*endpoint:\s*", lj):
                has_endpoint = True
            j += 1
        if not has_endpoint:
            continue  # this is the routing.modelCards block, not a backend
        ep_done = vllm_done = False
        for k in range(i, j):
            ep = re.match(r"^(\s*endpoint:\s*).*$", lines[k])
            if ep and not ep_done:
                lines[k] = ep.group(1) + endpoint + "\n"
                ep_done = True
            vl = re.match(r"^(\s*vllm:\s*).*$", lines[k])
            if vl and not vllm_done:
                lines[k] = vl.group(1) + model + "\n"
                vllm_done = True
        return lines, ep_done
    return lines, False


def main(argv=None):
    p = argparse.ArgumentParser(prog="repoint_backend", description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--alias", required=True, help="model card name to repoint")
    p.add_argument("--endpoint", required=True, help="new backend host:port")
    p.add_argument("--model", required=True, help="new external_model_ids.vllm value")
    args = p.parse_args(argv)

    with open(args.config, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    new_lines, changed = repoint(lines, args.alias, args.endpoint, args.model)
    if not changed:
        print(
            "ERROR: no backend-bearing model card named %r found" % args.alias,
            file=sys.stderr,
        )
        return 1
    # In-place truncate write -- preserve the inode for fsnotify hot-reload.
    with open(args.config, "w", encoding="utf-8") as fh:
        fh.writelines(new_lines)
    print("repointed %s -> %s (%s)" % (args.alias, args.endpoint, args.model))
    return 0


if __name__ == "__main__":
    sys.exit(main())
