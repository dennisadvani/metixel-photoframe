# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""System metrics service — CPU, memory, swap, disk, cache & media sizing.

Extracted from :class:`metixel.backend.state.StateManager` so the state
manager stays focused on config/playlist persistence while all health
metric gathering lives in one injectable, thread-safe service.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from metixel.shared.config import Config
from metixel.shared.paths import resolve_install_path
from metixel.shared.system_stats import read_meminfo

logger = logging.getLogger(__name__)


class SystemMetrics:
    """Collects system health metrics for MQTT/API reporting.

    The config is obtained lazily via a *config provider* callable so the
    service stays decoupled from ``StateManager`` — it never holds its own
    copy of config, always reading the latest snapshot.

    All mutable state (the CPU-jiffies delta cache) is guarded by a lock so
    concurrent health polls from the web API and MQTT thread are safe.
    """

    def __init__(self, config_provider: Callable[[], Config]) -> None:
        self._config_provider = config_provider
        self._lock = threading.Lock()
        self._prev_cpu_jiffies: float | None = None
        self._prev_idle_jiffies: float | None = None

    @property
    def _config(self) -> Config:
        """Latest config snapshot from the provider."""
        return self._config_provider()

    # -- Public -------------------------------------------------------------

    def get_system_health(self) -> dict[str, Any]:
        """Return system health metrics for MQTT/API reporting."""
        disk = shutil.disk_usage("/")
        # On Linux, disk.free is space available to non-root users
        # (excludes reserved blocks).  Compute "used" as total − free
        # so the dashboard math is internally consistent:
        #   free + used = total,  used / total = percent
        used_bytes = disk.total - disk.free
        cache_size = self._get_cache_size()
        # Media size: ALL files under media/ (not just watched folders).
        # Playlist counts: only items in enabled watch folders.
        media_dir = self._resolve_media_dir()
        media_size = self._get_media_folder_size(media_dir)
        img_count, vid_count = self._get_playlist_counts()

        cpu_pct = self._get_cpu_percent()
        cpu_temp = self._get_cpu_temp()
        mem = self._get_memory_stats()
        swap = self._get_swap_stats()

        return {
            "uptime_seconds": self._get_uptime(),
            "disk_total_gb": round(disk.total / (1024**3), 1),
            "disk_used_gb": round(used_bytes / (1024**3), 1),
            "disk_free_gb": round(disk.free / (1024**3), 1),
            "disk_used_percent": round(used_bytes / disk.total * 100, 1),
            "cache_size_mb": round(cache_size / (1024**2), 1),
            "cache_size_bytes": cache_size,
            "media_size_bytes": media_size,
            "playlist_image_count": img_count,
            "playlist_video_count": vid_count,
            "cpu_percent": cpu_pct,
            "cpu_temp_c": cpu_temp,
            "memory_percent": mem["percent"],
            "memory_used_gb": mem["used_gb"],
            "memory_total_gb": mem["total_gb"],
            "swap_percent": swap["percent"],
            "swap_used_gb": swap["used_gb"],
            "swap_total_gb": swap["total_gb"],
        }

    # -- Path resolution ----------------------------------------------------

    def _resolve_media_dir(self) -> Path:
        """Resolve the configured media directory to an absolute path."""
        return resolve_install_path(self._config.system.get("media_dir", "media/"))

    def _resolve_cache_dir(self) -> Path:
        """Resolve the configured cache directory to an absolute path."""
        return resolve_install_path(self._config.system.get("cache_dir", "cache/"))

    # -- Size helpers -------------------------------------------------------

    def _get_cache_size(self) -> int:
        """Calculate the total size of the cache directory in bytes.

        Returns 0 if the directory does not exist or cannot be read.
        """
        try:
            cache_dir = self._resolve_cache_dir()
            if not cache_dir.is_dir():
                return 0
            total = 0
            for entry in cache_dir.rglob("*"):
                if entry.is_file():
                    with contextlib.suppress(OSError):
                        total += entry.stat().st_size
            return total
        except Exception:
            logger.debug("Could not compute cache size", exc_info=True)
            return 0

    def _get_media_folder_size(self, media_dir: Path) -> int:
        """Calculate the total size of ALL files under *media_dir* in bytes.

        This includes everything — watched folders, Immich sync data,
        sample media — not just the actively-watched paths.
        """
        try:
            if not media_dir.is_dir():
                return 0
            total = 0
            seen: set[int] = set()
            for entry in media_dir.rglob("*"):
                if not entry.is_file():
                    continue
                try:
                    st = entry.stat()
                    if st.st_ino not in seen:
                        seen.add(st.st_ino)
                        total += st.st_size
                except OSError:
                    pass
            return total
        except Exception:
            logger.debug("Could not compute media folder size", exc_info=True)
            return 0

    def _get_playlist_counts(self) -> tuple[int, int]:
        """Count images and videos in enabled watch folders (playlist items).

        This counts only items picked up by the folder watcher — not all
        media on disk (Immich sync files, sample media, etc.).

        Returns:
            A tuple of ``(image_count, video_count)``.
        """
        IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}  # noqa: N806
        VID_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"}  # noqa: N806
        try:
            from metixel.shared.config import resolve_watch_paths

            watch_paths = resolve_watch_paths(self._config)
            img_count = 0
            vid_count = 0
            for media_folder in watch_paths:
                if not media_folder.is_dir():
                    continue
                for entry in media_folder.rglob("*"):
                    if not entry.is_file():
                        continue
                    suffix = entry.suffix.lower()
                    if suffix in IMG_EXT:
                        img_count += 1
                    elif suffix in VID_EXT:
                        vid_count += 1
            return img_count, vid_count
        except Exception:
            logger.debug("Could not count media files", exc_info=True)
            return 0, 0

    # -- CPU / temp / memory / swap -----------------------------------------

    @staticmethod
    def _get_uptime() -> float:
        """Get system uptime in seconds."""
        try:
            with open("/proc/uptime") as f:
                return float(f.readline().split()[0])
        except Exception:
            return 0.0

    def _get_cpu_percent(self) -> float:
        """Compute CPU utilisation as a percentage (0–100).

        Reads ``/proc/stat`` and computes the delta in total CPU jiffies
        since the previous call.  Because the web dashboard polls health
        every ~3 seconds, the delta window matches the display interval.
        On the first call (no prior sample) returns 0.0.
        """
        try:
            with open("/proc/stat") as f:
                line = f.readline()
            # cpu  user nice system idle iowait irq softirq steal ...
            parts = line.split()
            if parts[0] != "cpu":
                return 0.0
            # Sum all jiffy columns (ignore guest/guest_nice)
            jiffies = sum(int(x) for x in parts[1:8])
            idle_now = int(parts[4])  # idle column
        except Exception:
            return 0.0

        with self._lock:
            prev_total = self._prev_cpu_jiffies
            prev_idle = self._prev_idle_jiffies
            self._prev_cpu_jiffies = float(jiffies)
            self._prev_idle_jiffies = float(idle_now)

        if prev_total is None or prev_idle is None:
            return 0.0

        delta_total = jiffies - prev_total
        if delta_total <= 0:
            return 0.0

        delta_idle = idle_now - prev_idle
        if delta_idle < 0:
            delta_idle = 0.0

        pct = (1.0 - delta_idle / delta_total) * 100.0
        return round(max(0.0, min(100.0, pct)), 1)

    @staticmethod
    def _get_cpu_temp() -> float:
        """Read CPU temperature via ``vcgencmd measure_temp``.

        Returns temperature in degrees Celsius, or 0.0 on failure.
        """
        try:
            result = subprocess.run(
                ["vcgencmd", "measure_temp"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Output format: "temp=45.0'C\n"
            out = result.stdout.strip()
            if out.startswith("temp="):
                return float(out.split("=")[1].split("'")[0])
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _get_memory_stats() -> dict[str, float]:
        """Read ``/proc/meminfo`` and return memory usage stats.

        Returns:
            Dict with keys ``percent``, ``used_gb``, ``total_gb``.
            Falls back to zeros if ``/proc/meminfo`` cannot be read.
        """
        mem = read_meminfo()
        total_kb = mem.get("MemTotal", 0)
        available_kb = mem.get("MemAvailable", 0)
        if total_kb <= 0:
            return {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0}
        used_kb = total_kb - available_kb
        pct = round(used_kb / total_kb * 100.0, 1)
        return {
            "percent": max(0.0, min(100.0, pct)),
            "used_gb": round(used_kb / (1024 * 1024), 1),
            "total_gb": round(total_kb / (1024 * 1024), 1),
        }

    @staticmethod
    def _get_swap_stats() -> dict[str, float]:
        """Read ``/proc/meminfo`` and return swap usage stats.

        Returns:
            Dict with keys ``percent``, ``used_gb``, ``total_gb``.
            Falls back to zeros if swap is disabled or unreadable.
        """
        mem = read_meminfo()
        total_kb = mem.get("SwapTotal", 0)
        free_kb = mem.get("SwapFree", 0)
        if total_kb <= 0:
            return {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0}
        used_kb = total_kb - free_kb
        pct = round(used_kb / total_kb * 100.0, 1)
        return {
            "percent": max(0.0, min(100.0, pct)),
            "used_gb": round(used_kb / (1024 * 1024), 1),
            "total_gb": round(total_kb / (1024 * 1024), 1),
        }
