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

from metixel.backend.processing.thumbnail import (
    generate_image_thumbnail,
    generate_video_thumbnail,
)
from metixel.backend.processing.utils import nice_cmd
from metixel.backend.state import StateManager
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus

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

    def __init__(self, state: StateManager, opt_queue: OptimisationQueue | None = None) -> None:
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

        # Periodic config refresh (so new watch paths added via the web UI
        # are picked up without a backend restart).
        self._last_config_refresh: float = 0.0

    def _resolve_watch_paths(self) -> list[Path]:
        """Resolve the watch_paths config to a list of enabled Path objects.

        Handles both the new object format (``[{"path": "...", "enabled": true}]``)
        and the legacy flat-list format (``["media/", ...]``).

        Always reads from the **live** ``self._state.config`` so that
        watch paths added via the web UI are picked up on the next
        periodic refresh.
        """
        raw = self._state.config.sync["local"].get("watch_paths", [])
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
                # Refresh config periodically so new watch paths added via
                # the web UI are picked up without a backend restart.
                now = time.monotonic()
                if now - self._last_config_refresh >= 30.0:
                    self._refresh_watch_config()
                    self._last_config_refresh = now

                # ── Coordination with the optimisation queue ──────────
                # When the optimiser is actively processing images or
                # videos, add extra delay between folder scans so the
                # two threads don't compete for CPU.  The optimiser
                # always has priority — scanning can wait.
                if (
                    self._opt_queue is not None
                    and self._opt_queue.is_busy
                ):
                    logger.debug(
                        "FolderWatcher: optimiser busy — delaying scan "
                        "by %ds",
                        self._poll_interval,
                    )
                    time.sleep(self._poll_interval)
                    continue

                self._scan()
            except Exception:
                logger.exception("Folder watcher error")
            time.sleep(self._poll_interval)

    def _refresh_watch_config(self) -> None:
        """Re-read the live config and update watch paths if they changed.

        Called periodically so that watch paths added/removed via the web
        UI take effect without restarting the backend.  Also updates the
        poll interval to match any config changes.
        """
        config = self._state.config
        old_paths = [str(p) for p in self._watch_paths]
        new_paths = self._resolve_watch_paths()
        new_interval = config.sync["local"]["poll_interval_seconds"]

        if [str(p) for p in new_paths] != old_paths:
            logger.info(
                "Watch paths changed — was %s, now %s",
                old_paths, [str(p) for p in new_paths],
            )
            self._watch_paths = new_paths
            self._config = config

            # New watch paths need an immediate scan to discover their
            # files and generate thumbnails.  We don't reset
            # _initial_scan_done — the normal diff logic in _scan()
            # will detect files in the new paths as "new".
        elif new_interval != self._poll_interval:
            logger.debug("Poll interval changed: %d → %d", self._poll_interval, new_interval)
            self._poll_interval = new_interval
            self._config = config

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
        - **Thumbnail** — generated here (Phase 1) so the web UI can
          display previews immediately, regardless of whether the file
          later needs optimisation.

        Items are pushed to the ``OptimisationQueue`` for Phase 2/3.
        The queue decides whether each item needs optimisation or is
        ready-to-play.
        """
        # Resolve cache directory from live config (for thumbnail output)
        cache_dir = Path(self._state.config.system.get("cache_dir", "cache/"))

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
                    stub = self._gather_image_metadata(path, cache_dir)
                elif suffix in VIDEO_EXTENSIONS:
                    stub = self._gather_video_metadata(path, cache_dir)
                else:
                    continue

                if stub is not None:
                    # ── Set play strategy at watch stage ────────────
                    # PLAY_ORIGINAL → cached_path = original_path
                    # PLAY_CACHED   → cached_path = expected cache file
                    stub.cached_path = self._resolve_cached_path(stub)
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
        """Push metadata stubs to the OptimisationQueue — or directly to the playlist.

        Items that don't need optimisation (``cached_path == original_path``,
        i.e. PLAY_ORIGINAL) are added directly to the slideshow playlist so
        they appear immediately — even if the optimisation queue worker is
        blocked on a long video transcode.

        * **Images**: PLAY_ORIGINAL images are always ready to play.
        * **Videos**: ALL videos go through the optimisation queue for
          mandatory first/last frame extraction.  ``VideoProcessor.process()``
          skips the expensive transcode step for H.264 videos within
          resolution limits but still generates ``.1.frame`` / ``.2.frame``.

        Items that need optimisation (PLAY_CACHED) are also sent to the
        OptimisationQueue for processing.
        """
        if self._opt_queue is not None:
            ready: list[MediaItem] = []
            needs_opt: list[MediaItem] = []

            for item in stubs:
                if item.cached_path == item.original_path:
                    # PLAY_ORIGINAL — no optimisation needed
                    if item.media_type == MediaType.VIDEO:
                        # Videos always go through the optimisation queue
                        # for frame extraction (first + last frame), even
                        # when the codec/resolution are already optimal.
                        # VideoProcessor.process() will skip the transcode
                        # step but still extract frames.
                        needs_opt.append(item)
                    else:
                        ready.append(item)
                else:
                    # PLAY_CACHED — needs resize / transcode
                    needs_opt.append(item)

            if ready:
                logger.info(
                    "[WATCHFOLDER] %d item(s) ready → playlist (bypass queue)",
                    len(ready),
                )
                for item in ready:
                    logger.debug(
                        "[WATCHFOLDER]  ready | %-5s | %s",
                        item.media_type.value,
                        item.original_path.name,
                    )
                self._state.add_playlist_items(ready)

            if needs_opt:
                logger.debug(
                    "[WATCHFOLDER] enqueue %d stub(s) → OptimisationQueue",
                    len(needs_opt),
                )
                self._opt_queue.enqueue(needs_opt)
        else:
            # No optimisation queue available (e.g. dev mode) —
            # add items directly to the playlist as a fallback.
            logger.warning(
                "OptimisationQueue not available — adding %d item(s) "
                "directly to playlist (unoptimised)",
                len(stubs),
            )
            self._state.add_playlist_items(stubs)

    def _gather_image_metadata(self, path: Path, cache_dir: Path) -> MediaItem | None:
        """Quickly extract image dimensions and generate a thumbnail.

        Uses PIL to read the image header only (does not decode pixel data).
        Generates a 320 px thumbnail in ``<cache_dir>/thumbnails/`` so the
        web UI can show previews immediately.

        Returns a MediaItem stub with metadata + thumbnail, or None on failure.
        """
        try:
            from PIL import Image

            with Image.open(path) as img:
                w, h = img.size

            file_hash = FolderWatcher._hash_path(path)

            # Generate thumbnail during Phase 1 (folder watch)
            thumb_path = generate_image_thumbnail(path, cache_dir)

            logger.debug(
                "[WATCHFOLDER] image  | %4dx%-4d | %s",
                w, h, path.name,
            )
            return MediaItem(
                id=file_hash,
                original_path=path,
                cached_path=path,  # Will be updated by OptimisationQueue if processed
                media_type=MediaType.IMAGE,
                width=w,
                height=h,
                thumbnail_path=thumb_path,
                source="local",
            )
        except Exception:
            logger.debug("Cannot read image metadata: %s", path.name)
            return None

    def _gather_video_metadata(self, path: Path, cache_dir: Path) -> MediaItem | None:
        """Quickly probe a video for dimensions, codec, and duration.

        Uses ffprobe for metadata extraction.  Also generates a thumbnail
        frame (2 s in) so the web UI can show previews immediately, before
        any transcoding decisions are made.

        Returns a MediaItem stub with codec info in exif_data, or None on failure.
        """
        try:
            result = subprocess.run(
                nice_cmd([
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,duration,codec_name",
                    "-of", "json",
                    str(path),
                ]),
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                logger.debug("ffprobe failed for %s", path.name)
                return None

            # ffprobe -of json is field-name–based, immune to column-order
            # variations across ffmpeg versions.
            import json

            probe = json.loads(result.stdout)
            streams = probe.get("streams", [])
            if not streams:
                logger.debug("No video stream in %s", path.name)
                return None
            s = streams[0]
            w = s.get("width", 0) or 0
            h = s.get("height", 0) or 0
            duration = float(s.get("duration", 0) or 0)
            codec = s.get("codec_name", "") or ""

            file_hash = FolderWatcher._hash_path(path)

            # Generate thumbnail during Phase 1 (folder watch)
            thumb_path = generate_video_thumbnail(path, cache_dir)

            logger.debug(
                "[WATCHFOLDER] video  | %4dx%-4d | %-6s | %5.1fs | %s",
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
                thumbnail_path=thumb_path,
                exif_data={"codec_name": codec},
                source="local",
            )
        except Exception:
            logger.debug("Cannot read video metadata: %s", path.name)
            return None

    def _resolve_cached_path(self, item: MediaItem) -> Path:
        """Determine the play path for a media item at the watch stage.

        PLAY_ORIGINAL: The file already meets thresholds or
            optimisation/transcoding is disabled.  ``cached_path``
            stays as the original file path.

        PLAY_CACHED: The file exceeds thresholds AND optimisation/
            transcoding is enabled.  ``cached_path`` points to the
            *expected* cache location (the file may not exist yet —
            the OptimisationQueue creates it).
        """
        config = self._state.config
        cache_dir = Path(config.system.get("cache_dir", "cache/"))
        if not cache_dir.is_absolute():
            cache_dir = Path("/opt/metixel") / cache_dir
        display = config.display
        screen_w = display.get("width") or 1920
        screen_h = display.get("height") or 1080

        if item.media_type == MediaType.IMAGE:
            img_cfg = config.image
            opt_enabled = img_cfg.get("optimisation_enabled", True)
            max_w = img_cfg.get("optimise_max_width", 0) or screen_w
            max_h = img_cfg.get("optimise_max_height", 0) or screen_h
            if opt_enabled and (item.width > max_w or item.height > max_h):
                # PLAY_CACHED — will be optimised
                return cache_dir / "images" / f"{item.id}.jpg"
            # PLAY_ORIGINAL
            return item.original_path

        elif item.media_type == MediaType.VIDEO:
            video_cfg = config.video
            transcode_enabled = video_cfg.get("transcoding_enabled", True)
            max_w = video_cfg.get("transcode_max_width", 0) or screen_w
            max_h = video_cfg.get("transcode_max_height", 0) or screen_h
            if not transcode_enabled:
                # PLAY_ORIGINAL — transcoding is off
                return item.original_path
            # Check whether the video needs transcoding
            codec = (item.exif_data.get("codec_name") or "").lower()
            needs_transcode = False
            if item.width <= 0 or item.height <= 0 or max_w > 0 and item.width > max_w or max_h > 0 and item.height > max_h or codec and codec not in {"h264", "avc", "avc1", "h.264"}:
                needs_transcode = True
            if needs_transcode:
                # PLAY_CACHED — will be transcoded
                return cache_dir / "videos" / f"{item.id}.mp4"
            # PLAY_ORIGINAL
            return item.original_path

        # Unknown media type — play original
        return item.original_path

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

    def _lookup_ids_by_path(self, paths: set[Path]) -> set[str]:
        """Find playlist item IDs matching a set of original file paths.

        Matches by resolved absolute path so symlinks and relative-path
        differences don't prevent matching deleted or changed files.
        """
        resolved = set()
        for p in paths:
            try:
                resolved.add(p.resolve())
            except OSError:
                # File may already be gone — try resolving parent + name
                try:
                    resolved.add(p.parent.resolve() / p.name)
                except OSError:
                    resolved.add(p)

        ids: set[str] = set()
        for item in self._state.get_playlist():
            try:
                if item.original_path.resolve() in resolved:
                    ids.add(item.id)
            except OSError:
                # Path no longer valid — compare the string form as
                # a last resort (both may use the same unresolvable path).
                if str(item.original_path) in {str(p) for p in resolved}:
                    ids.add(item.id)
        return ids

    def _process_changed(self, paths: list[Path]) -> None:
        """Re-process files that have been modified.

        Removes old playlist entries and re-gathers metadata for the
        changed files, pushing them to the OptimisationQueue.
        """
        path_set = set(paths)

        # Remove old playlist entries by path (not by recomputed hash —
        # the file content has changed so a recomputed hash won't match
        # the original playlist entry ID).
        old_ids = self._lookup_ids_by_path(path_set)
        if old_ids:
            self._state.remove_playlist_items(old_ids)

        # Also remove from the optimisation queue (image/video queues)
        # so we don't optimise stale versions of these files.
        if self._opt_queue is not None:
            self._opt_queue.remove_items(old_ids)

        # Re-gather metadata and push to optimisation queue
        self._gather_and_enqueue(paths)

    def _handle_deleted(self, paths: set[Path]) -> None:
        """Remove deleted files from the playlist and optimisation queues.

        Matches playlist entries by original file path — NOT by
        recomputing the content hash, which would fail for files
        that have already been deleted from disk.
        """
        ids_to_remove = self._lookup_ids_by_path(paths)

        if ids_to_remove:
            self._state.remove_playlist_items(ids_to_remove)

            # Also remove from the optimisation queue so we don't waste
            # time processing files that no longer exist.
            if self._opt_queue is not None:
                self._opt_queue.remove_items(ids_to_remove)

        # Clean up cached files for deleted originals (use the resolved
        # IDs to find cache files, not the path-based fallback hash).
        self._cleanup_cached_for_deleted(ids_to_remove)

    def _cleanup_cached_for_deleted(self, item_ids: set[str]) -> None:
        """Remove cached thumbnails and optimised files for deleted originals.

        Uses the resolved media item IDs (content hashes) to find cache
        files.  When the original media file is deleted there's no reason
        to keep its cached derivatives around — they just waste disk space
        and could cause stale thumbnails in the web UI.
        """
        if not item_ids:
            return

        config = self._state.config
        cache_dir = Path(config.system.get("cache_dir", "cache/"))
        if not cache_dir.is_absolute():
            cache_dir = Path("/opt/metixel") / cache_dir

        for file_hash in item_ids:
            # Remove thumbnail
            thumb = cache_dir / "thumbnails" / f"{file_hash}.jpg"
            if thumb.is_file():
                try:
                    thumb.unlink()
                    logger.debug("Cleaned up thumbnail: %s", thumb.name)
                except OSError:
                    pass
            # Remove cached image
            img_cache = cache_dir / "images" / f"{file_hash}.jpg"
            if img_cache.is_file():
                try:
                    img_cache.unlink()
                    logger.debug("Cleaned up cached image: %s", img_cache.name)
                except OSError:
                    pass
            # Remove cached video (and any partial transcodes)
            vid_cache = cache_dir / "videos" / f"{file_hash}.mp4"
            if vid_cache.is_file():
                try:
                    vid_cache.unlink()
                    logger.debug("Cleaned up cached video: %s", vid_cache.name)
                except OSError:
                    pass
            # Remove video frame caches
            for frame in (1, 2):
                frame_cache = cache_dir / "videos" / f"{file_hash}.{frame}.frame"
                if frame_cache.is_file():
                    try:
                        frame_cache.unlink()
                    except OSError:
                        pass
