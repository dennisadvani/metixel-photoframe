# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""State Manager — atomic configuration persistence and change notification.

Handles thread-safe reads/writes of config.json and notifies the frontend
renderer of changes via inotify or a flag file.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from metixel.shared.config import Config
from metixel.shared.io import atomic_write_json
from metixel.shared.models import MediaItem
from metixel.shared.paths import install_root
from metixel.shared.system_stats import read_meminfo

if TYPE_CHECKING:
    from metixel.backend.processing.journal import ProcessingJournal

logger = logging.getLogger(__name__)


class StateManager:
    """Manages application state with atomic disk writes and change notification.

    The frontend renderer watches for changes to the config file via inotify
    or by polling the ``config.updated`` flag file in the run directory.
    """

    def __init__(self, config_path: Path, run_dir: Path = Path("/run/metixel")) -> None:
        self._config_path = config_path
        self._run_dir = run_dir
        self._lock = threading.Lock()
        self._config: Config = Config.load(config_path)

        # Playlist state — tracks known media items from folder scans
        self._playlist: list[MediaItem] = []
        self._playlist_lock = threading.Lock()

        # Processing journal — lazily created single-writer store of per-file
        # pipeline state (see ProcessingJournal).  Lives in the cache dir so
        # "Clear cache" wipes it too.
        self._journal: ProcessingJournal | None = None

        # Ensure run directory exists
        run_dir.mkdir(parents=True, exist_ok=True)

    # -- Processing journal -------------------------------------------------

    @property
    def journal(self) -> ProcessingJournal:
        """The shared single-writer processing journal (lazily created).

        All pipeline participants (folder watcher, optimisation queue, web
        API) read/write through this controller — never the file directly.
        """
        if self._journal is None:
            from metixel.backend.processing.journal import ProcessingJournal

            self._journal = ProcessingJournal(self._journal_path())
        return self._journal

    def _journal_path(self) -> Path:
        """Resolve the journal file path inside the configured cache dir."""
        cache_dir = Path(self._config.system.get("cache_dir", "cache/"))
        if not cache_dir.is_absolute():
            cache_dir = install_root() / cache_dir
        return cache_dir / "processing_state.json"

    def flush_journal(self) -> None:
        """Persist any pending journal writes (call during shutdown).

        No-op if the journal was never created during this run.
        """
        if self._journal is not None:
            self._journal.flush()

    # -- Config Access -------------------------------------------------------

    @property
    def config(self) -> Config:
        """Get the current configuration (thread-safe copy)."""
        with self._lock:
            return Config(self._config.to_dict())

    @property
    def config_path(self) -> Path:
        """The filesystem path to the configuration file."""
        return self._config_path

    def get_config_value(self, *keys: str, default: Any = None) -> Any:
        """Get a nested config value by key path."""
        with self._lock:
            data: Any = self._config.to_dict()
            for key in keys:
                if isinstance(data, dict):
                    data = data.get(key)
                else:
                    return default
            return data if data is not None else default

    # -- Config Mutation -----------------------------------------------------

    def update_config(self, section: str, values: dict[str, Any]) -> None:
        """Update a config section and persist atomically."""
        with self._lock:
            logger.info(
                "Updating config section '%s' with %d keys: %s → %s",
                section,
                len(values),
                list(values.keys()),
                str(self._config_path),
            )
            self._config.update(section, values)
            self._config.save(self._config_path)
            self._notify_change()
            logger.info("Config section '%s' saved successfully to %s", section, self._config_path)

    def replace_config(self, data: dict[str, Any]) -> None:
        """Replace the entire configuration."""
        with self._lock:
            self._config.replace(data)
            self._config.save(self._config_path)
            self._notify_change()

    def reload_config(self) -> None:
        """Reload configuration from disk."""
        with self._lock:
            self._config = Config.load(self._config_path)

    # -- Change Notification -------------------------------------------------

    def _notify_change(self) -> None:
        """Signal the frontend that configuration has changed.

        Creates a flag file that the frontend's inotify watcher can detect.
        """
        flag_file = self._run_dir / "config.updated"
        try:
            flag_file.write_text("1")
            # Touch the file to update mtime even if content is same
            os.utime(flag_file, None)
        except OSError:
            logger.warning("Could not write config.updated flag file — is /run/metixel writable?")

    # -- System State --------------------------------------------------------

    def get_system_health(self) -> dict[str, Any]:
        """Return system health metrics for MQTT/API reporting."""
        import shutil

        disk = shutil.disk_usage("/")
        # On Linux, disk.free is space available to non-root users
        # (excludes reserved blocks).  Compute "used" as total − free
        # so the dashboard math is internally consistent:
        #   free + used = total,  used / total = percent
        used_bytes = disk.total - disk.free
        cache_size = self._get_cache_size()
        # Media size: ALL files under media/ (not just watched folders).
        # Playlist counts: only items in enabled watch folders.
        media_dir = Path(self.config.system.get("media_dir", "media/"))
        if not media_dir.is_absolute():
            media_dir = Path("/opt/metixel") / media_dir
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

    def _get_cache_size(self) -> int:
        """Calculate the total size of the cache directory in bytes.

        Returns 0 if the directory does not exist or cannot be read.
        """
        try:
            config = self.config
            cache_dir = Path(config.system.get("cache_dir", "cache/"))
            if not cache_dir.is_absolute():
                cache_dir = Path("/opt/metixel") / cache_dir
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

            watch_paths = resolve_watch_paths(self.config)
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

    @staticmethod
    def _get_uptime() -> float:
        """Get system uptime in seconds."""
        try:
            with open("/proc/uptime") as f:
                return float(f.readline().split()[0])
        except Exception:
            return 0.0

    # Cached /proc/stat from previous call for CPU delta calculation.
    _prev_cpu_jiffies: float | None = None
    _prev_idle_jiffies: float | None = None

    @classmethod
    def _get_cpu_percent(cls) -> float:
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

        prev_total = cls._prev_cpu_jiffies
        prev_idle = cls._prev_idle_jiffies
        cls._prev_cpu_jiffies = float(jiffies)
        cls._prev_idle_jiffies = float(idle_now)

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
            import subprocess

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

    # -- Playlist Management -------------------------------------------------

    def get_playlist(self) -> list[MediaItem]:
        """Get a copy of the current media playlist (thread-safe)."""
        with self._playlist_lock:
            return list(self._playlist)

    def replace_playlist(self, items: list[MediaItem]) -> None:
        """Replace the entire playlist and persist to disk."""
        with self._playlist_lock:
            self._playlist = list(items)
            self._write_playlist_file()
            self._notify_playlist_change()

    def add_playlist_items(self, items: list[MediaItem]) -> None:
        """Add items to the playlist (deduplicating by id)."""
        with self._playlist_lock:
            existing_ids = {item.id for item in self._playlist}
            new_items = [item for item in items if item.id not in existing_ids]
            if new_items:
                self._playlist.extend(new_items)
                self._write_playlist_file()
                self._notify_playlist_change()
                logger.info(
                    "[SLIDESHOWQ] +%d items (total: %d)",
                    len(new_items),
                    len(self._playlist),
                )
                for item in new_items:
                    logger.debug(
                        "[SLIDESHOWQ]  add  | %-5s | %s",
                        item.media_type.value,
                        item.original_path.name,
                    )
            else:
                logger.debug(
                    "[SLIDESHOWQ] no-op | %d item(s) already in playlist",
                    len(items),
                )

        # Record successful pipeline completion in the journal (single writer).
        # Any item that reaches the playlist is "ready" — this covers images
        # added directly at watch/classify time AND processed images/videos.
        # Guarded so pure in-memory playlist manipulation (dev/tests) does not
        # force-create the journal; in production the watcher always creates
        # it before any item reaches this point.
        if self._journal is not None:
            with contextlib.suppress(Exception):
                for item in new_items:
                    status = item.transcode_status.value if item.transcode_status else None
                    self.journal.mark_ready(item.original_path, status)

    def remove_playlist_items(self, item_ids: set[str]) -> int:
        """Remove items from the playlist by id. Returns count removed."""
        with self._playlist_lock:
            before = len(self._playlist)
            self._playlist = [item for item in self._playlist if item.id not in item_ids]
            removed = before - len(self._playlist)
            if removed > 0:
                self._write_playlist_file()
                self._notify_playlist_change()
                logger.info(
                    "[SLIDESHOWQ] -%d items (total: %d)",
                    removed,
                    len(self._playlist),
                )
            return removed

    def clear_playlist(self) -> None:
        """Clear the entire playlist and notify the frontend.

        Used after cache clear — all cached files are deleted, so the
        playlist must be rebuilt from scratch on the next folder scan.
        """
        with self._playlist_lock:
            count = len(self._playlist)
            self._playlist.clear()
            self._write_playlist_file()  # Writes empty JSON array
            self._notify_playlist_change()
            logger.info(
                "Playlist cleared (%d items removed) — frontend will reset queue",
                count,
            )

    def _write_playlist_file(self) -> None:
        """Atomically write the playlist to a JSON file for the frontend."""
        playlist_path = self._run_dir / "playlist.json"
        try:
            data = [
                {
                    "id": item.id,
                    "original_path": str(item.original_path),
                    "cached_path": str(item.cached_path),
                    "media_type": item.media_type.value,
                    "width": item.width,
                    "height": item.height,
                    "duration_seconds": item.duration_seconds,
                    "thumbnail_path": str(item.thumbnail_path) if item.thumbnail_path else None,
                    "first_frame_path": str(item.first_frame_path)
                    if item.first_frame_path
                    else None,
                    "last_frame_path": str(item.last_frame_path) if item.last_frame_path else None,
                    "source": item.source,
                    "transcode_status": item.transcode_status.value
                    if item.transcode_status
                    else None,
                    "failure_reason": item.failure_reason,
                }
                for item in self._playlist
            ]
            atomic_write_json(playlist_path, data, indent=2)
        except OSError:
            logger.warning("Could not write playlist file — is %s writable?", self._run_dir)

    def _notify_playlist_change(self) -> None:
        """Signal the frontend that the playlist has changed."""
        flag_file = self._run_dir / "playlist.updated"
        try:
            flag_file.write_text("1")
            os.utime(flag_file, None)
        except OSError:
            logger.debug("Could not write playlist.updated flag file")
