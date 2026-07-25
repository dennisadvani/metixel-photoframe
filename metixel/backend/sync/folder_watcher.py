# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Local folder watcher for media synchronization.

Monitors configured directories for new/changed media files using inotify
(via the watchdog library) with a polling fallback for network mounts.

**Phase 1 (Watch)**: Gathers minimal metadata (file type, pixel dimensions,
video codec) but does NOT process, resize, or transcode.  Discovered items
are pushed to the ``OptimisationQueue`` which handles Phase 2 (optimise)
and Phase 3 (queue to slideshow playlist).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from metixel.backend.state import StateManager
from metixel.shared.models import MediaItem, MediaType

if TYPE_CHECKING:
    from metixel.backend.processing.optimisation_queue import OptimisationQueue

logger = logging.getLogger(__name__)

# Accepted media file extensions
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Progress file written during initial scan — read by the frontend
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

    **Phase 1 only** — gathers minimal metadata (type, dimensions, codec)
    and pushes items to the ``OptimisationQueue`` for the rest of the
    pipeline.  Does NOT resize, transcode, or add items to the playlist
    directly.

    Maintains a snapshot of known files (path → mtime/size) and compares
    each scan against the previous state to detect:
    - **New files**: metadata gathered, pushed to OptimisationQueue
    - **Modified files**: old playlist entries removed, re-scanned
    - **Deleted files**: removed from the playlist
    """

    def __init__(self, state: StateManager, opt_queue: "OptimisationQueue | None" = None) -> None:
        self._state = state
        self._config = state.config
        self._opt_queue = opt_queue
        self._running: bool = False

        # Resolve watch paths from config (new object format with enabled flag).
        self._watch_paths: list[Path] = self._resolve_watch_paths()
        self._poll_interval: int = self._config.sync["local"]["poll_interval_seconds"]

        # File state snapshot: {absolute_path: (mtime_ns, size_bytes)}
        self._known_files: dict[Path, tuple[int, int]] = {}

        # Track whether the initial full scan has completed
        self._initial_scan_done: bool = False

    def _resolve_watch_paths(self) -> list[Path]:
        """Resolve the watch_paths config to a list of enabled Path objects.

        Handles both the new object format (``[{"path": "...", "enabled": true}]``)
        and the legacy flat-list format (``["media/", ...]``).
        """
        raw = self._config.sync["local"].get("watch_paths", [])
        paths: list[Path] = []
        for entry in raw:
            if isinstance(entry, dict):
                if entry.get("enabled", True):
                    p = Path(entry["path"])
                    if not p.is_absolute():
                        p = Path("/opt/metixel") / p
                    paths.append(p)
            elif isinstance(entry, str):
                # Legacy flat-list format — treat as enabled
                p = Path(entry)
                if not p.is_absolute():
                    p = Path("/opt/metixel") / p
                paths.append(p)
        return paths

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
        snapshot and gathers metadata for all discovered files.  Subsequent
        scans diff against the snapshot to detect incremental changes.
        """
        # 1. Walk all enabled watch paths and discover current files
        current_files: dict[Path, tuple[int, int]] = {}
        for watch_path in self._watch_paths:
            if not watch_path.exists():
                logger.debug("Watch path not found: %s", watch_path)
                continue
            self._walk_path(watch_path, current_files)

        # 2. If this is the initial scan, gather metadata for everything
        if not self._initial_scan_done:
            self._initial_scan_done = True
            self._known_files = dict(current_files)

            total = len(current_files)
            if total > 0:
                _write_progress("scanning", total, 0, "")
                logger.info(
                    "Initial scan: discovered %d media files across %d watch paths",
                    total, len(self._watch_paths),
                )
                self._gather_and_enqueue(list(current_files.keys()), is_initial=True)
            else:
                _write_progress("complete", 0, 0, "")
                logger.info("Initial scan: no media files found")
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
            self._gather_and_enqueue(list(new_paths))

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
        """Recursively walk a directory, collecting media files with stat metadata.

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

    # -- Metadata gathering --------------------------------------------------

    def _gather_and_enqueue(
        self, paths: list[Path], is_initial: bool = False,
    ) -> None:
        """Gather minimal metadata for each file and push to the OptimisationQueue.

        Does NOT process/resize/transcode — just determines:
        - Media type (image vs video) by extension
        - Pixel dimensions (PIL for images, ffprobe for videos)
        - Video codec name (for threshold gating)

        Items are pushed to the ``OptimisationQueue`` for Phase 2/3.
        The queue decides whether each item needs optimisation or is
        ready-to-play.
        """
        # Sort: images first so the optimisation queue can prioritise them.
        paths.sort(key=lambda p: (0 if p.suffix.lower() in IMAGE_EXTENSIONS else 1, p))

        total = len(paths)
        stubs: list[MediaItem] = []

        for idx, path in enumerate(paths):
            suffix = path.suffix.lower()
            try:
                if is_initial:
                    _write_progress("scanning", total, idx + 1, path.name)

                if suffix in IMAGE_EXTENSIONS:
                    stub = self._gather_image_metadata(path)
                elif suffix in VIDEO_EXTENSIONS:
                    stub = self._gather_video_metadata(path)
                else:
                    continue

                if stub is not None:
                    stubs.append(stub)
            except Exception:
                logger.exception("Failed to gather metadata: %s", path)

            # Push to optimisation queue in batches to avoid holding
            # too many items in memory at once.
            if len(stubs) >= 24:
                self._enqueue_stubs(stubs)
                stubs.clear()

        # Final push
        if stubs:
            self._enqueue_stubs(stubs)

        if is_initial:
            _write_progress("complete", total, total, "")
            logger.info(
                "Initial metadata scan complete: %d files discovered",
                total,
            )

    def _enqueue_stubs(self, stubs: list[MediaItem]) -> None:
        """Push metadata stubs to the OptimisationQueue."""
        if self._opt_queue is not None:
            logger.debug(
                "[WATCH] enqueue %d stub(s) → OptimisationQueue",
                len(stubs),
            )
            self._opt_queue.enqueue(stubs)
        else:
            # No optimisation queue available (e.g. dev mode) —
            # add items directly to the playlist as a fallback.
            logger.warning(
                "OptimisationQueue not available — adding %d item(s) "
                "directly to playlist (unoptimised)",
                len(stubs),
            )
            self._state.add_playlist_items(stubs)

    @staticmethod
    def _gather_image_metadata(path: Path) -> MediaItem | None:
        """Quickly extract image dimensions without full processing.

        Uses PIL to read the image header only (does not decode pixel data).
        Returns a MediaItem stub with minimal metadata, or None on failure.
        """
        try:
            from PIL import Image

            with Image.open(path) as img:
                w, h = img.size

            file_hash = FolderWatcher._hash_path(path)
            logger.debug(
                "[WATCH] image  | %4dx%-4d | %s",
                w, h, path.name,
            )
            return MediaItem(
                id=file_hash,
                original_path=path,
                cached_path=path,  # Will be updated by OptimisationQueue if processed
                media_type=MediaType.IMAGE,
                width=w,
                height=h,
                source="local",
            )
        except Exception:
            logger.debug("Cannot read image metadata: %s", path.name)
            return None

    @staticmethod
    def _gather_video_metadata(path: Path) -> MediaItem | None:
        """Quickly probe a video for dimensions, codec, and duration.

        Uses ffprobe for metadata extraction.  Returns a MediaItem stub
        with codec info in exif_data, or None on failure.
        """
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,duration,codec_name",
                    "-of", "csv=p=0",
                    str(path),
                ],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                logger.debug("ffprobe failed for %s", path.name)
                return None

            parts = result.stdout.strip().split(",")
            w = int(parts[0]) if len(parts) > 0 and parts[0] else 0
            h = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            duration = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
            codec = parts[3].strip() if len(parts) > 3 else ""

            file_hash = FolderWatcher._hash_path(path)
            logger.debug(
                "[WATCH] video  | %4dx%-4d | %-6s | %5.1fs | %s",
                w, h, codec or "?", duration, path.name,
            )
            return MediaItem(
                id=file_hash,
                original_path=path,
                cached_path=path,  # Will be updated by OptimisationQueue if transcoded
                media_type=MediaType.VIDEO,
                width=w,
                height=h,
                duration_seconds=duration,
                exif_data={"codec_name": codec},
                source="local",
            )
        except Exception:
            logger.debug("Cannot read video metadata: %s", path.name)
            return None

    @staticmethod
    def _hash_path(path: Path) -> str:
        """Compute a short content hash for a file (first 1MB + last 1KB)."""
        sha = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                sha.update(f.read(1024 * 1024))
                f.seek(-1024, 2)
                sha.update(f.read(1024))
        except OSError:
            # Fall back to path-based hash if file can't be read
            sha.update(str(path).encode())
        return sha.hexdigest()[:16]

    def _process_changed(self, paths: list[Path]) -> None:
        """Re-process files that have been modified.

        Removes old playlist entries and re-gathers metadata for the
        changed files, pushing them to the OptimisationQueue.
        """
        # Compute old IDs from known snapshot (before update)
        old_ids: set[str] = set()
        for p in paths:
            old_id = self._hash_path(p)
            if old_id:
                old_ids.add(old_id)

        if old_ids:
            self._state.remove_playlist_items(old_ids)

        # Re-gather metadata and push to optimisation queue
        self._gather_and_enqueue(paths)

    def _handle_deleted(self, paths: set[Path]) -> None:
        """Remove deleted files from the playlist."""
        ids_to_remove: set[str] = set()
        for p in paths:
            old_id = self._hash_path(p)
            if old_id:
                ids_to_remove.add(old_id)

        if ids_to_remove:
            self._state.remove_playlist_items(ids_to_remove)
