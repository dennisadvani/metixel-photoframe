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
from metixel.backend.processing.video import VideoProcessor
from metixel.backend.state import StateManager
from metixel.shared.models import MediaItem, MediaType

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

        while self._running:
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
                    _write_progress("complete", 0, 0, "")
                    logger.info("OptimisationQueue: initial processing complete")
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
                if self._video_needs_optimisation(item):
                    codec = (item.exif_data.get("codec_name") or "?")
                    logger.debug(
                        "[OPTQ] VID→opt  | %4dx%-4d | %-6s | %s",
                        item.width, item.height, codec,
                        item.original_path.name,
                    )
                    vid_opt.append(item)
                else:
                    codec = (item.exif_data.get("codec_name") or "?")
                    logger.debug(
                        "[OPTQ] VID→play | %4dx%-4d | %-6s | %s",
                        item.width, item.height, codec,
                        item.original_path.name,
                    )
                    ready.append(item)
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
        """Check whether an image exceeds the optimisation threshold.

        Delegates to ``ImageProcessor.needs_optimisation()``.
        """
        if not self._image_opt_enabled:
            return False
        return ImageProcessor.needs_optimisation(
            item.width, item.height,
            max_width=self._image_max_w,
            max_height=self._image_max_h,
        )

    def _video_needs_optimisation(self, item: MediaItem) -> bool:
        """Check whether a video needs transcoding.

        Delegates to ``VideoProcessor.needs_optimisation()``.
        """
        if not self._video_transcode_enabled:
            return False
        codec = (item.exif_data.get("codec_name") or "")
        return VideoProcessor.needs_optimisation(
            item.width, item.height,
            codec_name=codec,
            max_width=self._video_max_w,
            max_height=self._video_max_h,
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
            _write_progress(
                "optimising_images",
                total_remaining + len(optimised),
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

        logger.debug(
            "[OPTQ] VID opt  | %4dx%-4d | %-6s | %5.1fs | %s",
            item.width, item.height,
            (item.exif_data.get("codec_name") or "?"),
            item.duration_seconds,
            item.original_path.name,
        )
        try:
            _write_progress(
                "transcoding", 1, 0, item.original_path.name,
            )
            result = processor.process(item.original_path, source=item.source)
            if result is not None:
                self._state.add_playlist_items([result])
                logger.info(
                    "[OPTQ] VID done | %4dx%-4d → cached | %s",
                    result.width, result.height,
                    item.original_path.name,
                )
            _write_progress("transcoding", 1, 1, "")
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
        import subprocess

        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries",
                    "stream=width,height,duration,codec_name",
                    "-of", "csv=p=0",
                    str(path),
                ],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split(",")
                return {
                    "width": int(parts[0]) if len(parts) > 0 and parts[0] else 0,
                    "height": int(parts[1]) if len(parts) > 1 and parts[1] else 0,
                    "duration": float(parts[2]) if len(parts) > 2 and parts[2] else 0.0,
                    "codec_name": parts[3].strip() if len(parts) > 3 else "",
                }
        except Exception:
            pass
        return {"width": 0, "height": 0, "duration": 0.0, "codec_name": ""}
