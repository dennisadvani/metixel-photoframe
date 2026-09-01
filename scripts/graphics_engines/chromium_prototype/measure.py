#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Summarise Chromium kiosk prototype benchmark results.

Aggregates the FPS samples (from benchmark_results.json) and the CPU/mem
samples (from cpu_mem.json) into a concise report.

Usage:
    python scripts/chromium_prototype/measure.py \
        --cpu-mem scripts/chromium_prototype/out/cpu_mem.json \
        --fps scripts/chromium_prototype/out/benchmark_results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def _load(path: Path) -> list:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _summarise_fps(samples: list) -> dict:
    if not samples:
        return {"count": 0}
    fps = [float(s.get("fps", 0)) for s in samples if s.get("fps") is not None]
    if not fps:
        return {"count": 0}
    return {
        "count": len(fps),
        "min": round(min(fps), 1),
        "max": round(max(fps), 1),
        "mean": round(statistics.mean(fps), 1),
        "median": round(statistics.median(fps), 1),
    }


def _summarise_cpu_mem(samples: list) -> dict:
    if not samples:
        return {"count": 0}
    cpu = [float(s.get("cpu_percent", 0)) for s in samples]
    avail = [int(s.get("mem_available_kb", 0)) for s in samples]
    total = [int(s.get("mem_total_kb", 0)) for s in samples]
    return {
        "count": len(samples),
        "cpu_percent_mean": round(statistics.mean(cpu), 1),
        "cpu_percent_max": round(max(cpu), 1),
        "mem_available_kb_mean": round(statistics.mean(avail)),
        "mem_total_kb": max(total) if total else 0,
        "mem_used_percent_mean": round(
            100.0 * (1.0 - statistics.mean(avail) / max(total)), 1
        )
        if total and max(total) > 0
        else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarise prototype benchmark")
    parser.add_argument("--cpu-mem", type=Path, default=Path("out/cpu_mem.json"))
    parser.add_argument("--fps", type=Path, default=Path("out/benchmark_results.json"))
    args = parser.parse_args()

    fps = _summarise_fps(_load(args.fps))
    cm = _summarise_cpu_mem(_load(args.cpu_mem))

    print("=== Metixel Chromium Kiosk Prototype — Results ===")
    print()
    print("FPS:")
    if fps.get("count"):
        print(f"  samples : {fps['count']}")
        print(f"  min     : {fps['min']} fps")
        print(f"  max     : {fps['max']} fps")
        print(f"  mean    : {fps['mean']} fps")
        print(f"  median  : {fps['median']} fps")
    else:
        print("  (no FPS samples collected)")
    print()
    print("CPU / memory:")
    if cm.get("count"):
        print(f"  samples            : {cm['count']}")
        print(f"  cpu mean           : {cm['cpu_percent_mean']}%")
        print(f"  cpu max            : {cm['cpu_percent_max']}%")
        print(f"  mem used (mean)    : {cm['mem_used_percent_mean']}%")
        print(f"  mem available (mean): {cm['mem_available_kb_mean']} kB")
        print(f"  mem total          : {cm['mem_total_kb']} kB")
    else:
        print("  (no CPU/mem samples collected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())