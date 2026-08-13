# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Slideshow scheduling, timing and control (next/prev/pause/resume)."""

from __future__ import annotations

import logging
import os
import signal
import time

from metixel.frontend.presentation.base import BaseEngineState
from metixel.frontend.presentation.video_state import _VIDEO_IDLE, _VIDEO_PLAYING
from metixel.shared.models import MediaType

logger = logging.getLogger(__name__)


class SlideshowSchedulerMixin(BaseEngineState):
    """Slideshow scheduling, timing and control (next/prev/pause/resume)."""

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
