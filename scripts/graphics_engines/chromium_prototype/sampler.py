#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""On-Pi /proc CPU/mem sampler for the Chromium kiosk prototype.

Samples total CPU utilisation and memory usage from /proc at a fixed
interval while the kiosk runs, and writes the samples to a JSON file.

Run this on the Pi alongside the chromium kiosk (see run_on_pi.sh).

Usage:
    python3 sampler.py --duration 60 --out cpu_mem.json [--interval 1.0]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def _read_cpu_times() -> list[int]:
    """Read the per-CPU cumulative jiffies from /proc/stat."""
    with open("/proc/stat", encoding="utf-8") as f:
        for line in f:
            if line.startswith("cpu "):
                parts = line.split()
                return [int(x) for x in parts[1:]]
    return []


def _cpu_percent(prev: list[int], now: list[int]) -> float:
    """Compute CPU utilisation % between two /proc/stat snapshots."""
    if not prev or not now or len(prev) < 4 or len(now) < 4:
        return 0.0
    prev_idle = prev[3] + (prev[4] if len(prev) > 4 else 0)
    now_idle = now[3] + (now[4] if len(now) > 4 else 0)
    prev_total = sum(prev)
    now_total = sum(now)
    d_total = now_total - prev_total
    d_idle = now_idle - prev_idle
    if d_total <= 0:
        return 0.0
    return 100.0 * (1.0 - d_idle / d_total)


def _mem_info() -> dict:
    """Read memory totals from /proc/meminfo."""
    out: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable", "MemFree"):
                out[key] = int(rest.strip().split()[0])  # kB
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="On-Pi /proc sampler")
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--out", type=Path, default=Path("cpu_mem.json"))
    args = parser.parse_args()

    samples: list[dict] = []
    prev = _read_cpu_times()
    start = time.monotonic()

    while time.monotonic() - start < args.duration:
        time.sleep(args.interval)
        now = _read_cpu_times()
        mem = _mem_info()
        samples.append(
            {
                "t": round(time.monotonic() - start, 2),
                "cpu_percent": round(_cpu_percent(prev, now), 1),
                "mem_total_kb": mem.get("MemTotal", 0),
                "mem_available_kb": mem.get("MemAvailable", 0),
                "mem_free_kb": mem.get("MemFree", 0),
            }
        )
        prev = now

    args.out.write_text(json.dumps(samples, indent=2), encoding="utf-8")
    print(f"Sampled {len(samples)} points -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())