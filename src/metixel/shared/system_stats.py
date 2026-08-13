# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Read-only /proc-based system + GPU statistics.

Deduplicates the ``/proc/meminfo`` / ``/proc/stat`` / ``/proc/loadavg``
parsers and the GPU memory log formatting that previously lived (in near-
identical form) in the renderer, optimisation queue, state manager, probe
helpers, ffmpeg command builders, and the GPU texture warnings.

All functions are pure reads that return safe defaults (never raise) so
callers can use them on any platform — non-Linux/dev machines simply get
``None`` / ``{}`` / ``-1.0`` back.
"""

from __future__ import annotations

from typing import Any


def read_meminfo() -> dict[str, int]:
    """Parse ``/proc/meminfo`` into a dict of integer kB values.

    Returns ``{}`` if the file is unavailable (non-Linux) or malformed.
    """
    meminfo: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" in line:
                    key, val = line.split(":", 1)
                    parts = val.strip().split()
                    if parts:
                        meminfo[key.strip()] = int(parts[0])
    except (OSError, ValueError):
        return {}
    return meminfo


def available_ram_bytes() -> int | None:
    """Read available system RAM (``MemAvailable``) in bytes.

    Returns ``None`` if ``/proc/meminfo`` is unavailable.
    """
    kb = read_meminfo().get("MemAvailable")
    return kb * 1024 if kb is not None else None


def read_cpu_percent() -> float:
    """CPU utilisation percent (0.0–100.0) from ``/proc/stat``.

    Returns ``-1.0`` if the file is unavailable or unparseable.
    """
    try:
        with open("/proc/stat") as f:
            cpu_line = f.readline()
        parts = cpu_line.split()
        if parts[0] == "cpu" and len(parts) >= 8:
            user, nice, system, idle = (
                int(parts[1]),
                int(parts[2]),
                int(parts[3]),
                int(parts[4]),
            )
            total = user + nice + system + idle
            active = user + nice + system
            return (active / total * 100) if total > 0 else 0.0
    except (OSError, ValueError, IndexError):
        pass
    return -1.0


def read_loadavg() -> tuple[str, str, str]:
    """Return the three load-average values from ``/proc/loadavg``.

    Raises ``OSError`` / ``ValueError`` if the file is unavailable or in
    an unexpected format — callers should treat those as "no data".
    """
    with open("/proc/loadavg") as f:
        parts = f.readline().split()[:3]
    if len(parts) != 3:
        raise ValueError("unexpected /proc/loadavg format")
    return parts[0], parts[1], parts[2]


def read_system_stats() -> dict[str, Any] | None:
    """Snapshot of CPU / memory / swap / load for the ``RES:`` log line.

    Returns ``None`` if ``/proc`` is unavailable (non-Linux/dev machine).
    Keys: ``cpu_percent``, ``mem_used_mb``, ``mem_total_mb``,
    ``mem_percent``, ``swap_used_mb``, ``swap_total_mb``, ``loadavg``
    (a ``(1m, 5m, 15m)`` tuple of strings).
    """
    mem = read_meminfo()
    if not mem:
        return None
    total_kb = mem.get("MemTotal", 0)
    avail_kb = mem.get("MemAvailable", 0)
    total_mb = total_kb // 1024
    used_mb = (total_kb - avail_kb) // 1024 if total_kb > 0 else 0
    mem_pct = (used_mb / total_mb * 100) if total_mb > 0 else 0.0
    swap_total = mem.get("SwapTotal", 0) // 1024
    swap_free = mem.get("SwapFree", 0) // 1024
    try:
        load = read_loadavg()
    except (OSError, ValueError):
        load = ("?", "?", "?")
    return {
        "cpu_percent": read_cpu_percent(),
        "mem_used_mb": used_mb,
        "mem_total_mb": total_mb,
        "mem_percent": mem_pct,
        "swap_used_mb": swap_total - swap_free,
        "swap_total_mb": swap_total,
        "loadavg": load,
    }


def format_gpu_stats(gpu_info: dict[str, Any] | None) -> str:
    """Format a ``gpu_memory_info()`` dict for log lines.

    Produces ``total=<n>M reloc=<n>M (<pct>%) V3D=<n>kb/<n>BOs
    textures=<n>/<n>``.  The percentage is included only when both total
    and reloc are numeric.  Returns ``"unavailable"`` for ``None``.
    """
    if not gpu_info:
        return "unavailable"
    total = gpu_info.get("gpu_total_mb", "?")
    reloc = gpu_info.get("reloc_used_mb", "?")
    pct = ""
    if isinstance(total, int) and isinstance(reloc, int) and total > 0:
        pct = f" ({reloc / total * 100:.0f}%)"
    return (
        f"total={total}M reloc={reloc}M{pct} "
        f"V3D={gpu_info.get('v3d_bo_kb', '?')}kb/{gpu_info.get('v3d_bo_count', '?')}BOs "
        f"textures={gpu_info.get('texture_count', '?')}/{gpu_info.get('max_textures', '?')}"
    )
