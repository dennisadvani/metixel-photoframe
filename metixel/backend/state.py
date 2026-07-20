# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""State Manager — atomic configuration persistence and change notification.

Handles thread-safe reads/writes of config.json and notifies the frontend
renderer of changes via inotify or a flag file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from metixel.shared.config import Config
from metixel.shared.models import MediaItem

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

        # Ensure run directory exists
        run_dir.mkdir(parents=True, exist_ok=True)

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
            data = self._config.to_dict()
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
                section, len(values), list(values.keys()), str(self._config_path),
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
        cache_size = self._get_cache_size()
        return {
            "uptime_seconds": self._get_uptime(),
            "disk_total_gb": round(disk.total / (1024**3), 1),
            "disk_used_gb": round(disk.used / (1024**3), 1),
            "disk_free_gb": round(disk.free / (1024**3), 1),
            "disk_used_percent": round(disk.used / disk.total * 100, 1),
            "cache_size_mb": round(cache_size / (1024**2), 1),
            "cache_size_bytes": cache_size,
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
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        pass
            return total
        except Exception:
            logger.debug("Could not compute cache size", exc_info=True)
            return 0

    @staticmethod
    def _get_uptime() -> float:
        """Get system uptime in seconds."""
        try:
            with open("/proc/uptime") as f:
                return float(f.readline().split()[0])
        except Exception:
            return 0.0

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
                logger.info("Added %d items to playlist (total: %d)", len(new_items), len(self._playlist))

    def remove_playlist_items(self, item_ids: set[str]) -> int:
        """Remove items from the playlist by id. Returns count removed."""
        with self._playlist_lock:
            before = len(self._playlist)
            self._playlist = [item for item in self._playlist if item.id not in item_ids]
            removed = before - len(self._playlist)
            if removed > 0:
                self._write_playlist_file()
                self._notify_playlist_change()
                logger.info("Removed %d items from playlist (total: %d)", removed, len(self._playlist))
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
                    "source": item.source,
                }
                for item in self._playlist
            ]
            tmp_path = playlist_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, playlist_path)
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
