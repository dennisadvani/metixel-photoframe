# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Presentation Engine — two-texture ping-pong slideshow with video support.

Uses exactly two GPU texture slots that alternate: the active slot is
displayed while the inactive slot is preloaded with the next image or
video frame.  Transitions crossfade between the two slots.  Video first
and last frames are cached to disk (``.1.frame`` / ``.2.frame``) and
treated like normal images — VLC simply plays on top of the slideshow.

Video playback is driven by a non-blocking state machine so the render
loop stays responsive to IPC control commands (next/prev/pause/resume)
at all times.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import random
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from metixel.display.backend import DisplayBackend
from metixel.frontend.presentation.layout import LayoutEngine
from metixel.frontend.presentation.transitions import TransitionEngine
from metixel.shared.config import Config
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Video playback state machine
# ---------------------------------------------------------------------------

#: No video is playing.
_VIDEO_IDLE = 0
#: VLC launched; waiting for first frame before starting swap timer.
_VIDEO_WAITING = 1
#: VLC is running; waiting for the last-frame swap time.
_VIDEO_PLAYING = 2
#: Last frame has been swapped under VLC; waiting for VLC to exit.
_VIDEO_SWAPPED = 3
#: Max seconds to wait for VLC to confirm it has started (CPU contention).
_VLC_START_TIMEOUT_DEFAULT = 30.0  # fallback if config.timeouts missing


def _hash_image_file(path: Path) -> str:
    """Compute a short content hash for an image file.

    Uses first 1MB + last 1KB, matching ``ImageProcessor._hash_file()``.
    Handles files smaller than 1KB gracefully.
    """
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        chunk = f.read(1024 * 1024)
        sha.update(chunk)
        if len(chunk) >= 1024:
            f.seek(-1024, 2)
            sha.update(f.read(1024))
    return sha.hexdigest()[:16]


# ---------------------------------------------------------------------------
# PresentationEngine
# ---------------------------------------------------------------------------


class PresentationEngine:
    """Two-texture ping-pong slideshow.

    Exactly two GPU texture slots are used — the *active* slot is drawn
    on screen while the *inactive* slot is preloaded with the next image
    or video frame.  Transitions crossfade between the two slots.

    Video frames are cached to disk as ``.1.frame`` / ``.2.frame`` JPEGs
    so extraction happens at most once per video file.  VLC plays on top
    of the slideshow; frame swaps underneath are invisible to the user.
    """

    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}

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

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Fit mode
    # ------------------------------------------------------------------

    def _resolve_fit_mode(self, item: MediaItem) -> str:
        mode = self._fit_mode_cache
        if mode != "cover":
            return mode
        if not self._config.slideshow.get("smart_cover", True):
            return mode
        if item.width <= 0 or item.height <= 0:
            return mode
        img_ratio = item.width / max(item.height, 1)
        if self._screen_ratio > 1.0 and img_ratio <= 1.0:
            return "contain"
        if self._screen_ratio < 1.0 and img_ratio >= 1.0:
            return "contain"
        return mode

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def set_queue(self, items: list[MediaItem]) -> None:
        self._queue = list(items)

        # Stop any running video before replacing the queue.
        if self._video_state != _VIDEO_IDLE:
            self._video_stop()

        # ── Video guardrails ─────────────────────────────────────────
        # Read video config (new section; fall back to slideshow legacy keys)
        video_cfg = self._config.video if hasattr(self._config, "video") else {}
        playback_enabled = video_cfg.get(
            "playback_enabled",
            self._config.slideshow.get("video_playback_enabled", True),
        )
        transcoding_enabled = video_cfg.get("transcoding_enabled", True)
        max_duration = video_cfg.get(
            "max_duration_seconds",
            self._config.slideshow.get("video_max_duration_seconds", 0),
        )

        filtered: list[MediaItem] = []
        skipped_playback: int = 0
        skipped_transcode: int = 0
        skipped_duration: int = 0
        skipped_ready: int = 0

        for item in self._queue:
            if item.media_type != MediaType.VIDEO:
                filtered.append(item)
                continue

            # 1. Video playback master switch
            if not playback_enabled:
                skipped_playback += 1
                continue

            # 2. Max duration filter
            if max_duration > 0 and item.duration_seconds > max_duration:
                skipped_duration += 1
                continue

            # 3. Transcoding guardrails
            if transcoding_enabled:
                # Only play transcoded videos (or failed ones that
                # will be played as original)
                if not item.is_ready_to_play:
                    skipped_transcode += 1
                    continue
                # Also skip if the transcode status is FAILED but
                # transcoding is explicitly requested (user wants
                # optimised videos, not originals)
                if item.transcode_status == TranscodeStatus.FAILED:
                    logger.debug(
                        "Skipping %s — transcode failed and transcoding is required",
                        item.original_path.name,
                    )
                    skipped_transcode += 1
                    continue

            filtered.append(item)

        if skipped_playback:
            logger.info(
                "Video playback disabled — filtered %d videos",
                skipped_playback,
            )
        if skipped_duration:
            logger.info(
                "Max video duration (%ds) — filtered %d videos",
                max_duration,
                skipped_duration,
            )
        if skipped_transcode:
            logger.info(
                "Videos not yet transcoded — filtered %d videos "
                "(transcoding is enabled; they will appear after processing)",
                skipped_transcode,
            )
        if skipped_ready:
            logger.info(
                "Videos not ready to play — filtered %d videos",
                skipped_ready,
            )

        self._queue = filtered

        if self._config.slideshow.get("shuffle", True):
            random.shuffle(self._queue)

        self._current_idx = 0 if self._queue else -1
        self._item_start_time = time.monotonic()

        for i in (0, 1):
            self._unload_texture(self._tex[i])
            self._tex[i] = None
            self._tex_item[i] = None
        self._active = 0
        with self._preload_lock:
            self._preload_array = None
            self._preload_cache_key = ""

        self._preload_into_inactive()
        self._write_current_media()
        self._queue_loaded = True
        logger.info("Media queue set: %d items", len(self._queue))

    def add_items(self, items: list[MediaItem]) -> int:
        """Add new items to the existing queue (deduplicating by id).

        Does NOT reset the current slideshow position — new items are
        appended to the end.  This is designed for hot-reload from the
        backend playlist without interrupting the currently displayed image.

        Also updates existing items' ``thumbnail_path`` if the backend
        provides one and the current item doesn't have one (e.g. items
        from the dev fallback scan lack thumbnails).

        Applies the same video guardrails as ``set_queue()``: respects
        ``video.playback_enabled``, ``video.transcoding_enabled``, and
        ``video.max_duration_seconds``.

        Returns the number of items actually added.
        """
        existing_ids = {item.id for item in self._queue}

        # Update existing items with richer backend data (e.g. thumbnail_path
        # and cached_path that the dev fallback scan couldn't provide).
        backend_by_id = {item.id: item for item in items}
        for existing in self._queue:
            backend_item = backend_by_id.get(existing.id)
            if backend_item is None:
                continue
            # Thumbnail from backend
            if existing.thumbnail_path is None and backend_item.thumbnail_path is not None:
                existing.thumbnail_path = backend_item.thumbnail_path
            # Optimised cache path from backend (avoids loading 4K originals)
            if str(existing.cached_path) == str(existing.original_path) and str(
                backend_item.cached_path
            ) != str(backend_item.original_path):
                existing.cached_path = backend_item.cached_path

        new_items = [item for item in items if item.id not in existing_ids]
        if not new_items:
            return 0

        # ── Video guardrails ─────────────────────────────────────────
        video_cfg = self._config.video if hasattr(self._config, "video") else {}
        playback_enabled = video_cfg.get(
            "playback_enabled",
            self._config.slideshow.get("video_playback_enabled", True),
        )
        transcoding_enabled = video_cfg.get("transcoding_enabled", True)
        max_duration = video_cfg.get(
            "max_duration_seconds",
            self._config.slideshow.get("video_max_duration_seconds", 0),
        )

        filtered: list[MediaItem] = []
        for item in new_items:
            if item.media_type != MediaType.VIDEO:
                filtered.append(item)
                continue
            if not playback_enabled:
                continue
            if max_duration > 0 and item.duration_seconds > max_duration:
                continue
            if transcoding_enabled:
                if not item.is_ready_to_play:
                    continue
                if item.transcode_status == TranscodeStatus.FAILED:
                    continue
            filtered.append(item)

        if not filtered:
            return 0

        # Cancel any in-progress preload BEFORE modifying the queue.
        # New items (especially videos via shuffle) may land at the next
        # position, making the in-flight preload stale.
        self._cancel_preload()

        self._queue.extend(filtered)
        if self._config.slideshow.get("shuffle", True):
            for item in filtered:
                if self._current_idx >= 0 and len(self._queue) > self._current_idx + 1:
                    pos = random.randint(self._current_idx + 1, len(self._queue) - 1)
                else:
                    pos = len(self._queue) - 1
                self._queue.pop()
                self._queue.insert(pos, item)

        added = len(filtered)
        skipped = len(new_items) - added
        if skipped:
            logger.info(
                "Added %d new items (filtered %d by video guardrails) — total: %d, current idx: %d",
                added,
                skipped,
                len(self._queue),
                self._current_idx,
            )
        else:
            logger.info(
                "Added %d new items to queue (total: %d, current idx: %d)",
                added,
                len(self._queue),
                self._current_idx,
            )

        # Restart preload for the correct next item after queue change.
        self._preload_into_inactive()
        return added

    def remove_items(self, item_ids: set[str]) -> int:
        """Remove items from the queue by media item ID.

        Does NOT reset the slideshow position — the currently displayed
        item is preserved.  If the current item is removed, the slideshow
        advances to the next item.  Items pending in the preload thread
        are cancelled if they match a removed ID.

        Returns the number of items actually removed.
        """
        if not item_ids:
            return 0

        before = len(self._queue)
        removed_current = (
            any(self._queue[self._current_idx].id in item_ids for _ in [0])
            if 0 <= self._current_idx < len(self._queue)
            else False
        )

        self._queue = [item for item in self._queue if item.id not in item_ids]
        removed = before - len(self._queue)

        if removed == 0:
            return 0

        # If the current item was removed, advance or reset
        if removed_current:
            if self._queue:
                # Stay at the same index (which now points to the next item
                # that slid into this position) or wrap to 0.
                if self._current_idx >= len(self._queue):
                    self._current_idx = 0
            else:
                self._current_idx = -1
            # Reset the texture slots so we don't keep displaying the
            # removed item.
            for i in (0, 1):
                self._unload_texture(self._tex[i])
                self._tex[i] = None
                self._tex_item[i] = None
            self._active = 0
            self._item_start_time = time.monotonic()

        # Cancel in-flight preload if it matches a removed item
        with self._preload_lock:
            pk = self._preload_cache_key
            if pk:
                for rid in item_ids:
                    if rid in pk:
                        self._preload_array = None
                        self._preload_cache_key = ""
                        break

        # Restart preload for the correct next item
        self._preload_into_inactive()

        logger.info(
            "Removed %d items from queue (total: %d, current idx: %d)",
            removed,
            len(self._queue),
            self._current_idx,
        )
        return removed

    def reset_slide_timer(self) -> None:
        """Reset the current slide's display timer to now.

        Called when the boot screen finishes fading out, so the first
        slide gets its full configured display duration rather than
        being partially elapsed from loading behind the boot layer.
        """
        if self._current_idx >= 0 and self._queue:
            self._item_start_time = time.monotonic()
            logger.debug("Slide timer reset for first visible slide")

    def _advance(self) -> None:
        """Move to the next item in the queue.

        Swaps active ↔ inactive slots: the preloaded texture becomes the
        displayed one, and the old displayed texture is freed.
        """
        if not self._queue:
            return
        logger.debug(
            "advance: %d → %d  active_slot=%d→%d",
            self._current_idx,
            (self._current_idx + 1) % len(self._queue),
            self._active,
            self._inactive,
        )
        self._unload_texture(self._tex[self._active])
        self._tex[self._active] = None
        self._tex_item[self._active] = None
        self._active = self._inactive
        self._current_idx = (self._current_idx + 1) % len(self._queue)
        self._item_start_time = time.monotonic()
        self._transition_stall_logged = False  # reset for new slide

        # If the new active slot (old inactive) has no texture, the
        # preload either failed or hasn't completed — screen will be
        # blank until the next texture loads.
        if self._tex[self._active] is None and self._current_idx >= 0:
            logger.warning(
                "Active slot %d has no texture after advance "
                "(item=%s, idx=%d) — preload may have failed or not completed. "
                "Screen will be blank until next texture loads.",
                self._active,
                getattr(self._queue[self._current_idx], "original_path", "?"),
                self._current_idx,
            )
        with self._preload_lock:
            self._preload_array = None
            self._preload_cache_key = ""
        self._preload_into_inactive()
        self._write_current_media()

    def next_item(self) -> None:
        """Skip to the next item in the queue.

        If a video is playing, the VLC process is killed and the
        preloaded next item is promoted immediately.  Implicitly
        resumes if the slideshow was paused.
        """
        if not self._queue:
            return

        # If a video is playing, stop it first.
        if self._video_state != _VIDEO_IDLE:
            logger.info("next_item: stopping video to advance")
            self._video_stop()

        self._paused = False
        self._advance()

        # If the new item is a video, launch it immediately instead of
        # waiting for the slide timer to expire.  Without this, the
        # video sits on its first frame for its full duration because
        # _advance() resets _item_start_time and the render loop won't
        # trigger video launch until elapsed >= duration.
        if self._current_idx >= 0 and self._current_idx < len(self._queue):
            new_item = self._queue[self._current_idx]
            if new_item.media_type == MediaType.VIDEO:
                self._video_launch(new_item)

    def prev_item(self) -> None:
        """Go back to the previous item in the queue.

        If a video is playing, the VLC process is killed.  The previous
        item is loaded directly into the active slot for an immediate
        cut (no transition animation — the user asked to jump).
        Implicitly resumes if the slideshow was paused.
        """
        if not self._queue:
            return

        # If a video is playing, stop it first.
        if self._video_state != _VIDEO_IDLE:
            logger.info("prev_item: stopping video to go back")
            self._video_stop()

        self._paused = False

        prev_idx = (self._current_idx - 1) % len(self._queue)
        prev = self._queue[prev_idx]

        # Unload both slots — we're doing a hard jump.
        for slot in (0, 1):
            self._unload_texture(self._tex[slot])
            self._tex[slot] = None
            self._tex_item[slot] = None

        self._current_idx = prev_idx
        self._item_start_time = time.monotonic()
        self._transition_stall_logged = False

        # Load the previous item directly into the active slot.
        if prev.media_type == MediaType.VIDEO:
            self._load_texture_for_slot(self._active, prev)
        else:
            self._tex[self._active] = self._load_texture_for_item(prev)
            self._tex_item[self._active] = prev

        # Preload the item that follows the new current position.
        with self._preload_lock:
            self._preload_array = None
            self._preload_cache_key = ""
        self._preload_into_inactive()
        self._write_current_media()

    def switch_album(self, album_id: str) -> None:
        logger.info("Album switch requested: %s (handled by backend)", album_id)

    def pause(self) -> None:
        """Pause the slideshow.

        If a video is playing, the VLC process is paused via SIGSTOP
        so playback freezes in place.
        """
        self._paused = True
        if self._video_state == _VIDEO_PLAYING and self._video_proc is not None:
            try:
                os.kill(self._video_proc.pid, signal.SIGSTOP)
                self._video_paused = True
                logger.info("VLC paused via SIGSTOP (pid=%d)", self._video_proc.pid)
            except OSError:
                logger.warning("Failed to SIGSTOP VLC", exc_info=True)
        self._write_current_media()

    def resume(self) -> None:
        """Resume the slideshow.

        If a video was paused, the VLC process is resumed via SIGCONT
        and the slide timer is reset.
        """
        self._paused = False
        if self._video_paused and self._video_proc is not None:
            try:
                os.kill(self._video_proc.pid, signal.SIGCONT)
                self._video_paused = False
                logger.info("VLC resumed via SIGCONT (pid=%d)", self._video_proc.pid)
            except OSError:
                logger.warning("Failed to SIGCONT VLC", exc_info=True)
                self._video_paused = False
        self._item_start_time = time.monotonic()
        self._write_current_media()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> None:
        if not self._queue or self._current_idx < 0:
            return

        self._upload_pending_preload()

        # --- Non-blocking video tick -----------------------------------
        # Drive the video state machine each frame so the render loop
        # stays responsive to IPC control commands even during playback.
        if self._video_state != _VIDEO_IDLE:
            self._video_tick()
            # Still render the underlying frame (first/last frame of
            # video) at full opacity — VLC's window covers it.
            current_item = self._queue[self._current_idx]
            self._render_item(current_item, 1.0)
            return

        current_item = self._queue[self._current_idx]
        elapsed = time.monotonic() - self._item_start_time
        duration = self._get_item_duration(current_item)
        transition_style = self._config.slideshow.get("transition_style", "crossfade")
        transition_ms = self._config.slideshow.get("transition_duration_ms", 1500)
        # When transition style is "none", the slide cuts immediately
        # with no animation — treat the transition duration as zero.
        transition_s = 0.0 if transition_style == "none" else transition_ms / 1000.0

        if self._paused:
            self._render_item(current_item, 1.0)
            return

        # If the inactive slot finally got its texture after a stall,
        # give the crossfade transition time to run from this point.
        next_tex = self._tex[self._inactive]
        if elapsed >= duration and next_tex is not None and self._transition_stall_logged:
            # Reset the clock so the crossfade gets its full duration
            # instead of jump-cutting.
            self._item_start_time = time.monotonic() - duration
            elapsed = duration  # transition starts now
            self._transition_stall_logged = False
            logger.debug(
                "Transition unstalled — crossfading to %s",
                getattr(
                    self._queue[(self._current_idx + 1) % len(self._queue)]
                    if self._queue
                    else None,
                    "original_path",
                    "?",
                ),
            )

        if elapsed >= (duration + transition_s):
            # If the inactive slot has no texture and the preload thread
            # is still running (e.g. video first-frame extraction), don't
            # jump-cut — wait for the preload to finish.  Cap at 30s to
            # prevent infinite stall on genuinely broken files.
            if (
                next_tex is None
                and self._preload_thread is not None
                and self._preload_thread.is_alive()
            ):
                stall_elapsed = elapsed - (duration + transition_s)
                if stall_elapsed < 30.0:
                    if not self._transition_stall_logged:
                        self._transition_stall_logged = True
                        logger.warning(
                            "Transition stalled: waiting for preload "
                            "(%.1fs past deadline) — holding current slide.",
                            stall_elapsed,
                        )
                    self._render_item(current_item, 1.0)
                    return
                else:
                    logger.warning(
                        "Preload timed out after %.1fs — advancing anyway",
                        stall_elapsed,
                    )
            self._advance()
            if self._current_idx >= 0:
                new_item = self._queue[self._current_idx]
                if new_item.media_type == MediaType.VIDEO:
                    # Transition just completed — the video's first
                    # frame is now in the active slot.  Launch VLC
                    # via the non-blocking state machine.
                    self._video_launch(new_item)
                else:
                    self._render_item(new_item, 1.0)
            return

        # During transition, crossfade between the two slots.
        next_tex = self._tex[self._inactive]
        if elapsed >= duration and next_tex is not None:
            progress = (elapsed - duration) / transition_s
            self._render_transition(current_item, progress, next_tex)
        elif elapsed >= duration and next_tex is None and self._current_idx >= 0:
            # Preload hasn't finished — log once per slide, not every frame.
            if not self._transition_stall_logged:
                self._transition_stall_logged = True
                next_idx = (self._current_idx + 1) % len(self._queue)
                next_item = self._queue[next_idx]
                logger.warning(
                    "Transition stalled: inactive slot %d has no texture "
                    "(next item=%s, elapsed=%.1fs, slide_duration=%.1fs). "
                    "Preload likely still in progress — holding current slide.",
                    self._inactive,
                    getattr(next_item, "original_path", next_item),
                    elapsed,
                    duration,
                )
            self._render_item(current_item, 1.0)
        else:
            self._render_item(current_item, 1.0)

    # ------------------------------------------------------------------
    # Item rendering
    # ------------------------------------------------------------------

    def _render_item(
        self,
        item: MediaItem,
        alpha: float,
        with_matte: bool = True,
        texture: Any = None,
        layout: dict | None = None,
    ) -> None:
        """Draw a single media item with layout and matte bars."""
        if texture is None:
            tex = self._tex[self._active]
            if tex is None and self._current_idx >= 0:
                gpu_info = self._backend.gpu_memory_info()
                logger.warning(
                    "Active slot %d has no texture — attempting sync load for %s",
                    self._active,
                    getattr(item, "original_path", item),
                )
                if gpu_info:
                    logger.debug(
                        "GPU mem at sync load: total=%sM reloc=%sM V3D=%skb/%sBOs textures=%s/%s",
                        gpu_info.get("gpu_total_mb", "?"),
                        gpu_info.get("reloc_used_mb", "?"),
                        gpu_info.get("v3d_bo_kb", "?"),
                        gpu_info.get("v3d_bo_count", "?"),
                        gpu_info.get("texture_count", "?"),
                        gpu_info.get("max_textures", "?"),
                    )
                self._load_texture_for_slot(self._active, item)
                tex = self._tex[self._active]
            if tex is None:
                gpu_info = self._backend.gpu_memory_info()
                logger.warning(
                    "No texture for active slot %d (item=%s, idx=%d) — "
                    "rendering blank frame (black screen)",
                    self._active,
                    getattr(item, "original_path", item),
                    self._current_idx,
                )
                if gpu_info:
                    logger.warning(
                        "GPU mem at black screen: total=%sM reloc=%sM "
                        "V3D=%skb/%sBOs textures=%s/%s",
                        gpu_info.get("gpu_total_mb", "?"),
                        gpu_info.get("reloc_used_mb", "?"),
                        gpu_info.get("v3d_bo_kb", "?"),
                        gpu_info.get("v3d_bo_count", "?"),
                        gpu_info.get("texture_count", "?"),
                        gpu_info.get("max_textures", "?"),
                    )
                return
            texture = tex

        if layout is None:
            # Use the texture's source item for layout when it differs
            # from the current queue item.  This happens after video
            # playback: the active slot holds the last frame, but the
            # queue has already advanced to the next image.
            layout_source = self._tex_item[self._active] or item
            resolved = self._resolve_fit_mode(layout_source)
            cache_key = (id(layout_source), resolved)
            if cache_key in self._layout_cache:
                layout = self._layout_cache[cache_key]
            else:
                layout = self._layout.compute(layout_source, fit_mode=resolved)
                if len(self._layout_cache) < 16:
                    self._layout_cache[cache_key] = layout

        if with_matte:
            matte_color = self._config.slideshow.get("matte_color", [0, 0, 0])
            for mx, my, mw, mh in layout.get("matte_rects", []):
                self._backend.draw_rect(
                    mx,
                    my,
                    mw,
                    mh,
                    (*matte_color, alpha),
                    z=-1,
                )

        ix, iy, iw, ih = layout["image_rect"]
        self._backend.draw_image(
            texture,
            ix,
            iy,
            iw,
            ih,
            alpha=alpha,
            uv_offset=(0.0, 0.0),
            uv_scale=(1.0, 1.0),
            z=0.0,
        )

    def _render_transition(
        self,
        current_item: MediaItem,
        progress: float,
        next_tex: Any,
    ) -> None:
        """Crossfade between active and inactive texture slots."""
        next_item = self._queue[(self._current_idx + 1) % len(self._queue)]
        style = self._config.slideshow.get("transition_style", "crossfade")

        # Use the texture's source item for layout when it differs from
        # the queue item (e.g. last frame of a video transitioning to
        # the next photo).
        cur_src = self._tex_item[self._active] or current_item
        next_src = self._tex_item[self._inactive] or next_item

        if style == "crossfade":
            current_layout = self._layout.compute(
                cur_src,
                fit_mode=self._resolve_fit_mode(cur_src),
            )
            next_layout = self._layout.compute(
                next_src,
                fit_mode=self._resolve_fit_mode(next_src),
            )
            matte_color = self._config.slideshow.get("matte_color", [0, 0, 0])

            # Full-screen black background ensures any partially-
            # transparent pixels from the crossfade shader blend
            # against solid black rather than showing framebuffer
            # artefacts or PNG transparency edges.
            self._backend.draw_rect(
                0,
                0,
                self._backend.width,
                self._backend.height,
                (*matte_color, 1.0),
                z=-2,
            )
            for mx, my, mw, mh in current_layout.get("matte_rects", []):
                self._backend.draw_rect(
                    mx,
                    my,
                    mw,
                    mh,
                    (*matte_color, 1.0),
                    z=-1,
                )
            self._backend.draw_crossfade(
                tex_current=self._tex[self._active],
                tex_next=next_tex,
                blend=progress,
                current_rect=current_layout["image_rect"],
                next_rect=next_layout["image_rect"],
            )
        elif style == "fade_through_black":
            # Compute layouts for both items explicitly.  _render_item
            # defaults to the *active* slot's source for layout, which
            # is still the current item during transition.  Without
            # explicit layouts, the second half would draw the next
            # texture with the current item's aspect ratio.
            cur_layout = self._layout.compute(
                cur_src,
                fit_mode=self._resolve_fit_mode(cur_src),
            )
            next_layout = self._layout.compute(
                next_src,
                fit_mode=self._resolve_fit_mode(next_src),
            )
            if progress < 0.5:
                self._render_item(
                    current_item,
                    1.0 - progress * 2,
                    texture=self._tex[self._active],
                    layout=cur_layout,
                )
            else:
                self._render_item(
                    next_item,
                    (progress - 0.5) * 2,
                    texture=next_tex,
                    layout=next_layout,
                )
        elif style == "none":
            # No transition — just show the next item immediately.
            # Layouts are still computed explicitly so the next
            # texture isn't drawn with the current item's aspect ratio.
            next_layout = self._layout.compute(
                next_src,
                fit_mode=self._resolve_fit_mode(next_src),
            )
            self._render_item(next_item, 1.0, texture=next_tex, layout=next_layout)
        else:
            # Hard cut: show current until midpoint, then next.
            # Explicit layouts prevent the next texture from being drawn
            # with the current item's aspect ratio during the second half.
            cur_layout = self._layout.compute(
                cur_src,
                fit_mode=self._resolve_fit_mode(cur_src),
            )
            next_layout = self._layout.compute(
                next_src,
                fit_mode=self._resolve_fit_mode(next_src),
            )
            if progress < 0.5:
                self._render_item(
                    current_item,
                    1.0,
                    texture=self._tex[self._active],
                    layout=cur_layout,
                )
            else:
                self._render_item(next_item, 1.0, texture=next_tex, layout=next_layout)

    # ------------------------------------------------------------------
    # Texture loading
    # ------------------------------------------------------------------

    def _load_texture_for_slot(self, slot: int, item: MediaItem) -> None:
        """Ensure ``_tex[slot]`` has a valid texture for *item*.

        For images: loads + downscales the JPEG.
        For videos: launches VLC via the non-blocking state machine.

        Loads the new texture BEFORE unloading the old one — if the load
        fails, the slot retains its previous texture rather than going
        black.  This is critical on memory-constrained hardware where
        GPU allocations can fail under fragmentation pressure.
        """
        if item.media_type == MediaType.VIDEO:
            self._video_launch(item)
            return
        new_tex = self._load_texture_for_item(item)
        if new_tex is not None:
            self._unload_texture(self._tex[slot])
            self._tex[slot] = new_tex
            self._tex_item[slot] = item

    def _load_texture_for_item(self, item: MediaItem) -> Any:
        """Load an image as a GPU texture (videos are handled elsewhere).

        If the file is an uncached original that is excessively large
        (> 3× screen pixels), the load is skipped to avoid OOM on
        memory-constrained hardware.  The backend will eventually
        provide a cached (resized) version via playlist hot-reload.
        """
        path_to_load = item.cached_path

        max_w = int(self._backend.width * 1.2)
        max_h = int(self._backend.height * 1.2)

        # ── Guard: skip huge uncached originals ──────────────────────
        if str(item.cached_path) == str(item.original_path):
            try:
                file_size_mb = path_to_load.stat().st_size / (1024 * 1024)
            except OSError:
                file_size_mb = 0.0
            # Files > 8 MB are likely high-res originals.  Loading them
            # into PIL can consume 100+ MB of RAM per image.  Defer to
            # the backend's cached (resized) version.
            if file_size_mb > 8.0:
                logger.debug(
                    "Skipping large uncached original (%.1f MB): %s — "
                    "waiting for backend cached version",
                    file_size_mb,
                    path_to_load,
                )
                return None

        try:
            from PIL import ImageFile

            ImageFile.LOAD_TRUNCATED_IMAGES = True

            img = Image.open(path_to_load)
            img = ImageOps.exif_transpose(img)

            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (0, 0, 0))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode not in ("RGB", "L") or img.mode == "L":
                img = img.convert("RGB")

            if img.width > max_w or img.height > max_h:
                orig_w, orig_h = img.width, img.height
                img.thumbnail((max_w, max_h), Image.LANCZOS)
                logger.debug(
                    "Downscaled [%s]: %dx%d → %dx%d",
                    path_to_load,
                    orig_w,
                    orig_h,
                    img.width,
                    img.height,
                )

            arr = np.asarray(img, dtype=np.uint8)
            img.close()

            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)  # noqa: SIM115
            try:
                Image.fromarray(arr).save(tmp.name, "JPEG", quality=92)
                texture = self._backend.load_texture(Path(tmp.name))
            finally:
                tmp.close()
                with contextlib.suppress(OSError):
                    os.unlink(tmp.name)
            logger.debug("Texture loaded [%s]: tex=%s", path_to_load, id(texture))
            return texture
        except Exception:
            logger.exception("Failed to load texture: %s", path_to_load)
            return None

    # ------------------------------------------------------------------
    # Preload system (CPU worker → GPU upload on main thread)
    # ------------------------------------------------------------------

    def _cancel_preload(self) -> None:
        """Cancel any in-progress preload and discard its result."""
        if self._preload_thread is not None and self._preload_thread.is_alive():
            logger.debug("Cancelling stale preload")
        with self._preload_lock:
            self._preload_array = None
            self._preload_cache_key = ""

    def _preload_into_inactive(self) -> None:
        """Start preloading the next queue item into the inactive slot."""
        if not self._queue or self._current_idx < 0:
            return
        next_idx = (self._current_idx + 1) % len(self._queue)
        next_item = self._queue[next_idx]

        if self._preload_thread is not None and self._preload_thread.is_alive():
            return

        self._preload_thread = threading.Thread(
            target=self._preload_worker,
            args=(next_item,),
            daemon=True,
            name="tex-preload",
        )
        self._preload_thread.start()

    def _preload_worker(self, item: MediaItem) -> None:
        """CPU work: load + downscale → numpy array.  Main thread uploads.

        For images: loads the JPEG, downscales, stores as numpy.
        For videos: extracts/caches the first frame, loads it the same way.
        """
        try:
            from PIL import ImageFile

            ImageFile.LOAD_TRUNCATED_IMAGES = True

            if item.media_type == MediaType.VIDEO:
                path_to_load = self._preload_video_first_frame(item)
                if path_to_load is None:
                    with self._preload_lock:
                        self._preload_array = None
                        self._tex[self._inactive] = None
                    return
            else:
                path_to_load = item.cached_path

            # ── Guard: skip huge uncached originals ──────────────────
            if str(item.cached_path) == str(item.original_path):
                try:
                    file_size_mb = path_to_load.stat().st_size / (1024 * 1024)
                except OSError:
                    file_size_mb = 0.0
                if file_size_mb > 8.0:
                    logger.debug(
                        "Preload skipping large uncached original "
                        "(%.1f MB): %s — waiting for backend cache",
                        file_size_mb,
                        path_to_load,
                    )
                    with self._preload_lock:
                        self._preload_array = None
                    return

            img = Image.open(path_to_load)
            img = ImageOps.exif_transpose(img)

            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (0, 0, 0))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode not in ("RGB", "L") or img.mode == "L":
                img = img.convert("RGB")

            max_w = int(self._backend.width * 1.2)
            max_h = int(self._backend.height * 1.2)
            if img.width > max_w or img.height > max_h:
                orig_w, orig_h = img.width, img.height
                img.thumbnail((max_w, max_h), Image.LANCZOS)
                logger.debug(
                    "Preload downscaled [%s]: %dx%d → %dx%d",
                    path_to_load,
                    orig_w,
                    orig_h,
                    img.width,
                    img.height,
                )

            arr = np.asarray(img, dtype=np.uint8)
            img.close()

            with self._preload_lock:
                self._preload_array = arr
                self._preload_cache_key = str(path_to_load)
            logger.debug("Preload ready [%s]", path_to_load)
        except Exception:
            logger.exception("Preload failed: %s", getattr(item, "cached_path", item.original_path))
            with self._preload_lock:
                self._preload_array = None

    def _preload_video_first_frame(self, item: MediaItem) -> Path | None:
        """Return the path to the pre-generated first frame JPEG.

        Frame extraction is a backend (Phase 2 OPTIMISE) responsibility.
        The frontend simply loads the cache file referenced by the
        ``MediaItem`` — no ffmpeg/ffprobe here.
        """
        video_path = str(item.cached_path or item.original_path)

        # Guard: if cached path != original and the file doesn't exist
        # (e.g. transcoding not yet complete), don't attempt to play it.
        from pathlib import Path as _Path

        _cached = _Path(video_path)
        if str(item.cached_path) != str(item.original_path) and (
            not _cached.is_file() or _cached.stat().st_size < 1024
        ):
            logger.warning(
                "Video cached file not ready — skipping: %s",
                video_path,
            )
            return None

        if item.first_frame_path is not None and item.first_frame_path.exists():
            return item.first_frame_path

        logger.warning(
            "No first frame cached for %s — backend should have pre-generated this during OPTIMISE",
            video_path,
        )
        return None

    def _upload_pending_preload(self) -> None:
        """Upload a finished preload numpy array to the inactive GPU slot."""
        arr: np.ndarray | None = None
        cache_key: str = ""
        with self._preload_lock:
            if self._preload_array is not None:
                arr = self._preload_array
                cache_key = self._preload_cache_key
                self._preload_array = None
                self._preload_cache_key = ""

        if arr is None:
            return

        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)  # noqa: SIM115
            try:
                Image.fromarray(arr).save(tmp.name, "JPEG", quality=92)
                texture = self._backend.load_texture(Path(tmp.name))
            finally:
                tmp.close()
                with contextlib.suppress(OSError):
                    os.unlink(tmp.name)

            self._unload_texture(self._tex[self._inactive])
            self._tex[self._inactive] = texture
            # Track which item this preload belongs to.
            next_idx = (self._current_idx + 1) % len(self._queue)
            self._tex_item[self._inactive] = self._queue[next_idx]
            logger.debug("Preload GPU upload OK: %s tex=%s", cache_key, id(texture))
        except Exception:
            logger.exception("Preload GPU upload failed: %s", cache_key)
            self._tex[self._inactive] = None
            self._tex_item[self._inactive] = None

    # ------------------------------------------------------------------
    # Video playback (non-blocking state machine)
    # ------------------------------------------------------------------

    def _video_launch(self, item: MediaItem) -> None:
        """Launch the video player for a video item without blocking the render loop.

        The first frame should already be in the active texture slot
        (loaded via the normal preload → advance flow).  The player runs
        on top.  The state machine in :meth:`_video_tick` handles the
        last-frame under-swap and post-playback transition.

        Player backend selection (``video.player_backend`` config):
        - ``"auto"`` (default): tries VLC subprocess first, falls back to ffmpeg.
        - ``"vlc"``: uses VLC as a subprocess (borderless window overlay).
          Recommended for Pi — zero Python heap memory during playback.
        - ``"ffmpeg"``: uses ffmpeg to decode frames into GPU textures.
          Currently experimental — falls back to VLC on Pi targets.
        """
        # ── Resolve player backend ────────────────────────────────────
        video_cfg = self._config.video if hasattr(self._config, "video") else {}
        player_backend = video_cfg.get("player_backend", "auto")
        # Fallback to legacy slideshow key if video section not present
        if not video_cfg:
            player_backend = self._config.slideshow.get("video_player_backend", "auto")

        if not self._config.slideshow.get("video_playback_enabled", True):
            # Also check new video section
            if video_cfg:
                if not video_cfg.get("playback_enabled", True):
                    logger.debug("Video playback disabled — skipping %s", item.original_path)
                    return
            else:
                logger.debug("Video playback disabled — skipping %s", item.original_path)
                return

        video_path = str(item.cached_path or item.original_path)

        # --- Use metadata from backend (no ffprobe here) ---------------
        vw = item.width
        vh = item.height
        duration = item.duration_seconds

        if vw <= 0 or vh <= 0:
            logger.warning(
                "Video has invalid dimensions (%dx%d): %s — skipping",
                vw,
                vh,
                video_path,
            )
            self._advance()
            return

        screen_w = int(self._backend.width)
        screen_h = int(self._backend.height)

        # --- Ensure first frame in active slot --------------------------
        if self._tex[self._active] is None:
            if item.first_frame_path is not None and item.first_frame_path.exists():
                self._tex[self._active] = self._backend.load_texture(
                    item.first_frame_path,
                )
                self._tex_item[self._active] = item
            else:
                logger.warning(
                    "No first frame cached for %s — "
                    "backend should have pre-generated this. Skipping video.",
                    video_path,
                )
                self._advance()
                return

        # --- Compute layout (needed for both the pre-VLC draw and VLC) ---
        resolved_fit = self._resolve_fit_mode(item)

        # ── Select and launch player backend ───────────────────────────
        if player_backend == "ffmpeg":
            # ffmpeg GPU-texture pipeline (experimental — Phase 2)
            logger.warning(
                "ffmpeg player backend selected but not yet integrated — "
                "falling back to VLC for %s",
                video_path,
            )
            player_backend = "vlc"  # Fall through to VLC path below

        if player_backend in ("auto", "vlc"):
            self._video_launch_vlc(
                item,
                video_path,
                vw,
                vh,
                duration,
                screen_w,
                screen_h,
                resolved_fit,
            )

    def _video_launch_vlc(
        self,
        item: MediaItem,
        video_path: str,
        vw: int,
        vh: int,
        duration: float,
        screen_w: int,
        screen_h: int,
        resolved_fit: str,
    ) -> None:
        """Launch VLC as a subprocess for video playback.

        VLC creates its own borderless window on top of the pi3d display.
        The state machine in :meth:`_video_tick` polls the subprocess and
        handles the last-frame under-swap (scheduled at 50% of duration).

        First and last frame caches are generated by the backend during
        Phase 2 (OPTIMISE) — the frontend never runs ffmpeg/ffprobe.
        """

        # --- Load last frame into GPU texture BEFORE launching VLC ----
        # Order: ① load from disk → ② force GL upload → ③ wait for DMA
        # → ④ verify GL texture is valid.  Only THEN continue.
        # Done BEFORE drawing the first frame to buffers — load_opengl()
        # binds the texture, which can momentarily affect pi3d's render
        # state.  Drawing the first frame afterwards overwrites it.
        self._video_last_frame_tex = None
        if item.last_frame_path is not None and item.last_frame_path.exists():
            try:
                tex = self._backend.load_texture(item.last_frame_path)
                if hasattr(tex, "load_opengl"):
                    tex.load_opengl()
                self._backend.flush_gpu()
                gl_id = getattr(tex, "_tex", None)
                if gl_id is None or (hasattr(gl_id, "value") and gl_id.value == 0):
                    logger.error(
                        "Last frame GL texture is invalid (gl_id=%s) — skipping video: %s",
                        gl_id,
                        video_path,
                    )
                    self._unload_texture(tex)
                    self._advance()
                    return
                self._video_last_frame_tex = tex
                logger.debug(
                    "Last frame ready for %s: %s (gl_id=%s)",
                    video_path,
                    item.last_frame_path,
                    gl_id,
                )
            except Exception:
                logger.exception(
                    "Failed to load last frame for %s: %s — "
                    "skipping video to avoid black screen on VLC exit",
                    video_path,
                    item.last_frame_path,
                )
                self._advance()
                return
        else:
            logger.warning(
                "Last frame not cached for %s — "
                "backend should have pre-generated this during OPTIMISE. "
                "First frame will persist after video ends.",
                video_path,
            )

        # --- Draw first frame to both buffers AFTER last-frame load ---
        # Must be done AFTER loading the last frame — load_opengl()
        # binds the texture and can momentarily affect pi3d's render
        # state.  Drawing the first frame to both buffers now overwrites
        # any side effects and ensures the correct frame is visible
        # when VLC's window appears.
        if self._tex[self._active] is not None:
            layout = self._layout.compute(item, fit_mode=resolved_fit)
            self._draw_frame_to_buffer(self._tex[self._active], layout)
            self._backend.loop_running()
            self._draw_frame_to_buffer(self._tex[self._active], layout)
        else:
            logger.warning(
                "No first-frame texture for %s — VLC will appear over black",
                video_path,
            )

        # --- Launch VLC -------------------------------------------------
        from metixel.frontend.presentation.video_player import VlcVideoPlayer

        vlc_player = VlcVideoPlayer()
        logger.debug(
            "Starting VLC: %s (duration=%.1fs, fit_mode=%s)",
            video_path,
            item.duration_seconds,
            resolved_fit,
        )
        vlc_proc = vlc_player.play(
            video_path,
            screen_w=screen_w,
            screen_h=screen_h,
            block=False,
            loop=False,
            fit_mode=resolved_fit,
        )
        if vlc_proc is None:
            logger.warning("VLC failed to start: %s", video_path)
            if self._video_last_frame_tex is not None:
                self._unload_texture(self._video_last_frame_tex)
                self._video_last_frame_tex = None
            self._advance()
            return

        # --- Enter state machine ----------------------------------------
        self._video_state = _VIDEO_WAITING
        self._video_proc = vlc_proc
        self._video_player = vlc_player
        self._video_launch_at = time.monotonic()
        self._video_item = item
        self._video_path = video_path
        self._video_vw = vw
        self._video_vh = vh
        self._video_duration = duration
        self._video_paused = False
        self._video_last_frame_loaded = False
        # Swap timer starts when VLC confirms it's rendering (see
        # _video_tick WAITING→PLAYING transition), not at launch.
        self._video_swap_at = 0.0
        logger.debug(
            "Video state machine: WAITING (duration=%.1f, last_frame_preloaded=%s)",
            duration,
            self._video_last_frame_tex is not None,
        )

    def _video_tick(self) -> None:
        """Drive the video playback state machine — called once per frame.

        This is the heart of the non-blocking video playback.  It
        replaces the old ``time.sleep()`` + ``vlc_proc.wait()`` pattern
        so the render loop stays responsive.
        """
        now = time.monotonic()

        # --- Check for VLC crash / early exit ---------------------------
        if self._video_proc is not None and not self._video_paused:
            rc = self._video_proc.poll()
            if rc is not None:
                # VLC exited (normally or crashed)
                logger.debug("VLC exited with code %s: %s", rc, self._video_path)
                self._video_finish()
                return

        if self._video_state == _VIDEO_WAITING:
            # --- Phase 0: Waiting for VLC to confirm playback started ---
            player = getattr(self, "_video_player", None)
            started = player is not None and player.is_playing
            waited = now - self._video_launch_at
            if started:
                # Swap the last frame at 50% of video playtime — early
                # enough that VLC is still running on slow hardware,
                # late enough that the viewer has seen most of the video.
                duration = self._video_duration
                swap_delay = duration * 0.50 if duration > 0 else 0.6
                self._video_swap_at = now + swap_delay
                self._video_state = _VIDEO_PLAYING
                logger.debug(
                    "Video state machine: PLAYING (swap_at=%.1f, duration=%.1f, vlc_startup=%.1fs)",
                    self._video_swap_at,
                    duration,
                    waited,
                )
            elif waited >= self._config.timeout("vlc_start", int(_VLC_START_TIMEOUT_DEFAULT)):
                # VLC never confirmed playback — skip the video.
                logger.warning(
                    "VLC start timeout (%.1fs/%ds) — skipping video: %s",
                    waited,
                    self._config.timeout("vlc_start", int(_VLC_START_TIMEOUT_DEFAULT)),
                    self._video_path,
                )
                self._video_stop()
                self._advance()

        elif self._video_state == _VIDEO_PLAYING:
            # --- Phase 1: VLC running, wait for swap time ---------------
            if now >= self._video_swap_at:
                self._video_do_last_frame_swap()

        elif self._video_state == _VIDEO_SWAPPED:
            # --- Phase 2: Last frame swapped, VLC still running ---------
            # Nothing to do — just wait for VLC to exit (polled above).
            pass

    # ------------------------------------------------------------------
    # Shared helper: extract + upload the video's last frame.
    # Used both during normal playback (under VLC) and as an emergency
    # fallback when VLC exits before the scheduled swap time.
    # ------------------------------------------------------------------

    def _load_last_frame_into_active(
        self,
        item: MediaItem,
        video_path: str,
        video_vw: int,
        video_vh: int,
        video_duration: float,
    ) -> bool:
        """Swap the preloaded last-frame texture into the active slot.

        The texture was fully loaded (disk → GL → DMA → verified) in
        :meth:`_video_launch_vlc` BEFORE VLC started.  This method
        only swaps it in — no I/O, no GL allocation, no failure modes.

        Returns ``True`` on success, ``False`` if the preloaded texture
        is missing (shouldn't happen — VLC wouldn't have started).
        """
        new_tex = self._video_last_frame_tex
        if new_tex is None:
            logger.warning(
                "No preloaded last frame for %s — "
                "backend should have pre-generated this during OPTIMISE. "
                "First frame will persist after video ends.",
                video_path,
            )
            return False

        # Swap: unload the old first-frame texture, install the last frame.
        self._unload_texture(self._tex[self._active])
        self._tex[self._active] = new_tex
        self._tex_item[self._active] = item
        self._video_last_frame_tex = None  # ownership transferred

        # Log the texture internals for black-screen diagnostics.
        tex_id = getattr(new_tex, "_tex", None) if new_tex is not None else None
        tex_size = getattr(new_tex, "size", None) if new_tex is not None else None
        logger.debug(
            "Last-frame texture: pi3d_id=%s gl_id=%s size=%s → slot %d",
            id(new_tex),
            tex_id,
            tex_size,
            self._active,
        )

        # Draw to front buffer, swap, then draw to back buffer
        # so both hold the last frame (avoids first-frame flash
        # on the next render-cycle loop_running swap).
        resolved = self._resolve_fit_mode(item)
        layout = self._layout.compute(item, fit_mode=resolved)
        self._draw_frame_to_buffer(self._tex[self._active], layout)
        self._backend.loop_running()
        self._draw_frame_to_buffer(self._tex[self._active], layout)
        logger.debug(
            "Last frame loaded into active slot %d: %s",
            self._active,
            video_path,
        )
        return True

    def _video_do_last_frame_swap(self) -> None:
        """Load the video's last frame into the active slot under VLC.

        The VLC window covers the display, so the swap is invisible.
        The last frame goes into the *active* slot — the inactive slot
        holds the preloaded next item and must NOT be overwritten.
        """
        if self._video_last_frame_loaded:
            return
        self._video_last_frame_loaded = True

        item = self._video_item
        if item is None:
            return

        self._load_last_frame_into_active(
            item,
            self._video_path,
            self._video_vw,
            self._video_vh,
            self._video_duration,
        )

        self._video_state = _VIDEO_SWAPPED
        logger.debug("Video state machine: SWAPPED (waiting for VLC exit)")

    def _video_finish(self) -> None:
        """Clean up after VLC exits and set up the post-video transition.

        The last frame is in the active slot; the next item is preloaded
        in the inactive slot.  We set ``_item_start_time`` so the render
        loop enters the transition phase on the next ``render()`` call.

        Two edge cases are handled here that the non-blocking state
        machine cannot guarantee on its own:

        1. **VLC exited before swap time** — If ``_video_swap_at`` has
           not been reached yet (e.g. ffprobe over-reported the
           duration), the last frame was never loaded.  We do it now
           synchronously.

        2. **No transition animation** — When ``transition_style`` is
           ``"none"``, ``elapsed >= duration + 0`` triggers an immediate
           ``_advance()``, skipping the last frame entirely.  We reserve
           a 0.5 s linger so the viewer sees the final frame.
        """
        item = self._video_item
        video_path = self._video_path

        # --- Emergency last-frame load (VLC exited before swap) --------
        if not self._video_last_frame_loaded and video_path and item is not None:
            logger.warning(
                "VLC exited before last-frame swap — loading now: %s",
                video_path,
            )
            try:
                self._load_last_frame_into_active(
                    item,
                    video_path,
                    self._video_vw,
                    self._video_vh,
                    self._video_duration,
                )
            except Exception:
                logger.exception(
                    "Emergency last-frame swap failed for %s",
                    video_path,
                )

        # --- Clear video state -----------------------------------------
        # If the last-frame texture was preloaded but never consumed
        # (e.g. VLC crashed before the swap), unload it now.
        if self._video_last_frame_tex is not None:
            self._unload_texture(self._video_last_frame_tex)
            self._video_last_frame_tex = None

        self._video_state = _VIDEO_IDLE
        self._video_proc = None
        self._video_item = None
        self._video_path = ""
        self._video_paused = False
        self._video_last_frame_loaded = False

        if item is not None:
            item_duration = self._get_item_duration(item)

            # --- Handle transition_style = "none" ------------------
            # Without a transition animation the _item_start_time
            # trick would cause _advance() to fire immediately,
            # never showing the last frame.  Reserve a brief linger.
            transition_style = self._config.slideshow.get(
                "transition_style",
                "crossfade",
            )
            if transition_style == "none":
                linger = 0.5
                self._item_start_time = time.monotonic() - item_duration + linger
                logger.debug(
                    "Post-VLC (no transition): lingering %.1fs "
                    "on last frame (active=%d, inactive=%d)",
                    linger,
                    self._active,
                    self._inactive,
                )
            else:
                self._item_start_time = time.monotonic() - item_duration
                logger.debug(
                    "Post-VLC: item_start_time set for transition (active=%d, inactive=%d)",
                    self._active,
                    self._inactive,
                )

    def _video_stop(self) -> None:
        """Force-stop video playback (used by next/prev controls).

        Kills the VLC subprocess, cleans up state, and ensures the
        slideshow can continue from the current position.
        """
        if self._video_proc is not None:
            pid = self._video_proc.pid
            try:
                # Resume if paused, then terminate
                if self._video_paused:
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGCONT)
                self._video_proc.terminate()
                try:
                    self._video_proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._video_proc.kill()
                    self._video_proc.wait(timeout=1.0)
            except OSError:
                pass
            logger.info("VLC stopped (pid=%d)", pid)

        self._video_state = _VIDEO_IDLE
        self._video_proc = None
        self._video_item = None
        self._video_path = ""
        self._video_paused = False
        self._video_last_frame_loaded = False
        if self._video_last_frame_tex is not None:
            self._unload_texture(self._video_last_frame_tex)
            self._video_last_frame_tex = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_item_duration(self, item: MediaItem) -> float:
        if item.media_type == MediaType.VIDEO:
            if item.duration_seconds > 0:
                duration = item.duration_seconds
            else:
                duration = self._config.slideshow.get("image_duration_seconds", 30)
            # Read max duration from new video section, fall back to legacy slideshow key
            video_cfg = self._config.video if hasattr(self._config, "video") else {}
            max_video = video_cfg.get(
                "max_duration_seconds",
                self._config.slideshow.get("video_max_duration_seconds", 0),
            )
            if max_video > 0 and duration > max_video:
                duration = float(max_video)
            return duration
        return self._config.slideshow.get("image_duration_seconds", 30)

    def _unload_texture(self, texture: Any) -> None:
        if texture is not None:
            self._backend.unload_texture(texture)

    def _draw_frame_to_buffer(self, texture: Any, layout: dict) -> None:
        """Draw a texture with matte bars at the given layout position."""
        matte_color = self._config.slideshow.get("matte_color", [0, 0, 0])
        for mx, my, mw, mh in layout.get("matte_rects", []):
            self._backend.draw_rect(
                mx,
                my,
                mw,
                mh,
                (*matte_color, 1.0),
                z=-1,
            )
        ix, iy, iw, ih = layout["image_rect"]
        self._backend.draw_image(
            texture,
            ix,
            iy,
            iw,
            ih,
            alpha=1.0,
            uv_offset=(0.0, 0.0),
            uv_scale=(1.0, 1.0),
            z=0.0,
        )

    def _write_current_media(self) -> None:
        try:
            if self._current_idx < 0 or not self._queue:
                data = {
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
                        file_hash = _hash_image_file(item.original_path)
                        hash_thumb = Path("/opt/metixel/cache/thumbnails") / f"{file_hash}.jpg"
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
            run_dir = os.environ.get("METIXEL_RUN_DIR", "/run/metixel")
            os.makedirs(run_dir, exist_ok=True)
            tmp = os.path.join(run_dir, ".current_media.tmp")
            dst = os.path.join(run_dir, "current_media.json")
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, dst)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Folder scanning
    # ------------------------------------------------------------------

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
            if suffix not in self.IMAGE_EXTENSIONS:
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

    # ------------------------------------------------------------------
    # Hot reload
    # ------------------------------------------------------------------

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
