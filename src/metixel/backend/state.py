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

from metixel.backend.system_metrics import SystemMetrics
from metixel.shared.config import Config
from metixel.shared.io import atomic_write_json
from metixel.shared.models import MediaItem
from metixel.shared.paths import install_root

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

        # System metrics service — owns all CPU/memory/disk/cache sizing.
        self._metrics = SystemMetrics(config_provider=self._provide_config)

        # Ensure run directory exists
        run_dir.mkdir(parents=True, exist_ok=True)

    def _provide_config(self) -> Config:
        """Config provider for the metrics service (always the latest copy)."""
        return self.config

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
        """Return system health metrics for MQTT/API reporting.

        Delegates to the :class:`~metixel.backend.system_metrics.SystemMetrics`
        service, which owns all CPU/memory/swap/disk/cache sizing.
        """
        return self._metrics.get_system_health()

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
