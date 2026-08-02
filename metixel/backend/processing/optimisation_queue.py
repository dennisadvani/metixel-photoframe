# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Optimisation Queue — background media processing with threshold gating.

Runs as a background thread in the backend daemon.  Receives metadata-only
``MediaItem`` stubs from the ``FolderWatcher`` and decides whether each
item needs optimisation:

* **Images**: optimised if pixel dimensions exceed the configured threshold
  (default: display resolution).  Images at or below the threshold are
  added directly to the slideshow playlist.

* **Videos**: transcoded if pixel dimensions exceed the configured threshold
  OR the codec is not H.264.  Videos within limits in a compatible codec
  are added directly to the slideshow playlist.

Priority order (per user specification):
1. Ready-to-play items are pushed to the playlist immediately.
2. Image optimisation runs first (higher priority).
3. Video optimisation runs after all images are done.
4. If new items arrive during video processing, the current job finishes
   before switching back to images.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from metixel.backend.processing.image import ImageProcessor
from metixel.backend.processing.utils import nice_cmd
from metixel.backend.processing.video import VideoProcessor
from metixel.backend.state import StateManager
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus

logger = logging.getLogger(__name__)

# Accepted media file extensions (mirrors folder_watcher.py)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"}

# Progress file written during optimisation — read by the frontend
# so it can show a progress bar during initial processing.
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


class OptimisationQueue:
    """Background worker that optimises media and feeds the slideshow playlist.

    Thread-safe: the ``FolderWatcher`` can call :meth:`enqueue` from its
    own thread while the worker is processing.
    """

    # How many items to flush to the playlist at once (avoids the frontend
    # waiting for ALL files to finish before showing anything).
    _FLUSH_EVERY = 12

    #: Known H.264 codec names (lowercase) that skip transcoding when the
    #: video is also within the resolution threshold.
    H264_CODECS = {"h264", "avc", "avc1", "h.264"}

    def __init__(self, state: StateManager) -> None:
        self._state = state
        self._config = state.config
        self._running: bool = False

        # Incoming items from FolderWatcher (metadata-only stubs).
        self._incoming: list[MediaItem] = []
        self._incoming_lock = threading.Lock()

        # Separate queues for items that need optimisation.
        self._image_queue: list[MediaItem] = []
        self._video_queue: list[MediaItem] = []
        self._queue_lock = threading.Lock()

        # Event to wake the worker when new items arrive.
        self._wake = threading.Event()

        # Processors (lazy-init)
        self._image_processor: ImageProcessor | None = None
        self._video_processor: VideoProcessor | None = None

        # Configuration-derived thresholds
        self._image_opt_enabled: bool = True
        self._image_max_w: int = 1920
        self._image_max_h: int = 1080
        self._video_transcode_enabled: bool = True
        self._video_max_w: int = 1920
        self._video_max_h: int = 1080

        # Track whether initial processing is done
        self._initial_done: bool = False

    # -- Public API ----------------------------------------------------------

    def run(self) -> None:
        """Main worker loop — runs as a background thread."""
        self._running = True
        self._init_processors()
        self._cleanup_partial_transcodes()
        logger.info("OptimisationQueue worker started")

        # Track when we last refreshed config thresholds
        _last_config_refresh = 0.0
        # Track when we last logged resource usage
        _last_resource_log = 0.0

        while self._running:
            # Refresh config thresholds periodically (every 30s) so
            # web UI changes take effect without a backend restart.
            now = time.monotonic()
            if now - _last_config_refresh >= 30.0:
                self.reload_config()
                _last_config_refresh = now

            # Log system resources every 30s for debugging
            if now - _last_resource_log >= 30.0:
                self._log_resources()
                _last_resource_log = now

            # Drain incoming items into the appropriate queues
            self._classify_incoming()

            # Process image queue first, then video queue
            self._process_image_queue()
            self._process_video_queue()

            # If nothing left to do, wait for new items
            with self._queue_lock:
                pending = len(self._image_queue) + len(self._video_queue)
            with self._incoming_lock:
                pending += len(self._incoming)

            if pending == 0:
                if not self._initial_done:
                    self._initial_done = True
                    logger.info("OptimisationQueue: initial processing complete")
                # Always write completion so the UI reflects the current state.
                _write_progress("complete", 0, 0, "")
                # Wait for wake event (with timeout to allow clean shutdown)
                self._wake.wait(timeout=5.0)
                self._wake.clear()
            else:
                # Brief sleep to avoid busy-waiting
                time.sleep(0.1)

        logger.info("OptimisationQueue worker stopped")

    def stop(self) -> None:
        """Signal the worker loop to stop."""
        self._running = False
        self._wake.set()

    def pause(self) -> None:
        """Temporarily halt processing (used during cache clears).

        Drains the incoming and image/video queues so no stale items
        are processed against now-deleted cached files.
        """
        with self._incoming_lock:
            self._incoming.clear()
        with self._queue_lock:
            self._image_queue.clear()
            self._video_queue.clear()
        logger.debug("OptimisationQueue paused — all queues drained")

    def enqueue(self, items: list[MediaItem]) -> None:
        """Accept metadata-only items from the FolderWatcher.

        Thread-safe — can be called from any thread.
        """
        if not items:
            return
        with self._incoming_lock:
            self._incoming.extend(items)
        self._wake.set()
        logger.debug("OptimisationQueue: received %d item(s)", len(items))

    @property
    def is_busy(self) -> bool:
        """Check whether the optimiser is actively processing or has pending work.

        The folder watcher uses this to throttle its own scan rate:
        when the optimiser is busy, the watcher adds extra delay between
        scans so the two threads don't compete for CPU.
        """
        with self._queue_lock:
            pending = len(self._image_queue) + len(self._video_queue)
        with self._incoming_lock:
            pending += len(self._incoming)
        return pending > 0

    def remove_items(self, item_ids: set[str]) -> int:
        """Remove items from all internal queues by media item ID.

        Called by the FolderWatcher when files are deleted or changed,
        so we don't waste time optimising files that no longer exist
        (or stale versions of changed files).

        Thread-safe.  Returns the number of items removed.
        """
        removed = 0

        # Drain incoming items
        with self._incoming_lock:
            before = len(self._incoming)
            self._incoming = [
                item for item in self._incoming if item.id not in item_ids
            ]
            removed += before - len(self._incoming)

        # Drain image and video optimisation queues
        with self._queue_lock:
            before_img = len(self._image_queue)
            before_vid = len(self._video_queue)
            self._image_queue = [
                item for item in self._image_queue if item.id not in item_ids
            ]
            self._video_queue = [
                item for item in self._video_queue if item.id not in item_ids
            ]
            removed += (before_img - len(self._image_queue))
            removed += (before_vid - len(self._video_queue))

        if removed:
            logger.info(
                "[OPTQ] -%d item(s) removed from optimisation queues "
                "(incoming=%d, image=%d, video=%d)",
                removed,
                len(self._incoming),
                len(self._image_queue),
                len(self._video_queue),
            )
        return removed

    def get_video_queue_status(self) -> dict[str, str]:
        """Return the current transcoding status of every known video.

        Thread-safe snapshot for the web UI media library.  Keys are
        content hashes (``MediaItem.id``); values are one of:

        * ``"queued"`` — waiting in the video queue
        * ``"transcoding"`` — actively being transcoded right now

        Videos not in either state are omitted from the dict entirely.
        """
        result: dict[str, str] = {}

        # Snapshot the queue under lock
        with self._queue_lock:
            for item in self._video_queue:
                result[item.id] = "queued"

        # Snapshot active transcodes (VideoProcessor has its own lock-free set)
        if self._video_processor is not None:
            for file_hash in self._video_processor.active_transcodes():
                result[file_hash] = "transcoding"

        return result

    def reload_config(self) -> None:
        """Refresh optimisation thresholds from the current config.

        Called when the user changes image/video settings via the web UI.
        Re-reads thresholds from ``self._state.config`` without recreating
        the processor objects.  Takes effect on the next classification cycle.
        """
        config = self._state.config
        display = config.display
        sw = display.get("width") or 1920
        sh = display.get("height") or 1080

        # Image threshold config
        image_cfg = config.image
        old_img_enabled = self._image_opt_enabled
        old_img_w = self._image_max_w
        old_img_h = self._image_max_h
        self._image_opt_enabled = image_cfg.get("optimisation_enabled", True)
        self._image_max_w = image_cfg.get("optimise_max_width", 0) or sw
        self._image_max_h = image_cfg.get("optimise_max_height", 0) or sh

        # Video threshold config
        video_cfg = config.video
        old_vid_enabled = self._video_transcode_enabled
        old_vid_w = self._video_max_w
        old_vid_h = self._video_max_h
        self._video_transcode_enabled = video_cfg.get("transcoding_enabled", True)
        self._video_max_w = video_cfg.get("transcode_max_width", 0) or sw
        self._video_max_h = video_cfg.get("transcode_max_height", 0) or sh

        # Update the VideoProcessor's config if it exists
        if self._video_processor is not None:
            self._video_processor.update_config(video_cfg)

        # When transcoding is turned OFF, drain the video queue and
        # push those items directly to the slideshow playlist.  Otherwise
        # they would sit in the queue and still get transcoded (the
        # VideoProcessor checks its own cached flag, but queued items
        # need to be re-classified immediately).
        if old_vid_enabled and not self._video_transcode_enabled:
            self._drain_video_queue_to_playlist()

        # Log changes for debugging
        changes: list[str] = []
        if old_img_enabled != self._image_opt_enabled:
            changes.append(f"image_opt_enabled: {old_img_enabled}→{self._image_opt_enabled}")
        if old_img_w != self._image_max_w or old_img_h != self._image_max_h:
            changes.append(f"image_threshold: {old_img_w}x{old_img_h}→{self._image_max_w}x{self._image_max_h}")
        if old_vid_enabled != self._video_transcode_enabled:
            changes.append(f"video_transcode: {old_vid_enabled}→{self._video_transcode_enabled}")
        if old_vid_w != self._video_max_w or old_vid_h != self._video_max_h:
            changes.append(f"video_threshold: {old_vid_w}x{old_vid_h}→{self._video_max_w}x{self._video_max_h}")
        if changes:
            logger.info("OptimisationQueue config reloaded: %s", "; ".join(changes))

    # -- Internal: initialisation --------------------------------------------

    def _init_processors(self) -> None:
        """Lazy-initialize media processors with current config thresholds."""
        config = self._state.config
        display = config.display
        sw = display.get("width") or 1920
        sh = display.get("height") or 1080

        cache_dir = Path(config.system.get("cache_dir", "cache/"))
        if not cache_dir.is_absolute():
            cache_dir = Path("/opt/metixel") / cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Image threshold config
        image_cfg = config.image
        self._image_opt_enabled = image_cfg.get("optimisation_enabled", True)
        self._image_max_w = image_cfg.get("optimise_max_width", 0) or sw
        self._image_max_h = image_cfg.get("optimise_max_height", 0) or sh

        # Video threshold config
        video_cfg = config.video
        self._video_transcode_enabled = video_cfg.get("transcoding_enabled", True)
        self._video_max_w = video_cfg.get("transcode_max_width", 0) or sw
        self._video_max_h = video_cfg.get("transcode_max_height", 0) or sh

        self._image_processor = ImageProcessor(
            cache_dir, screen_width=sw, screen_height=sh,
        )
        self._video_processor = VideoProcessor(
            cache_dir, screen_width=sw, screen_height=sh, video_config=video_cfg,
        )
        logger.info(
            "OptimisationQueue processors initialised: cache=%s, screen=%dx%d, "
            "image_threshold=%dx%d, video_threshold=%dx%d, transcode=%s",
            cache_dir, sw, sh,
            self._image_max_w, self._image_max_h,
            self._video_max_w, self._video_max_h,
            "enabled" if self._video_transcode_enabled else "disabled",
        )

    def _cleanup_partial_transcodes(self) -> None:
        """Remove incomplete transcode artifacts from cache/videos/ on startup.

        Looks for files smaller than a reasonable minimum (1 KB) and
        ``.tmp`` / ``.partial`` files that may have been left behind
        after an unclean shutdown.
        """
        config = self._state.config
        cache_dir = Path(config.system.get("cache_dir", "cache/"))
        if not cache_dir.is_absolute():
            cache_dir = Path("/opt/metixel") / cache_dir
        video_cache = cache_dir / "videos"
        if not video_cache.is_dir():
            return

        cleaned = 0
        for entry in video_cache.iterdir():
            if not entry.is_file():
                continue
            name = entry.name.lower()
            # Remove temp/partial files
            if name.endswith(".tmp") or name.endswith(".partial"):
                try:
                    entry.unlink()
                    cleaned += 1
                except OSError:
                    pass
                continue
            # Remove files that are suspiciously small (< 1 KB)
            try:
                if entry.stat().st_size < 1024:
                    entry.unlink()
                    cleaned += 1
            except OSError:
                pass

        if cleaned > 0:
            logger.info(
                "Cleaned up %d partial transcode artifact(s) in %s",
                cleaned, video_cache,
            )

    # -- Internal: classification --------------------------------------------

    def _classify_incoming(self) -> None:
        """Drain ``_incoming`` list and sort items into ready / image / video queues.

        Items that don't need optimisation are pushed to the playlist immediately.
        Items that do need optimisation are placed in the appropriate queue.
        """
        with self._incoming_lock:
            if not self._incoming:
                return
            items = self._incoming
            self._incoming = []

        ready: list[MediaItem] = []
        img_opt: list[MediaItem] = []
        vid_opt: list[MediaItem] = []

        for item in items:
            if item.media_type == MediaType.IMAGE:
                if self._image_needs_optimisation(item):
                    logger.debug(
                        "[OPTQ] IMG→opt  | %4dx%-4d > %dx%d | %s",
                        item.width, item.height,
                        self._image_max_w, self._image_max_h,
                        item.original_path.name,
                    )
                    img_opt.append(item)
                else:
                    logger.debug(
                        "[OPTQ] IMG→play | %4dx%-4d ≤ %dx%d | %s",
                        item.width, item.height,
                        self._image_max_w, self._image_max_h,
                        item.original_path.name,
                    )
                    ready.append(item)
            elif item.media_type == MediaType.VIDEO:
                # All videos go through the video queue — frame extraction
                # (first + last frame) is always required, even when the
                # codec/resolution are already optimal.  VideoProcessor.process()
                # will skip the actual transcode step for H.264 videos within
                # resolution limits but still extract frames.
                codec = (item.exif_data.get("codec_name") or "?")
                if self._video_needs_optimisation(item):
                    logger.debug(
                        "[OPTQ] VID→opt  | %4dx%-4d | %-6s | %s",
                        item.width, item.height, codec,
                        item.original_path.name,
                    )
                else:
                    logger.debug(
                        "[OPTQ] VID→frame| %4dx%-4d | %-6s | %s",
                        item.width, item.height, codec,
                        item.original_path.name,
                    )
                vid_opt.append(item)
            else:
                ready.append(item)

        # Push ready-to-play items to the playlist immediately.
        if ready:
            logger.info(
                "[OPTQ] %d ready → playlist (bypass optimisation)",
                len(ready),
            )
            self._state.add_playlist_items(ready)

        # Queue items that need optimisation.
        if img_opt or vid_opt:
            with self._queue_lock:
                old_img = len(self._image_queue)
                old_vid = len(self._video_queue)
                self._image_queue.extend(img_opt)
                self._video_queue.extend(vid_opt)
            if img_opt:
                logger.info(
                    "[OPTQ] %d image(s) queued for optimisation "
                    "(queue: %d→%d, threshold: %dx%d)",
                    len(img_opt), old_img, len(self._image_queue),
                    self._image_max_w, self._image_max_h,
                )
            if vid_opt:
                logger.info(
                    "[OPTQ] %d video(s) queued for optimisation "
                    "(queue: %d→%d, threshold: %dx%d)",
                    len(vid_opt), old_vid, len(self._video_queue),
                    self._video_max_w, self._video_max_h,
                )

    # -- Internal: threshold checks ------------------------------------------

    def _image_needs_optimisation(self, item: MediaItem) -> bool:
        """Check whether an image needs optimisation.

        Respects the play strategy set by the folder watcher:
        if ``cached_path != original_path`` the item is marked
        PLAY_CACHED and needs processing — but only if optimisation
        is actually enabled.
        """
        if not self._image_opt_enabled:
            return False
        if item.cached_path != item.original_path:
            return True
        return False

    def _video_needs_optimisation(self, item: MediaItem) -> bool:
        """Check whether a video needs transcoding.

        Respects the play strategy set by the folder watcher:
        if ``cached_path != original_path`` the item is marked
        PLAY_CACHED and needs processing — but only if transcoding
        is actually enabled.
        """
        if not self._video_transcode_enabled:
            return False
        if item.cached_path != item.original_path:
            return True
        return False

    def _drain_video_queue_to_playlist(self) -> None:
        """Move all queued videos directly to the slideshow playlist.

        Called when the user disables transcoding at runtime.  Videos
        waiting in the queue are marked ``NOT_TRANSCODED`` and pushed
        to the playlist so they can play immediately at original quality.
        """
        with self._queue_lock:
            if not self._video_queue:
                return
            drained = self._video_queue
            self._video_queue = []
            count = len(drained)

        # Mark each video as NOT_TRANSCODED so the frontend knows it's
        # safe to play the original file.
        for item in drained:
            item.transcode_status = TranscodeStatus.NOT_TRANSCODED
            # Reset cached_path to original so the frontend doesn't
            # try to read a non-existent cache file.
            item.cached_path = item.original_path

        self._state.add_playlist_items(drained)
        logger.info(
            "[OPTQ] Transcoding disabled — %d video(s) moved from "
            "optimisation queue → playlist (play original)",
            count,
        )

    # -- Internal: processing ------------------------------------------------

    def _process_image_queue(self) -> None:
        """Process items in the image optimisation queue.

        Processes one batch at a time, flushing to the playlist periodically.
        """
        with self._queue_lock:
            if not self._image_queue:
                return
            # Take a batch
            batch = self._image_queue[: self._FLUSH_EVERY]
            self._image_queue = self._image_queue[self._FLUSH_EVERY :]

        total_remaining = len(batch) + len(self._image_queue)
        self._process_image_batch(batch, total_remaining)

    def _process_image_batch(
        self, batch: list[MediaItem], total_remaining: int,
    ) -> None:
        """Process a batch of images through the ImageProcessor."""
        if self._image_processor is None:
            self._init_processors()
        processor = self._image_processor
        if processor is None:
            return

        optimised: list[MediaItem] = []
        for idx, item in enumerate(batch):
            logger.debug(
                "[OPTQ] IMG opt  | %4dx%-4d | (%d/%d) | %s",
                item.width, item.height,
                idx + 1, len(batch),
                item.original_path.name,
            )
            try:
                result = processor.process(item.original_path, source=item.source)
                if result is not None:
                    optimised.append(result)
                    logger.debug(
                        "[OPTQ] IMG done | %4dx%-4d → cached | %s",
                        result.width, result.height,
                        item.original_path.name,
                    )
            except Exception:
                logger.exception(
                    "Failed to optimise image: %s", item.original_path,
                )
            # Yield between items so the frontend gets CPU time.
            # Sleep duration scales with system load to prevent the
            # Pi from becoming unresponsive during batch processing.
            _sleep = 0.05
            try:
                with open("/proc/loadavg") as f:
                    _load1 = float(f.readline().split()[0])
                # At load 2.0 → sleep 0.10s, load 4.0 → 0.20s,
                # load 7.0 → 0.35s.  Caps at 1.0s.
                _sleep = min(1.0, max(0.05, _load1 * 0.05))
            except (OSError, ValueError, IndexError):
                pass
            time.sleep(_sleep)
            _write_progress(
                "optimising_images",
                total_remaining,
                idx + 1,
                item.original_path.name,
            )

        if optimised:
            self._state.add_playlist_items(optimised)
            logger.debug(
                "Image optimisation: %d processed, added to playlist",
                len(optimised),
            )

    def _process_video_queue(self) -> None:
        """Process items in the video optimisation queue.

        Only runs when the image queue is empty (per priority order).
        Processes one at a time since video transcoding is expensive.
        """
        # Do NOT process videos if there are images waiting
        with self._queue_lock:
            if self._image_queue:
                return
            if not self._video_queue:
                return
            item = self._video_queue.pop(0)

        self._process_single_video(item)

    def _process_single_video(self, item: MediaItem) -> None:
        """Transcode a single video and add it to the playlist."""
        if self._video_processor is None:
            self._init_processors()
        processor = self._video_processor
        if processor is None:
            return

        # Guardrail: skip if already transcoding this file
        file_hash = item.id
        if processor.is_transcoding(file_hash):
            logger.debug(
                "[OPTQ] VID defer | already transcoding | %s",
                item.original_path.name,
            )
            with self._queue_lock:
                self._video_queue.append(item)
            return

        # Count remaining videos (including this one) for progress reporting
        with self._queue_lock:
            remaining = len(self._video_queue) + 1  # +1 for the one we just popped

        logger.debug(
            "[OPTQ] VID opt  | %4dx%-4d | %-6s | %5.1fs | %s",
            item.width, item.height,
            (item.exif_data.get("codec_name") or "?"),
            item.duration_seconds,
            item.original_path.name,
        )
        try:
            _write_progress(
                "transcoding", remaining, 0, item.original_path.name,
            )
            result = processor.process(item.original_path, source=item.source)
            if result is not None:
                # Guard: only add to playlist if the cached file is real.
                # This prevents empty/corrupt stub files (from failed or
                # partial transcodes) from entering the slideshow.
                cached = result.cached_path
                if cached == result.original_path or (
                    cached.is_file() and cached.stat().st_size >= 1024
                ):
                    self._state.add_playlist_items([result])
                    logger.info(
                        "[OPTQ] VID done | %4dx%-4d → cached | %s",
                        result.width, result.height,
                        item.original_path.name,
                    )
                else:
                    logger.warning(
                        "[OPTQ] VID skip | cached file missing or too small "
                        "(%s: %d bytes) — not adding to playlist",
                        cached.name,
                        cached.stat().st_size if cached.is_file() else 0,
                    )
            with self._queue_lock:
                still_remaining = len(self._video_queue)
            _write_progress("transcoding", remaining, remaining - still_remaining, "")
        except Exception:
            logger.exception(
                "Failed to optimise video: %s", item.original_path,
            )

    # -- Internal: helpers ---------------------------------------------------

    @staticmethod
    def _gather_image_metadata(path: Path) -> tuple[int, int]:
        """Quickly extract image dimensions without full processing.

        Returns ``(width, height)`` or ``(0, 0)`` on failure.
        """
        try:
            from PIL import Image

            with Image.open(path) as img:
                return img.size
        except Exception:
            return (0, 0)

    @staticmethod
    def _gather_video_metadata(path: Path) -> dict[str, Any]:
        """Quickly probe a video for dimensions, codec, and duration.

        Returns a dict with keys ``width``, ``height``, ``duration``,
        ``codec_name``.  Values are 0/empty on failure.
        """
        import json
        import subprocess

        try:
            result = subprocess.run(
                nice_cmd([
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries",
                    "stream=width,height,duration,codec_name",
                    "-of", "json",
                    str(path),
                ]),
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                probe = json.loads(result.stdout)
                streams = probe.get("streams", [])
                if streams:
                    s = streams[0]
                    return {
                        "width": s.get("width", 0) or 0,
                        "height": s.get("height", 0) or 0,
                        "duration": float(s.get("duration", 0) or 0),
                        "codec_name": s.get("codec_name", "") or "",
                    }
        except Exception:
            pass
        return {"width": 0, "height": 0, "duration": 0.0, "codec_name": ""}

    @staticmethod
    def _log_resources() -> None:
        """Log CPU, memory, and swap usage at DEBUG level.

        Reads ``/proc/stat``, ``/proc/meminfo``, and ``/proc/loadavg``.
        Silent on non-Linux (dev/Win) systems.
        """
        try:
            # ── CPU utilisation via /proc/stat ───────────────────────
            with open("/proc/stat") as f:
                cpu_line = f.readline()
            parts = cpu_line.split()
            if parts[0] == "cpu" and len(parts) >= 8:
                user, nice, system, idle = (
                    int(parts[1]), int(parts[2]),
                    int(parts[3]), int(parts[4]),
                )
                total = user + nice + system + idle
                active = user + nice + system
                cpu_pct = (active / total * 100) if total > 0 else 0.0
            else:
                cpu_pct = -1.0

            # ── Memory + swap via /proc/meminfo ──────────────────────
            meminfo: dict[str, int] = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    if ":" in line:
                        key, val = line.split(":", 1)
                        val = val.strip().split()[0]
                        meminfo[key.strip()] = int(val)

            total_mb = meminfo.get("MemTotal", 0) // 1024
            avail_mb = meminfo.get("MemAvailable", 0) // 1024
            used_mb = total_mb - avail_mb if total_mb > 0 else 0
            mem_pct = (used_mb / total_mb * 100) if total_mb > 0 else 0.0

            swap_total = meminfo.get("SwapTotal", 0) // 1024
            swap_free = meminfo.get("SwapFree", 0) // 1024
            swap_used = swap_total - swap_free

            # ── Load average ─────────────────────────────────────────
            with open("/proc/loadavg") as f:
                load = f.readline().split()[:3]

            logger.debug(
                "RES: CPU=%.1f%%  MEM=%d/%dMB (%.1f%%)  "
                "SWAP=%d/%dMB  LOAD=%s %s %s",
                cpu_pct,
                used_mb, total_mb, mem_pct,
                swap_used, swap_total,
                load[0], load[1], load[2],
            )
        except (OSError, ValueError, IndexError):
            pass  # Non-Linux or /proc not available
