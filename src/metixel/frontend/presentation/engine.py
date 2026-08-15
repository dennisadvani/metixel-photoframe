# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Presentation Engine — two-texture ping-pong slideshow with video support.

This module is the composition root (facade): ``PresentationEngine`` inherits the
focused mixins (queue, scheduler, rendering, preload, video state) and keeps the
public API plus the shared state (declared in ``base.BaseEngineState``).
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

import numpy as np

from metixel.display.backend import DisplayBackend
from metixel.frontend.presentation.layout import LayoutEngine
from metixel.frontend.presentation.preload import TexturePreloaderMixin
from metixel.frontend.presentation.queue import PlaylistControllerMixin
from metixel.frontend.presentation.rendering import FrameRendererMixin
from metixel.frontend.presentation.scheduler import SlideshowSchedulerMixin
from metixel.frontend.presentation.transitions import TransitionEngine
from metixel.frontend.presentation.video_state import _VIDEO_IDLE, VideoStateMachineMixin
from metixel.shared.config import Config
from metixel.shared.io import atomic_write_json
from metixel.shared.media import IMAGE_EXTENSIONS, content_hash
from metixel.shared.models import MediaItem, MediaType
from metixel.shared.paths import resolve_install_path, run_path

logger = logging.getLogger(__name__)


class PresentationEngine(
    PlaylistControllerMixin,
    SlideshowSchedulerMixin,
    FrameRendererMixin,
    TexturePreloaderMixin,
    VideoStateMachineMixin,
):
    """Two-texture ping-pong slideshow.

    Exactly two GPU texture slots are used — the *active* slot is drawn
    on screen while the *inactive* slot is preloaded with the next image
    or video frame. Transitions crossfade between the two slots.
    """

    def __init__(self, config: Config, backend: DisplayBackend) -> None:
        self._config = config
        self._backend = backend

        sw = backend.width or config.display.get("width") or 1920
        sh = backend.height or config.display.get("height") or 1080

        fit_mode = config.slideshow.get("fit_mode", "contain")

        logger.info(
            "PresentationEngine: resolution=%dx%d fit_mode=%s transition=%s slide_duration=%ds",
            sw,
            sh,
            fit_mode,
            config.slideshow.get("transition_style", "crossfade"),
            config.slideshow.get("image_duration_seconds", 30),
        )

        self._layout = LayoutEngine(screen_w=sw, screen_h=sh)
        self._transitions = TransitionEngine(config)

        # --- Two-texture slots ---
        self._tex: list[Any | None] = [None, None]
        self._tex_item: list[MediaItem | None] = [None, None]  # which item each slot holds
        self._active: int = 0

        # --- Queue state ---
        self._queue: list[MediaItem] = []
        self._current_idx: int = -1
        self._paused: bool = False
        self._item_start_time: float = 0.0
        self._queue_loaded: bool = False  # True after first set_queue() call

        # --- Preload (CPU worker → GPU upload on main thread) ---
        self._preload_thread: threading.Thread | None = None
        self._preload_lock = threading.Lock()
        self._preload_array: np.ndarray | None = None
        self._preload_cache_key: str = ""

        # --- Layout cache ---
        self._layout_cache: dict[tuple[int, str], dict] = {}
        self._fit_mode_cache: str = fit_mode
        self._screen_ratio: float = sw / max(sh, 1)

        # --- Rate-limited warnings ---
        self._transition_stall_logged: bool = False

        # --- Non-blocking video state machine ---
        self._video_state: int = _VIDEO_IDLE
        self._video_proc: subprocess.Popen[bytes] | None = None
        self._video_player: Any = None  # VlcVideoPlayer instance
        self._video_launch_at: float = 0.0  # monotonic when VLC launched
        self._video_swap_at: float = 0.0  # monotonic timestamp for last-frame swap
        self._video_item: MediaItem | None = None
        self._video_path: str = ""
        self._video_vw: int = 0
        self._video_vh: int = 0
        self._video_duration: float = 0.0
        self._video_paused: bool = False  # True when SIGSTOP sent to VLC
        self._video_last_frame_loaded: bool = False
        self._video_last_frame_tex: Any | None = None  # preloaded before VLC starts

    @property
    def _cache_base(self) -> str:
        """Resolved cache directory from config (always absolute)."""
        cache_dir = self._config.system.get("cache_dir", "cache/")
        path = Path(cache_dir)
        if not path.is_absolute():
            path = Path("/opt/metixel") / path
        return str(path)

    @property
    def _inactive(self) -> int:
        """Index of the texture slot NOT currently displayed."""
        return 1 - self._active

    def _write_current_media(self) -> None:
        try:
            if self._current_idx < 0 or not self._queue:
                data: dict[str, Any] = {
                    "file": None,
                    "index": -1,
                    "total": 0,
                    "paused": self._paused,
                    "media_type": None,
                    "thumbnail_path": None,
                }
            else:
                item = self._queue[self._current_idx]
                # Resolve the thumbnail path:
                # 1. Use the item's thumbnail_path (set by ImageProcessor
                #    or merged from backend playlist).
                # 2. Fall back to the hash-based thumbnail in cache/thumbnails/.
                # 3. For videos: last resort is the raw first-frame cache (.1.frame).
                thumb = None
                if item.thumbnail_path is not None:
                    thumb = str(item.thumbnail_path)
                else:
                    # Fall back to hash-based thumbnail lookup.
                    # CRITICAL: use original_path (NOT cached_path) because
                    # thumbnails are always named after the ORIGINAL file's
                    # content hash.  cached_path may point to the optimised
                    # cache file whose content differs from the original,
                    # producing a different hash that won't match any thumbnail.
                    try:
                        file_hash = content_hash(item.original_path)
                        hash_thumb = resolve_install_path("cache/thumbnails") / f"{file_hash}.jpg"
                        if hash_thumb.exists():
                            thumb = str(hash_thumb)
                    except OSError:
                        pass
                # Video-only: fall back to first-frame cache (backend-generated)
                if (
                    thumb is None
                    and item.media_type == MediaType.VIDEO
                    and item.first_frame_path is not None
                    and item.first_frame_path.exists()
                ):
                    thumb = str(item.first_frame_path)
                data = {
                    "file": str(item.original_path.name) if item.original_path else "unknown",
                    "index": self._current_idx,
                    "total": len(self._queue),
                    "paused": self._paused,
                    "media_type": item.media_type.value,
                    "thumbnail_path": thumb,
                }
            atomic_write_json(run_path("current_media.json"), data)
        except OSError:
            pass

    def scan_folder(self, folder_path: Path) -> list[MediaItem]:
        """Scan a folder and build lightweight MediaItem stubs from its contents.

        This is a dev/debug fallback — in normal operation the backend
        ``OptimisationQueue`` handles media discovery and processing.

        The scan is deliberately lightweight (no PIL, no content hashing)
        to avoid competing with the backend optimiser for CPU/memory/I/O
        on resource-constrained hardware.  Stub items are replaced by
        backend-processed items via playlist hot-reload.

        Videos are intentionally excluded — they must come through the
        backend pipeline so transcoding, guardrails, and playlist gating
        are applied before playback.
        """
        items: list[MediaItem] = []
        if not folder_path.exists():
            logger.warning("Media folder not found: %s", folder_path)
            return items

        for entry in sorted(folder_path.rglob("*")):
            if not entry.is_file():
                continue
            suffix = entry.suffix.lower()
            if suffix not in IMAGE_EXTENSIONS:
                continue
            try:
                # Fast identity: use file path hash (NOT content hash).
                # Content hashing reads 1 MB per file and competes with
                # the backend optimiser for I/O — skipped intentionally.
                # Backend-processed items will replace these stubs via
                # playlist hot-reload with correct content-hash IDs.
                file_id = hashlib.sha256(str(entry).encode()).hexdigest()[:16]

                items.append(
                    MediaItem(
                        id=file_id,
                        original_path=entry,
                        cached_path=entry,  # No cache check — backend will replace
                        media_type=MediaType.IMAGE,
                        width=0,
                        height=0,  # No PIL — backend provides dimensions
                        duration_seconds=0.0,
                        thumbnail_path=None,
                        source="local",
                    )
                )
            except OSError:
                logger.debug("Skipping unreadable file: %s", entry)

        logger.info(
            "Folder scan (dev fallback): %d images in %s",
            len(items),
            folder_path,
        )
        return items

    def reload_config(self, config: Config) -> None:
        # Determine old/new video playback status from new video section
        # with fallback to legacy slideshow keys
        def _get_playback(cfg):
            if hasattr(cfg, "video") and cfg.video:
                return cfg.video.get("playback_enabled", True)
            return cfg.slideshow.get("video_playback_enabled", True)

        old_video = _get_playback(self._config)
        new_video = _get_playback(config)
        self._config = config
        self._transitions.reload_config(config)
        self._fit_mode_cache = config.slideshow.get("fit_mode", "contain")
        self._layout_cache.clear()

        if old_video != new_video:
            logger.info(
                "Video playback toggled (%s → %s) — regenerating queue",
                old_video,
                new_video,
            )
            from metixel.shared.config import resolve_watch_paths

            watch_paths = resolve_watch_paths(config)
            if watch_paths:
                folder_path = watch_paths[0]
                items = self.scan_folder(folder_path)
                self.set_queue(items)

        logger.debug("Presentation engine config reloaded")
