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

import logging
import threading
import time
from pathlib import Path
from typing import Any

from metixel.backend.processing.image import ImageProcessor
from metixel.backend.processing.utils import nice_cmd
from metixel.backend.processing.video import VideoProcessor, VideoScan
from metixel.backend.state import StateManager
from metixel.shared.display import effective_screen_size
from metixel.shared.io import merge_json
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus
from metixel.shared.paths import resolve_install_path
from metixel.shared.system_stats import read_meminfo, read_system_stats

logger = logging.getLogger(__name__)

# Progress file written during optimisation — read by the frontend
# so it can show a progress bar during initial processing.
PROCESSING_STATUS_PATH = "/run/metixel/processing_status.json"


def _write_progress(phase: str, total: int, processed: int, current_file: str = "") -> None:
    """Atomically write per-phase processing progress.

    Each phase (``scanning``, ``optimising_images``, ``transcoding``)
    tracks its own ``total`` / ``processed`` independently so the web
    UI can show separate progress bars that persist across phase switches
    instead of flickering between them.  Uses the shared locked
    :func:`merge_json` so the folder watcher's ``scanning`` updates are
    never lost to a stale snapshot from this queue's writes.
    """
    try:
        merge_json(
            PROCESSING_STATUS_PATH,
            lambda data: {
                "active": phase,
                "phases": {
                    **data.get("phases", {}),
                    phase: {
                        "total": total,
                        "processed": processed,
                        "current_file": current_file,
                    },
                },
            },
            default={},
        )
    except OSError:
        logger.debug("Could not write processing status — /run/metixel not available?")


class OptimisationQueue:
    """Background worker that optimises media and feeds the slideshow playlist.

    Thread-safe: the ``FolderWatcher`` can call :meth:`enqueue` from its
    own thread while the worker is processing.
    """

    # How many items to flush to the playlist at once (avoids the frontend
    # waiting for ALL files to finish before showing anything).
    _FLUSH_EVERY = 6

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

        # Cumulative progress counters — track total backlog across
        # batches so progress bars show the full queue, not just the
        # current batch of 6.
        self._img_processed = 0  # total images processed in current run
        self._vid_scanned = 0  # total videos scanned (Phase A) in current run
        self._vid_transcoded = 0  # total videos transcoded (Phase B) in current run
        # Whether a two-phase video batch is currently being processed —
        # keeps ``is_busy`` true while the queue drains (the video queue is
        # emptied into the batch upfront).
        self._video_processing = False

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
        self._img_processed = 0
        self._vid_scanned = 0
        self._vid_transcoded = 0
        self._video_processing = False
        logger.debug("OptimisationQueue paused — all queues drained")

    def enqueue(self, items: list[MediaItem]) -> None:
        """Accept metadata-only items from the FolderWatcher.

        Thread-safe — can be called from any thread.  Deduplicates by
        original path so the same file is never queued twice, even if a
        racing scan re-discovers it.
        """
        if not items:
            return
        with self._incoming_lock:
            seen = {item.original_path for item in self._incoming}
            fresh = [item for item in items if item.original_path not in seen]
            if len(fresh) != len(items):
                logger.debug(
                    "OptimisationQueue: dropped %d duplicate(s) already queued",
                    len(items) - len(fresh),
                )
            self._incoming.extend(fresh)
        self._wake.set()
        logger.debug("OptimisationQueue: received %d item(s)", len(fresh))

    @property
    def is_busy(self) -> bool:
        """Check whether the optimiser is actively processing or has pending work.

        The folder watcher uses this to throttle its own scan rate:
        when the optimiser is busy, the watcher adds extra delay between
        scans so the two threads don't compete for CPU.
        """
        with self._queue_lock:
            pending = len(self._image_queue) + len(self._video_queue)
            if self._video_processing:
                pending += 1
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
            self._incoming = [item for item in self._incoming if item.id not in item_ids]
            removed += before - len(self._incoming)

        # Drain image and video optimisation queues
        with self._queue_lock:
            before_img = len(self._image_queue)
            before_vid = len(self._video_queue)
            self._image_queue = [item for item in self._image_queue if item.id not in item_ids]
            self._video_queue = [item for item in self._video_queue if item.id not in item_ids]
            removed += before_img - len(self._image_queue)
            removed += before_vid - len(self._video_queue)

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
        sw, sh = effective_screen_size(display)

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
            changes.append(
                f"image_threshold: {old_img_w}x{old_img_h}→{self._image_max_w}x{self._image_max_h}"
            )
        if old_vid_enabled != self._video_transcode_enabled:
            changes.append(f"video_transcode: {old_vid_enabled}→{self._video_transcode_enabled}")
        if old_vid_w != self._video_max_w or old_vid_h != self._video_max_h:
            changes.append(
                f"video_threshold: {old_vid_w}x{old_vid_h}→{self._video_max_w}x{self._video_max_h}"
            )
        if changes:
            logger.info("OptimisationQueue config reloaded: %s", "; ".join(changes))

    # -- Internal: initialisation --------------------------------------------

    def _init_processors(self) -> None:
        """Lazy-initialize media processors with current config thresholds."""
        config = self._state.config
        display = config.display
        sw, sh = effective_screen_size(display)

        cache_dir = resolve_install_path(config.system.get("cache_dir", "cache/"))
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
            cache_dir,
            screen_width=sw,
            screen_height=sh,
        )
        self._video_processor = VideoProcessor(
            cache_dir,
            screen_width=sw,
            screen_height=sh,
            video_config=video_cfg,
            timeouts=config.timeouts,
        )
        logger.info(
            "OptimisationQueue processors initialised: cache=%s, screen=%dx%d, "
            "image_threshold=%dx%d, video_threshold=%dx%d, transcode=%s",
            cache_dir,
            sw,
            sh,
            self._image_max_w,
            self._image_max_h,
            self._video_max_w,
            self._video_max_h,
            "enabled" if self._video_transcode_enabled else "disabled",
        )

    def _sync_processor_screen_size(self) -> None:
        """Update processors to the current effective (post-rotation) size.

        The frontend writes ``display_info.json`` (with the real rotated
        resolution) some seconds after boot — potentially after the
        processors were constructed with a fallback size.  Re-reading it here
        and pushing the new target into the processors fixes that boot-order
        race, so images/videos re-optimise at the correct dimensions.
        """
        config = self._state.config
        sw, sh = effective_screen_size(config.display)
        if self._image_processor is not None:
            self._image_processor.update_screen_size(sw, sh)
        if self._video_processor is not None:
            self._video_processor.update_screen_size(sw, sh)

    def _cleanup_partial_transcodes(self) -> None:
        """Remove incomplete transcode artifacts from cache/videos/ on startup.

        Looks for files smaller than a reasonable minimum (1 KB) and
        ``.tmp`` / ``.partial`` files that may have been left behind
        after an unclean shutdown.
        """
        config = self._state.config
        cache_dir = resolve_install_path(config.system.get("cache_dir", "cache/"))
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
                cleaned,
                video_cache,
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
                        item.width,
                        item.height,
                        self._image_max_w,
                        self._image_max_h,
                        item.original_path.name,
                    )
                    img_opt.append(item)
                else:
                    logger.debug(
                        "[OPTQ] IMG→play | %4dx%-4d ≤ %dx%d | %s",
                        item.width,
                        item.height,
                        self._image_max_w,
                        self._image_max_h,
                        item.original_path.name,
                    )
                    ready.append(item)
            elif item.media_type == MediaType.VIDEO:
                # All videos go through the video queue — frame extraction
                # (first + last frame) is always required, even when the
                # codec/resolution are already optimal.  VideoProcessor.process()
                # will skip the actual transcode step for H.264 videos within
                # resolution limits but still extract frames.
                codec = item.exif_data.get("codec_name") or "?"
                if self._video_needs_optimisation(item):
                    logger.debug(
                        "[OPTQ] VID→opt  | %4dx%-4d | %-6s | %s",
                        item.width,
                        item.height,
                        codec,
                        item.original_path.name,
                    )
                else:
                    logger.debug(
                        "[OPTQ] VID→frame| %4dx%-4d | %-6s | %s",
                        item.width,
                        item.height,
                        codec,
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
                # Dedup against items already waiting, so a racing scan (or
                # an overlapping watch path) can never enqueue the same file
                # twice for processing.
                img_seen = {item.original_path for item in self._image_queue}
                vid_seen = {item.original_path for item in self._video_queue}
                img_opt = [i for i in img_opt if i.original_path not in img_seen]
                vid_opt = [i for i in vid_opt if i.original_path not in vid_seen]
                old_img = len(self._image_queue)
                old_vid = len(self._video_queue)
                self._image_queue.extend(img_opt)
                self._video_queue.extend(vid_opt)
            if img_opt:
                logger.info(
                    "[OPTQ] %d image(s) queued for optimisation (queue: %d→%d, threshold: %dx%d)",
                    len(img_opt),
                    old_img,
                    len(self._image_queue),
                    self._image_max_w,
                    self._image_max_h,
                )
            if vid_opt:
                logger.info(
                    "[OPTQ] %d video(s) queued for optimisation (queue: %d→%d, threshold: %dx%d)",
                    len(vid_opt),
                    old_vid,
                    len(self._video_queue),
                    self._video_max_w,
                    self._video_max_h,
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
        return item.cached_path != item.original_path

    def _image_requires_optimisation(self, item: MediaItem) -> bool:
        """Whether this image needs a real optimisation (a new cache created).

        A valid, non-trivial cached image (>= 1 KB) means the file was already
        optimised in a previous run — no new work is done, so it should not
        count toward the "Optimising images" progress bar.  Returns True when
        the cache is missing or too small (i.e. real work will run).
        """
        cached = item.cached_path
        return not (cached.is_file() and cached.stat().st_size >= 1024)

    def _video_needs_optimisation(self, item: MediaItem) -> bool:
        """Check whether a video needs transcoding.

        Respects the play strategy set by the folder watcher:
        if ``cached_path != original_path`` the item is marked
        PLAY_CACHED and needs processing — but only if transcoding
        is actually enabled.
        """
        if not self._video_transcode_enabled:
            return False
        return item.cached_path != item.original_path

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
        self,
        batch: list[MediaItem],
        total_remaining: int,
    ) -> None:
        """Process a batch of images through the ImageProcessor."""
        if self._image_processor is None:
            self._init_processors()
        processor = self._image_processor
        if processor is None:
            return

        # The effective (post-rotation) screen size is only known once the
        # frontend has written display_info.json during boot, which may arrive
        # AFTER the processors were constructed.  Re-resolve lazily so images
        # are re-optimised at the current screen target, not a stale fallback.
        self._sync_processor_screen_size()

        # ── Snapshot memory at batch start ────────────────────────────
        _mem_before = self._read_mem_used_mb()
        _batch_start = time.monotonic()

        optimised: list[MediaItem] = []
        # Determine which images in this batch need real optimisation (a new
        # cache file created) vs are cache hits (already optimised).  Only real
        # optimisations count toward the "Optimising images" bar, so re-scanning
        # already-cached images never fills it.
        batch_real = [self._image_requires_optimisation(item) for item in batch]
        real_done = 0
        for idx, item in enumerate(batch):
            _img_start = time.monotonic()
            logger.debug(
                "[OPTQ] IMG opt  | %4dx%-4d | (%d/%d) | %s",
                item.width,
                item.height,
                idx + 1,
                len(batch),
                item.original_path.name,
            )
            try:
                result = processor.process(item.original_path, source=item.source)
                _img_elapsed = time.monotonic() - _img_start
                if result is not None:
                    optimised.append(result)
                    logger.debug(
                        "[OPTQ] IMG done | %4dx%-4d → cached | %5.1fs | %s",
                        result.width,
                        result.height,
                        _img_elapsed,
                        item.original_path.name,
                    )
                else:
                    # ImageProcessor returns None only on failure (corrupt /
                    # worker error / timeout) — cache hits return a MediaItem.
                    logger.warning(
                        "[OPTQ] IMG failed | %s — excluded from playlist",
                        item.original_path.name,
                    )
                    self._state.journal.mark_failed(
                        item.original_path,
                        "Image optimisation failed",
                    )
            except Exception:
                logger.exception(
                    "Failed to optimise image: %s",
                    item.original_path,
                )
                self._state.journal.mark_failed(
                    item.original_path,
                    "Image optimisation error",
                )
            # Yield between items so the frontend gets CPU time.
            # Sleep duration scales with system load to prevent the
            # Pi from becoming unresponsive during batch processing.
            _sleep = 0.05
            _load1 = 0.0
            try:
                with open("/proc/loadavg") as f:
                    _load1 = float(f.readline().split()[0])
                # At load 2.0 → sleep 0.40s, load 4.0 → 0.80s,
                # load 7.0 → 1.00s (capped).  Pi 3 has 4 cores
                # so load > 4 means processes are waiting.
                _sleep = min(1.0, max(0.05, _load1 * 0.2))
            except (OSError, ValueError, IndexError):
                pass
            logger.debug(
                "[OPTQ] yield  | load=%.2f  sleep=%.3fs",
                _load1,
                _sleep,
            )
            time.sleep(_sleep)
            # Cumulative progress — only advance the bar for images that
            # actually created a new cache entry.  Cache hits are still
            # processed (to return a MediaItem and ensure a thumbnail) but
            # don't count, so a re-scan of already-optimised images never
            # fills the "Optimising images" bar.
            if batch_real[idx]:
                real_done += 1
                with self._queue_lock:
                    queue_real = sum(
                        1 for i in self._image_queue if self._image_requires_optimisation(i)
                    )
                _total = (
                    self._img_processed + real_done + (sum(batch_real) - real_done) + queue_real
                )
                _current = self._img_processed + real_done
                _write_progress(
                    "optimising_images",
                    _total,
                    _current,
                    item.original_path.name,
                )

        # ── Batch summary ─────────────────────────────────────────────
        _batch_elapsed = time.monotonic() - _batch_start
        _mem_after = self._read_mem_used_mb()
        _mem_delta = _mem_after - _mem_before
        self._img_processed += real_done

        if optimised:
            self._state.add_playlist_items(optimised)
        logger.debug(
            "[OPTQ] IMG batch done | %d/%d optimised | %.1fs | mem: %+dMB (%d→%d)",
            len(optimised),
            len(batch),
            _batch_elapsed,
            _mem_delta,
            _mem_before,
            _mem_after,
        )

    @staticmethod
    def _read_mem_used_mb() -> int:
        """Read current used memory in MB from /proc/meminfo.

        Returns 0 if /proc/meminfo is unavailable (e.g. Windows).
        """
        mem = read_meminfo()
        total = mem.get("MemTotal", 0) // 1024
        avail = mem.get("MemAvailable", 0) // 1024
        return total - avail if total > 0 else 0

    def _process_video_queue(self) -> None:
        """Process videos in two phases: scan all, then transcode the subset.

        Only runs when the image queue is empty (per priority order).

        * **Phase A (Scanning video)** — every queued video is probed,
          thumbnailed, and frame-extracted, and a full-profile decision is
          made on whether it needs transcoding.  Videos that don't need
          transcoding are added to the playlist as soon as they finish
          scanning (streaming).  Scan failures are recorded in the journal.
        * **Phase B (Transcoding)** — once all scanning is done, the videos
          that need transcoding are encoded and added to the playlist.
        """
        # Do NOT process videos if there are images waiting
        with self._queue_lock:
            if self._image_queue:
                return
            if not self._video_queue:
                return
            batch = self._video_queue
            self._video_queue = []
            self._video_processing = True

        try:
            # Re-resolve the effective screen size in case it was only written
            # by the frontend after this queue started (see _sync_processor_screen_size).
            self._sync_processor_screen_size()

            # ── Phase A: scan every video ─────────────────────────────
            scan_total = self._vid_scanned + len(batch)
            pending_encode: list[VideoScan] = []
            for item in batch:
                self._scan_video(item, pending_encode, scan_total)

            # ── Phase B: encode only the videos that actually need it ──
            transcode_total = self._vid_transcoded + len(pending_encode)
            # Start the Transcoding bar at 0/Total as soon as Phase B begins,
            # so the UI shows "0/N" immediately instead of staying blank until
            # the first encode finishes.
            _write_progress("transcoding", transcode_total, 0, "")
            for scan in pending_encode:
                self._transcode_video(scan, transcode_total)
        finally:
            with self._queue_lock:
                self._video_processing = False

    def _scan_video(
        self,
        item: MediaItem,
        pending_encode: list[VideoScan],
        scan_total: int,
    ) -> None:
        """Scan a single video (Phase A): probe + thumbnail + frames + decide.

        Records the outcome in the processing journal:

        * Scan OK + frames present + no transcode needed → added to the
          playlist immediately (streaming).
        * Scan OK + transcode needed + cache missing/invalid → appended to
          ``pending_encode`` so Phase B actually encodes it (counts in the
          "Transcoding" bar).
        * Scan OK + transcode needed + valid cache already present → the
          cache is reused immediately (NOT counted in the transcode bar).
        * Scan failure / missing frames → marked failed (excluded, shown in
          the status area with a Retry action).
        """
        if self._video_processor is None:
            self._init_processors()
        processor = self._video_processor
        if processor is None:
            return

        journal = self._state.journal
        journal.mark_processing(item.original_path)

        current = self._vid_scanned + 1
        _write_progress(
            "inspecting_videos",
            scan_total,
            current - 1,
            item.original_path.name,
        )

        logger.info(
            "[OPTQ] VID opt  | %4dx%-4d | %-6s | %5.1fs | mem=%dMB | %s",
            item.width,
            item.height,
            (item.exif_data.get("codec_name") or "?"),
            item.duration_seconds,
            self._read_mem_used_mb(),
            item.original_path.name,
        )
        try:
            scan = processor.scan(item.original_path, source=item.source)
            if scan is None:
                journal.mark_failed(item.original_path, "Could not read video metadata")
                return
            if scan.errors or not scan.has_frames:
                reason = "; ".join(scan.errors) if scan.errors else "Frame extraction failed"
                logger.warning(
                    "[OPTQ] VID scan failed | %s — excluded (%s)",
                    item.original_path.name,
                    reason,
                )
                journal.mark_failed(item.original_path, reason)
                return
            if scan.needs_transcode:
                if processor.requires_encode(scan):
                    # Real encode needed — queue for Phase B (counts in the
                    # "Transcoding" bar).
                    pending_encode.append(scan)
                else:
                    # Valid cache already exists — finalize (reuse) without
                    # encoding, so it does NOT appear in the transcode bar.
                    result = processor.transcode(scan)
                    if result is not None:
                        self._state.add_playlist_items([result])
                        logger.info(
                            "[OPTQ] VID done (cache) | %4dx%-4d | %s",
                            result.width,
                            result.height,
                            item.original_path.name,
                        )
            else:
                # No transcode needed — build the NOT_TRANSCODED item and
                # stream it into the playlist right away.
                result = processor.transcode(scan)
                if result is not None:
                    self._state.add_playlist_items([result])
                    logger.info(
                        "[OPTQ] VID done | %4dx%-4d → original | %s",
                        result.width,
                        result.height,
                        item.original_path.name,
                    )
        except Exception:
            logger.exception("Failed to scan video: %s", item.original_path)
            journal.mark_failed(item.original_path, "Video scan error")
        finally:
            self._vid_scanned += 1
            _write_progress("inspecting_videos", scan_total, self._vid_scanned, "")

    def _transcode_video(self, scan: VideoScan, transcode_total: int) -> None:
        """Transcode a scanned video (Phase B) and add it to the playlist."""
        processor = self._video_processor
        if processor is None:
            return
        journal = self._state.journal
        item_path = scan.source_path

        # Guardrail: skip if already transcoding this file
        if processor.is_transcoding(scan.file_hash):
            logger.debug(
                "[OPTQ] VID defer | already transcoding | %s",
                item_path.name,
            )
            return

        current = self._vid_transcoded + 1
        _write_progress(
            "transcoding",
            transcode_total,
            current - 1,
            item_path.name,
        )

        try:
            result = processor.transcode(scan)
            if result is None:
                journal.mark_failed(item_path, "Video processing error")
            elif result.transcode_status == TranscodeStatus.FAILED:
                # A failed transcode must NEVER play at native resolution.
                logger.warning(
                    "[OPTQ] VID failed | %s — excluded from playlist (%s)",
                    item_path.name,
                    result.failure_reason or "transcode failed",
                )
                journal.mark_failed(
                    item_path,
                    result.failure_reason or "Transcode failed",
                )
            else:
                # Guard: only add to playlist if the cached file is real.
                # This prevents empty/corrupt stub files (from failed or
                # partial transcodes) from entering the slideshow.
                cached = result.cached_path
                if cached == result.original_path or (
                    cached.is_file() and cached.stat().st_size >= 1024
                ):
                    self._state.add_playlist_items([result])
                    status = result.transcode_status.value if result.transcode_status else None
                    journal.mark_ready(item_path, status)
                    logger.info(
                        "[OPTQ] VID done | %4dx%-4d → cached | %s",
                        result.width,
                        result.height,
                        item_path.name,
                    )
                else:
                    logger.warning(
                        "[OPTQ] VID skip | cached file missing or too small "
                        "(%s: %d bytes) — not adding to playlist",
                        cached.name,
                        cached.stat().st_size if cached.is_file() else 0,
                    )
                    journal.mark_failed(
                        item_path,
                        "Cached file missing or too small after processing",
                    )
        except Exception:
            logger.exception(
                "Failed to optimise video: %s",
                item_path,
            )
            journal.mark_failed(item_path, "Video processing error")
        finally:
            self._vid_transcoded += 1
            _write_progress("transcoding", transcode_total, self._vid_transcoded, "")

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
                nice_cmd(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-select_streams",
                        "v:0",
                        "-show_entries",
                        "stream=width,height,duration,codec_name",
                        "-of",
                        "json",
                        str(path),
                    ]
                ),
                capture_output=True,
                text=True,
                timeout=10,
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
        stats = read_system_stats()
        if stats is None:
            return  # Non-Linux or /proc not available
        load = stats["loadavg"]
        logger.debug(
            "RES: CPU=%.1f%%  MEM=%d/%dMB (%.1f%%)  SWAP=%d/%dMB  LOAD=%s %s %s",
            stats["cpu_percent"],
            stats["mem_used_mb"],
            stats["mem_total_mb"],
            stats["mem_percent"],
            stats["swap_used_mb"],
            stats["swap_total_mb"],
            load[0],
            load[1],
            load[2],
        )
