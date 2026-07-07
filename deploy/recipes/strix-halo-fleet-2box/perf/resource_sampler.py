#!/usr/bin/env python3
"""Resource sampler for the Strix Halo perf benchmarks (stdlib only).

Samples the three things that matter when a vllm-sr stack shares a **unified
memory** APU (Ryzen AI Max+ 395, gfx1151) with an inference server:

  * GPU: busy %, VRAM used/total, and GTT used/total (rocm-smi, else amd-smi).
    On an APU the "VRAM" carveout + GTT both come out of the same LPDDR5X pool,
    so GTT growth is the tell-tale of spilling past the GPU carveout.
  * Host memory: total / available / used from /proc/meminfo (locale-proof).
    ``mem_total_b`` IS the unified-memory budget the max-model question needs.
  * Per-container CPU% and memory (docker stats) -- so the router/Envoy/dashboard
    footprint can be attributed separately from the inference server.

Subcommands:
  snapshot   one sample -> stdout or --out
  start      launch a background sampling loop, write NDJSON, record a pidfile
  stop       stop the loop (SIGTERM) and, if --in/--out given, summarize it
  summarize  reduce an NDJSON timeseries to min/mean/max/peak JSON

Everything degrades gracefully: a missing rocm-smi/amd-smi/docker just leaves
those fields null instead of failing, so the same sampler runs on any box.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess  # noqa: S404 (fixed, local diagnostic commands only)
import sys
import time


def _run(cmd, timeout=8):
    """Run a command, return stdout text ('' on any failure)."""
    try:
        out = subprocess.run(  # noqa: S603 (fixed argv, no shell)
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return out.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _first_num(d, *needles):
    """Return the first value whose key contains ALL needles (case-insensitive)."""
    for key, val in d.items():
        low = key.lower()
        if all(n in low for n in needles):
            try:
                return float(str(val).strip().rstrip("%"))
            except (TypeError, ValueError):
                continue
    return None


def sample_gpu():
    """GPU busy % + VRAM/GTT bytes. rocm-smi JSON first, amd-smi fallback."""
    out = _run(["rocm-smi", "--showuse", "--showmeminfo", "vram", "gtt", "--json"])
    if out.strip():
        try:
            data = json.loads(out)
        except ValueError:
            data = {}
        for card, fields in sorted(data.items()):
            if not isinstance(fields, dict) or not card.lower().startswith("card"):
                continue
            return {
                "busy_pct": _first_num(fields, "gpu", "use"),
                "vram_used_b": _first_num(fields, "vram", "used"),
                "vram_total_b": _first_num(fields, "vram", "total"),
                "gtt_used_b": _first_num(fields, "gtt", "used"),
                "gtt_total_b": _first_num(fields, "gtt", "total"),
                "source": "rocm-smi",
            }
    # amd-smi fallback (best effort; field names differ across versions).
    out = _run(["amd-smi", "metric", "--mem-usage", "--json"])
    if out.strip():
        try:
            data = json.loads(out)
        except ValueError:
            data = []
        rec = data[0] if isinstance(data, list) and data else data
        if isinstance(rec, dict):
            mem = rec.get("mem_usage", rec)
            return {
                "busy_pct": None,
                "vram_used_b": _first_num(mem, "used"),
                "vram_total_b": _first_num(mem, "total"),
                "gtt_used_b": None,
                "gtt_total_b": None,
                "source": "amd-smi",
            }
    return {
        "busy_pct": None,
        "vram_used_b": None,
        "vram_total_b": None,
        "gtt_used_b": None,
        "gtt_total_b": None,
        "source": None,
    }


def sample_host_mem():
    """Total/available/used host memory in bytes from /proc/meminfo."""
    info = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    info[key.strip()] = int(parts[0]) * 1024  # kB -> B
    except OSError:
        return {"mem_total_b": None, "mem_available_b": None, "mem_used_b": None}
    total = info.get("MemTotal")
    avail = info.get("MemAvailable")
    used = (total - avail) if (total is not None and avail is not None) else None
    return {"mem_total_b": total, "mem_available_b": avail, "mem_used_b": used}


def _parse_bytes(text):
    """'1.5GiB' / '512MiB' / '900MB' -> bytes."""
    text = text.strip()
    units = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
             "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
    num = ""
    for i, ch in enumerate(text):
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            unit = text[i:].strip().upper()
            try:
                return float(num) * units.get(unit, 1)
            except ValueError:
                return None
    try:
        return float(num)
    except ValueError:
        return None


def sample_containers(names=None):
    """Per-container CPU% and memory bytes via `docker stats --no-stream`."""
    fmt = "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
    out = _run(["docker", "stats", "--no-stream", "--format", fmt], timeout=15)
    result = {}
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        name, cpu, mem = cols[0].strip(), cols[1].strip(), cols[2].strip()
        if names and name not in names:
            continue
        used = mem.split("/")[0].strip()
        try:
            cpu_pct = float(cpu.rstrip("%"))
        except ValueError:
            cpu_pct = None
        result[name] = {"cpu_pct": cpu_pct, "mem_used_b": _parse_bytes(used)}
    return result


def one_sample(names=None):
    return {
        "t": time.time(),
        "gpu": sample_gpu(),
        "host": sample_host_mem(),
        "containers": sample_containers(names),
    }


def _loop(out_path, interval, names, stop_path):
    """Background loop: append one NDJSON sample per tick until SIGTERM/stop-file."""
    running = {"go": True}

    def _handle(_signo, _frame):
        running["go"] = False

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    with open(out_path, "a", encoding="utf-8") as fh:
        while running["go"]:
            if stop_path and os.path.exists(stop_path):
                break
            fh.write(json.dumps(one_sample(names)) + "\n")
            fh.flush()
            time.sleep(interval)


def _reduce(values):
    xs = [v for v in values if isinstance(v, (int, float))]
    if not xs:
        return None
    return {"min": min(xs), "mean": sum(xs) / len(xs), "max": max(xs), "last": xs[-1]}


def summarize(in_path):
    """Reduce an NDJSON timeseries to peak/mean resource stats."""
    samples = []
    try:
        with open(in_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    samples.append(json.loads(line))
    except OSError:
        samples = []
    gpu_keys = ["busy_pct", "vram_used_b", "vram_total_b", "gtt_used_b", "gtt_total_b"]
    host_keys = ["mem_total_b", "mem_available_b", "mem_used_b"]
    out = {
        "schema": "resource-summary/v1",
        "samples": len(samples),
        "gpu": {k: _reduce([s["gpu"].get(k) for s in samples if s.get("gpu")]) for k in gpu_keys},
        "host": {k: _reduce([s["host"].get(k) for s in samples if s.get("host")]) for k in host_keys},
        "containers": {},
    }
    names = set()
    for s in samples:
        names.update((s.get("containers") or {}).keys())
    for name in sorted(names):
        cpu = [s["containers"][name].get("cpu_pct") for s in samples if name in (s.get("containers") or {})]
        mem = [s["containers"][name].get("mem_used_b") for s in samples if name in (s.get("containers") or {})]
        out["containers"][name] = {"cpu_pct": _reduce(cpu), "mem_used_b": _reduce(mem)}
    # Convenience: the unified-memory budget + peak GPU allocation.
    out["unified_mem_total_b"] = (out["host"]["mem_total_b"] or {}).get("max") if out["host"]["mem_total_b"] else None
    out["peak_vram_used_b"] = (out["gpu"]["vram_used_b"] or {}).get("max") if out["gpu"]["vram_used_b"] else None
    out["peak_gtt_used_b"] = (out["gpu"]["gtt_used_b"] or {}).get("max") if out["gpu"]["gtt_used_b"] else None
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="resource_sampler", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("snapshot", help="one sample")
    ps.add_argument("--containers", default="", help="space-separated name filter")
    ps.add_argument("--out", default="")

    pa = sub.add_parser("start", help="launch a background sampling loop")
    pa.add_argument("--out", required=True, help="NDJSON timeseries file")
    pa.add_argument("--interval", type=float, default=1.0)
    pa.add_argument("--containers", default="")
    pa.add_argument("--pidfile", required=True)
    pa.add_argument("--stop-file", default="")

    pt = sub.add_parser("stop", help="stop the loop; optionally summarize")
    pt.add_argument("--pidfile", required=True)
    pt.add_argument("--in", dest="in_path", default="")
    pt.add_argument("--out", default="")

    pz = sub.add_parser("summarize", help="reduce NDJSON to summary JSON")
    pz.add_argument("--in", dest="in_path", required=True)
    pz.add_argument("--out", default="")

    # Hidden: the actual loop body (re-exec'd by `start`).
    pl = sub.add_parser("_loop")
    pl.add_argument("--out", required=True)
    pl.add_argument("--interval", type=float, default=1.0)
    pl.add_argument("--containers", default="")
    pl.add_argument("--stop-file", default="")

    args = p.parse_args(argv)
    names = set(args.containers.split()) if getattr(args, "containers", "") else None

    if args.cmd == "snapshot":
        text = json.dumps(one_sample(names), indent=2, sort_keys=True)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        print(text)
        return 0

    if args.cmd == "start":
        cmd = [sys.executable, os.path.abspath(__file__), "_loop",
               "--out", args.out, "--interval", str(args.interval)]
        if args.containers:
            cmd += ["--containers", args.containers]
        if args.stop_file:
            cmd += ["--stop-file", args.stop_file]
        proc = subprocess.Popen(  # noqa: S603 (fixed argv)
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        with open(args.pidfile, "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
        print(args.pidfile)
        return 0

    if args.cmd == "stop":
        try:
            with open(args.pidfile, "r", encoding="utf-8") as fh:
                pid = int(fh.read().strip())
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        finally:
            try:
                os.remove(args.pidfile)
            except OSError:
                pass
        if args.in_path and args.out:
            time.sleep(0.3)  # let the last flush land
            text = json.dumps(summarize(args.in_path), indent=2, sort_keys=True)
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            print(text)
        return 0

    if args.cmd == "summarize":
        text = json.dumps(summarize(args.in_path), indent=2, sort_keys=True)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        print(text)
        return 0

    if args.cmd == "_loop":
        _loop(args.out, args.interval, names, args.stop_file)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
