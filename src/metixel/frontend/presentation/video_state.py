# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Non-blocking video playback state machine for the presentation engine."""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import time

from metixel.frontend.presentation.base import BaseEngineState
from metixel.shared.models import MediaItem, MediaType

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


class VideoStateMachineMixin(BaseEngineState):
    """Non-blocking video playback state machine for the presentation engine."""

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
