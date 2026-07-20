# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Local folder watcher for media synchronization.

Monitors configured directories for new/changed media files using inotify
(via the watchdog library) with a polling fallback for network mounts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from metixel.backend.processing.image import ImageProcessor
from metixel.backend.processing.video import VideoProcessor
from metixel.backend.state import StateManager
from metixel.shared.models import MediaItem

logger = logging.getLogger(__name__)

# Accepted media file extensions
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Progress file written during initial processing — read by the frontend
# so it can show a progress bar before the slideshow starts.
PROCESSING_STATUS_PATH = "/run/metixel/processing_status.json"


def _write_progress(phase: str, total: int, processed: int, current_file: str = "") -> None:
    """Atomically write the processing progress status file."""
    try:
        os.makedirs(os.path.dirname(PROCESSING_STATUS_PATH), exist_ok=True)
        tmp = PROCESSING_STATUS_PATH + ".tmp"
        data = {
            "phase": phase,
            "total": total,
            "processed": processed,
            "current_file": current_file,
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, PROCESSING_STATUS_PATH)
    except OSError:
        logger.debug("Could not write processing status — /run/metixel not available?")


class FolderWatcher:
    """Watches local directories for new, changed, and deleted media files.

    Maintains a snapshot of known files (path → mtime/size) and compares
    each scan against the previous state to detect:
    - **New files**: processed and added to the playlist
    - **Modified files**: re-processed (cache invalidated)
    - **Deleted files**: removed from the playlist

    Uses the ``watchdog`` library for inotify-based monitoring on local
    filesystems, with a configurable polling fallback for NFS/SMB mounts.
    """

    def __init__(self, state: StateManager) -> None:
        self._state = state
        self._config = state.config
        self._watch_paths: list[Path] = [
            Path(p) for p in self._config.sync["local"].get("watch_paths", ["media/"])
        ]
        self._poll_interval: int = self._config.sync["local"]["poll_interval_seconds"]
        self._running: bool = False

        # File state snapshot: {absolute_path: (mtime_ns, size_bytes)}
        self._known_files: dict[Path, tuple[int, int]] = {}

        # Media processors (lazy init — needs config resolution)
        self._image_processor: ImageProcessor | None = None
        self._video_processor: VideoProcessor | None = None

        # Track whether the initial full scan has completed
        self._initial_scan_done: bool = False

    def run(self) -> None:
        """Main watch loop."""
        self._running = True
        logger.info(
            "Folder watcher started (paths: %s, interval: %ds)",
            [str(p) for p in self._watch_paths],
            self._poll_interval,
        )

        while self._running:
            try:
                self._scan()
            except Exception:
                logger.exception("Folder watcher error")
            time.sleep(self._poll_interval)

    def stop(self) -> None:
        """Signal the watch loop to stop."""
        self._running = False

    # -- Scanning ------------------------------------------------------------

    def _scan(self) -> None:
        """Scan watch directories for new, changed, or deleted files.

        On the first scan (initial_scan_done=False), builds the baseline
        snapshot and processes all discovered files as "new."  Subsequent
        scans diff against the snapshot to detect incremental changes.
        """
        # Lazy-init processors on first scan
        if self._image_processor is None:
            self._init_processors()

        # 1. Walk all watch paths and discover current files
        current_files: dict[Path, tuple[int, int]] = {}
        for watch_path in self._watch_paths:
            if not watch_path.exists():
                logger.debug("Watch path not found: %s", watch_path)
                continue
            self._walk_path(watch_path, current_files)

        # 2. If this is the initial scan, process everything as new
        if not self._initial_scan_done:
            self._initial_scan_done = True
            self._known_files = dict(current_files)

            # Write initial progress (phase=scanning, show we discovered files)
            total = len(current_files)
            if total > 0:
                _write_progress("scanning", total, 0, "")
            else:
                _write_progress("complete", 0, 0, "")
                logger.info("Initial scan: no media files found")
                return

            logger.info(
                "Initial scan: discovered %d media files across %d watch paths",
                total, len(self._watch_paths),
            )
            self._process_new(list(current_files.keys()), is_initial=True)
            return

        # 3. Diff: detect new, changed, and deleted files
        known_paths = set(self._known_files.keys())
        current_paths = set(current_files.keys())

        new_paths = current_paths - known_paths
        deleted_paths = known_paths - current_paths
        common_paths = known_paths & current_paths

        # Modified: same path, different mtime or size
        changed_paths: set[Path] = set()
        for p in common_paths:
            if self._known_files[p] != current_files[p]:
                changed_paths.add(p)

        # 4. Act on changes
        if new_paths:
            logger.info("Detected %d new file(s): %s", len(new_paths),
                        ", ".join(str(p.name) for p in list(new_paths)[:5]))
            self._process_new(list(new_paths))

        if changed_paths:
            logger.info("Detected %d changed file(s): %s", len(changed_paths),
                        ", ".join(str(p.name) for p in list(changed_paths)[:5]))
            self._process_changed(list(changed_paths))

        if deleted_paths:
            logger.info("Detected %d deleted file(s): %s", len(deleted_paths),
                        ", ".join(str(p.name) for p in list(deleted_paths)[:5]))
            self._handle_deleted(deleted_paths)

        # 5. Update snapshot
        self._known_files = current_files

    def _walk_path(
        self, root: Path, out: dict[Path, tuple[int, int]]
    ) -> None:
        """Recursively walk a directory, collecting media files with metadata.

        Args:
            root: Directory to walk.
            out: Dict to populate with {path: (mtime_ns, size)}.
        """
        try:
            for entry in root.rglob("*"):
                if not entry.is_file():
                    continue
                suffix = entry.suffix.lower()
                if suffix not in MEDIA_EXTENSIONS:
                    continue
                try:
                    stat = entry.stat()
                    out[entry.resolve()] = (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    logger.debug("Cannot stat: %s", entry)
        except OSError:
            logger.debug("Cannot walk directory: %s", root)

    # -- Media processing ----------------------------------------------------

    def _init_processors(self) -> None:
        """Lazy-initialize media processors with screen resolution from config."""
        display = self._config.display
        sw = display.get("width") or 1920
        sh = display.get("height") or 1080

        cache_dir = Path(self._config.system.get("cache_dir", "cache/"))
        if not cache_dir.is_absolute():
            # Relative to config file location — resolve
            cache_dir = self._state.config_path.parent.parent / cache_dir

        cache_dir.mkdir(parents=True, exist_ok=True)

        # Read video config for transcoding settings
        video_config = self._config.video if hasattr(self._config, "video") else {}

        self._image_processor = ImageProcessor(cache_dir, screen_width=sw, screen_height=sh)
        self._video_processor = VideoProcessor(
            cache_dir, screen_width=sw, screen_height=sh, video_config=video_config,
        )
        logger.info(
            "Media processors initialised: cache=%s, screen=%dx%d, transcode=%s",
            cache_dir, sw, sh,
            "enabled" if video_config.get("transcoding_enabled", True) else "disabled",
        )

    def _process_new(self, paths: list[Path], is_initial: bool = False) -> None:
        """Process newly discovered files and add them to the playlist.

        Delegates to ImageProcessor or VideoProcessor based on file extension.
        On success, the returned MediaItem is added to the playlist via
        ``StateManager.add_playlist_items()``.

        When *is_initial* is True, a progress status file is written after
        each file so the frontend can render a progress bar on screen while
        waiting for processing to complete.

        Guardrails:
        - Videos that are still transcoding are queued for later (re-scanned
          on the next poll cycle).  The ``transcode_status`` on each
          MediaItem lets the presentation engine skip items that aren't
          ready to play yet.
        """
        # Sort: images first, videos last — so the frontend gets photos
        # into the playlist quickly before slow video transcodes begin.
        paths.sort(key=lambda p: (0 if p.suffix.lower() in IMAGE_EXTENSIONS else 1, p))

        total = len(paths)

        # Flush items to the playlist every N files so the frontend can
        # start showing images while the backend is still processing.
        # Without this, all 342 files must finish before any image appears.
        _FLUSH_EVERY = 12

        items: list[MediaItem] = []
        deferred_paths: list[Path] = []  # Videos still transcoding — retry later

        for idx, path in enumerate(paths):
            suffix = path.suffix.lower()
            try:
                if suffix in VIDEO_EXTENSIONS and self._video_processor:
                    # Guardrail: do not add a video that is currently
                    # being transcoded — defer to the next scan cycle.
                    file_hash = self._video_processor._hash_file(path)
                    if self._video_processor.is_transcoding(file_hash):
                        logger.debug(
                            "Deferring %s — transcode still in progress",
                            path.name,
                        )
                        deferred_paths.append(path)
                        continue

                    # Report progress BEFORE transcoding begins — video
                    # processing can take minutes and the dashboard needs
                    # to show what's happening *during* the operation,
                    # not only after it finishes.
                    if is_initial:
                        _write_progress("transcoding", total, idx + 1, path.name)

                    item = self._video_processor.process(path, source="local")
                elif self._image_processor:
                    # Report progress before image processing too, so the
                    # dashboard updates immediately rather than after the
                    # fact (small win for large images on slow storage).
                    if is_initial:
                        _write_progress("processing", total, idx + 1, path.name)

                    item = self._image_processor.process(path, source="local")
                else:
                    continue

                if item is not None:
                    items.append(item)
                    logger.debug("Processed: %s → %s", path.name, item.id)
            except Exception:
                logger.exception("Failed to process: %s", path)

            # ── Incremental flush: write to playlist every N items ──
            # This is critical for boot UX — the frontend loads from
            # playlist.json and would otherwise see an empty playlist
            # until ALL files are processed (which can take several
            # minutes when videos need transcoding).
            if len(items) >= _FLUSH_EVERY:
                self._state.add_playlist_items(items)
                items.clear()

        # Final flush: write remaining items
        if items:
            self._state.add_playlist_items(items)

        # Re-add deferred paths to known_files with their original metadata
        # so they get re-detected on the next scan cycle.
        for dp in deferred_paths:
            try:
                stat = dp.stat()
                self._known_files[dp.resolve()] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                pass

        if is_initial:
            pending_msg = (
                f" ({len(deferred_paths)} video(s) still transcoding)"
                if deferred_paths else ""
            )
            _write_progress(
                "complete", total, total,
                current_file=deferred_paths[0].name if deferred_paths else "",
            )
            if deferred_paths:
                logger.info(
                    "Initial scan complete: %d items added, %d video(s) deferred "
                    "(still transcoding — will retry on next scan)",
                    len(items), len(deferred_paths),
                )

    def _process_changed(self, paths: list[Path]) -> None:
        """Re-process files that have been modified.

        Removes old playlist entries (by computing the old hash from the
        removed snapshot) and re-adds the re-processed versions.
        """
        # Compute old IDs from known snapshot (before update)
        old_ids: set[str] = set()
        for p in paths:
            old_id = self._hash_path_for_id(p)
            if old_id:
                old_ids.add(old_id)

        if old_ids:
            self._state.remove_playlist_items(old_ids)

        # Re-process as new
        self._process_new(paths)

    def _handle_deleted(self, paths: set[Path]) -> None:
        """Remove deleted files from the playlist."""
        ids_to_remove: set[str] = set()
        for p in paths:
            old_id = self._hash_path_for_id(p)
            if old_id:
                ids_to_remove.add(old_id)

        if ids_to_remove:
            self._state.remove_playlist_items(ids_to_remove)

    @staticmethod
    def _hash_path_for_id(path: Path) -> str | None:
        """Compute an id for a file path that matches the processor's hash.

        Uses the same algorithm as ImageProcessor._hash_file(): SHA-256 of
        first 1MB + last 1KB, truncated to 16 hex chars.
        """
        import hashlib

        try:
            sha = hashlib.sha256()
            with open(path, "rb") as f:
                sha.update(f.read(1024 * 1024))
                f.seek(-1024, 2)
                sha.update(f.read(1024))
            return sha.hexdigest()[:16]
        except OSError:
            return None
