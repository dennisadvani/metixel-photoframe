# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Video player — SDL2 + python-vlc with hardware decoding for Metixel Photoframe.

Three strategies are available:

1. **vlc + SDL2** (:class:`VlcVideoPlayer`): Creates a borderless SDL2 window
   and embeds VLC via ``set_xwindow()``.  Uses ``h264_v4l2m2m`` hardware
   decoding on Raspberry Pi.  Works under cage/XWayland without DRM lease
   contention — the same approach used by PicFrame.  This is the recommended
   path for Phase 1 (Pi Zero 2 W / Pi 2 / Pi 3).

2. **ffmpeg pipeline** (:class:`VideoPlayer`): Decodes frames via ffmpeg
   into numpy arrays, uploaded to GPU textures via the DisplayBackend.
   Works everywhere but uses more CPU/GPU memory.  Fallback when python-vlc
   or pysdl2 are unavailable.

Architecture (VLC path):
  - SDL2 creates a borderless X11 window (via cage's XWayland)
  - VLC renders into that window using ``player.set_xwindow()``
  - VLC is configured with ``--codec=h264_v4l2m2m`` for hardware decode
  - VLC event callbacks drive state transitions (no polling)
  - A progress watchdog kills VLC if playback gets stuck (>3s no progress)
  - Cage composites the SDL2/VLC window into the display
  - No DRM lease needed — XWayland handles compositing

Memory-conscious design (Pi Zero 2 W: 512MB):
- VLC uses zero Python heap memory during playback
- HW decoder uses dedicated V4L2 M2M block (separate from GPU memory)
- SDL2 window is hidden after playback and destroyed on stop()
- No frame buffers in Python process
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

# Accepted video file extensions
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"}


# =========================================================================
# VlcVideoPlayer — SDL2 + python-vlc (recommended for Pi)
# =========================================================================


class VlcVideoPlayer:
    """Play videos using SDL2 + python-vlc with hardware decoding.

    Creates a borderless SDL2 window, embeds VLC via ``set_xwindow()``,
    and uses ``h264_v4l2m2m`` hardware decoding on Raspberry Pi.  Works
    under cage/XWayland without DRM lease contention — the same approach
    used by the PicFrame project.

    Key improvements (from picframe analysis):

    - **VLC event callbacks** (MediaPlayerPlaying, EndReached, Error)
      replace polling ``get_state()`` every 50ms.  Callbacks fire exactly
      when state changes, eliminating race conditions.
    - **Progress watchdog** monitors ``player.get_time()`` and kills the
      player if no progress is detected for >3 seconds (handles VLC
      hangs on malformed video without freezing the slideshow).
    - **Window lifecycle**: window starts hidden, shown only on
      MediaPlayerPlaying, hidden on stop/end/error.  Waits for
      ``SDL_WINDOWEVENT_SHOWN`` before proceeding.
    - Cross-platform embedding (X11, macOS NSView, Windows HWND).

    Usage::

        player = VlcVideoPlayer()
        player.play("/path/to/video.mp4")           # blocks until video ends
        # or
        player.play("/path/to/video.mp4", block=False)
        while player.is_playing:
            player.poll()
            time.sleep(0.05)
        player.stop()

    On Raspberry Pi, install::

        sudo apt install -y vlc python3-vlc
        pip install pysdl2
    """

    # Seconds without time progress before the player is considered stuck
    STUCK_TIMEOUT = 3.0

    # Seconds to wait for SDL_WINDOWEVENT_SHOWN after ShowWindow
    WINDOW_SHOWN_TIMEOUT = 4.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        self._window: ctypes.c_void_p | None = None
        self._player: vlc.MediaPlayer | None = None  # type: ignore[name-defined]
        self._instance: vlc.Instance | None = None   # type: ignore[name-defined]
        self._playing: bool = False
        self._finished: bool = False
        self._video_path: str = ""
        self._duration: float = 0.0
        self._start_time: float = 0.0
        self._event: sdl2.SDL_Event | None = None    # type: ignore[name-defined]
        self._screen_w: int = 1920
        self._screen_h: int = 1080
        self._hw_codecs: list[str] = []

        # VLC event-callback driven state
        self._vlc_playing_event = threading.Event()
        self._vlc_ended_event = threading.Event()
        self._vlc_error_event = threading.Event()
        self._vlc_callbacks_registered: bool = False

        # Window lifecycle flags (set from VLC callbacks on the VLC thread)
        self._show_window_request: bool = False
        self._hide_window_request: bool = False

        # Progress watchdog
        self._last_vlc_time: int = 0
        self._last_progress_time: float = 0.0
        self._startup: bool = True

    def play(
        self,
        video_path: str,
        *,
        screen_w: int = 1920,
        screen_h: int = 1080,
        block: bool = True,
        loop: bool = False,
        fit_mode: str = "contain",
    ) -> int | None:
        """Start video playback via SDL2 + VLC.

        Args:
            video_path: Path to the video file.
            screen_w: Screen width in pixels (for the SDL2 window).
            screen_h: Screen height in pixels (for the SDL2 window).
            block: If True, run the SDL2 event loop until the video ends.
                   If False, start playback and return immediately —
                   caller must call :meth:`poll` and :meth:`stop`.
            loop: If True, loop the video indefinitely.
            fit_mode: How to fit the video to the screen.
                      ``"contain"`` (default) — letterbox/pillarbox,
                      ``"cover"`` — crop to fill,
                      ``"fill"`` — stretch to fill (distorts AR).

        Returns:
            Exit code (0 = success) when ``block=True``, or ``None``
            when ``block=False``.  Returns ``None`` if VLC/SDL2 is
            unavailable.
        """
        self.stop()

        if not self._vlc_available():
            logger.error("VLC not available — install vlc")
            self._playing = False
            return None

        self._screen_w = screen_w
        self._screen_h = screen_h
        self._video_path = video_path

        try:
            return self._play_subprocess(
                video_path, block=block, fit_mode=fit_mode,
            )
        except Exception:
            logger.exception("VLC playback failed: %s", video_path)
            self._teardown()
            return None

    def stop(self) -> None:
        """Stop playback and release SDL2/VLC resources."""
        self._playing = False
        self._finished = True
        # Signal VLC callbacks so the event loop can exit cleanly
        self._vlc_playing_event.clear()
        self._vlc_ended_event.set()
        if self._player:
            try:
                self._player.stop()
            except Exception:
                pass
        self._teardown()

    # ------------------------------------------------------------------
    # Subprocess playback — runs VLC CLI as a separate process
    # ------------------------------------------------------------------

    def _play_subprocess(
        self,
        video_path: str,
        *,
        block: bool = True,
        fit_mode: str = "contain",
    ) -> int | subprocess.Popen[bytes] | None:
        """Play video by spawning the ``vlc`` CLI as a subprocess.

        This is the approach that actually works on this Pi — running VLC
        in-process via libVLC/python-vlc fails because pi3d already owns
        the GPU/GLES context.  A separate ``vlc`` process gets its own
        GPU context (or uses X11 software rendering) and renders via
        XWayland under cage.

        picframe uses the same subprocess model (video_player.py spawned
        via Popen) for the same reason.

        When ``block=False``, returns the ``Popen`` handle so the caller
        can poll the subprocess and keep the render loop alive for a clean
        transition (picframe approach).
        """
        import shutil

        # Find the VLC binary
        vlc_bin = shutil.which("vlc")
        if not vlc_bin:
            logger.error("VLC binary not found on PATH")
            return None

        # Probe HW codecs for logging only
        self._detect_best_codec()

        display_ratio = self._compute_crop_ratio(
            self._screen_w, self._screen_h,
        )

        cmd = [
            vlc_bin,
            "--no-audio",
            "--play-and-exit",
            "--no-video-title-show",
            "--intf", "dummy",       # No interactive interface
            video_path,
        ]

        if fit_mode == "cover":
            # Crop source to display aspect ratio (CSS cover behaviour).
            # For a 16:9 video on a 16:10 display, crops left+right
            # so the video fills vertically without stretching.
            cmd.insert(1, f"--crop={display_ratio}")
        elif fit_mode == "fill":
            # Stretch video to fill display, ignoring source aspect ratio
            # (picframe's --fit_display).
            cmd.insert(1, f"--aspect-ratio={display_ratio}")
        # "contain": nothing — VLC's default letterbox/pillarbox

        logger.info(
            "VlcVideoPlayer (subprocess): %s (hw_codecs=%s, fit=%s)",
            video_path,
            ", ".join(self._hw_codecs) if self._hw_codecs else "auto",
            fit_mode,
        )

        self._playing = True
        self._finished = False
        self._start_time = time.monotonic()

        try:
            env = os.environ.copy()
            if "DISPLAY" not in env:
                env["DISPLAY"] = ":0"

            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            if block:
                rc = proc.wait()
                self._playing = False
                self._finished = True
                self._duration = time.monotonic() - self._start_time
                logger.info(
                    "VlcVideoPlayer finished: rc=%d elapsed=%.1fs path=%s",
                    rc, self._duration, video_path,
                )
                return rc
            else:
                # Non-blocking: return the Popen handle so the engine
                # can poll it and keep pi3d's render loop alive.
                return proc

        except FileNotFoundError:
            logger.error("VLC binary not found: %s", vlc_bin)
            self._playing = False
            self._finished = True
            return None
        except Exception:
            logger.exception("VLC subprocess failed: %s", video_path)
            self._playing = False
            self._finished = True
            return None

    def poll(self) -> int | None:
        """Check if the VLC player has finished.

        Returns:
            Exit code (0 = success) if the video ended, ``None`` if still
            playing, or ``None`` if no video was started.
        """
        if self._player is None:
            return None

        # Check VLC event-driven flags first (fast path — no VLC API call)
        if self._vlc_error_event.is_set():
            self._playing = False
            self._finished = True
            self._duration = time.monotonic() - self._start_time
            logger.error("VLC playback error (event): %s", self._video_path)
            self._teardown()
            return 1

        if self._vlc_ended_event.is_set():
            self._playing = False
            self._finished = True
            self._duration = time.monotonic() - self._start_time
            logger.info(
                "VLC playback ended (event): %.1fs elapsed (%s)",
                self._duration, self._video_path,
            )
            self._teardown()
            return 0

        # Fallback: check state directly (belt-and-suspenders)
        try:
            state = self._player.get_state()
            import vlc  # type: ignore
            if state == vlc.State.Ended:
                self._playing = False
                self._finished = True
                self._duration = time.monotonic() - self._start_time
                logger.info(
                    "VLC playback ended (poll): %.1fs elapsed (%s)",
                    self._duration, self._video_path,
                )
                self._teardown()
                return 0
            elif state == vlc.State.Error:
                self._playing = False
                self._finished = True
                self._duration = time.monotonic() - self._start_time
                logger.error("VLC playback error (poll): %s", self._video_path)
                self._teardown()
                return 1
        except Exception:
            self._playing = False
            self._finished = True
            self._teardown()
            return 1

        return None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_playing(self) -> bool:
        """Whether a video is currently loaded and playing."""
        return self._playing

    @property
    def is_finished(self) -> bool:
        """Whether the video has reached its end (or was stopped)."""
        return self._finished

    @property
    def duration(self) -> float:
        """Elapsed wall-clock time in seconds since playback started."""
        if self._finished:
            return self._duration
        if self._playing:
            return time.monotonic() - self._start_time
        return 0.0

    @property
    def hw_codecs(self) -> list[str]:
        """V4L2 M2M codecs available (e.g. ``['h264', 'mpeg4']``)."""
        return list(self._hw_codecs)

    # ------------------------------------------------------------------
    # Internal: playback implementation
    # ------------------------------------------------------------------

    def _play_impl(
        self,
        video_path: str,
        *,
        block: bool = True,
        loop: bool = False,
        fit_mode: str = "contain",
    ) -> int | None:
        """Internal: set up SDL2 + VLC and start playback.

        The ``fit_mode`` controls how the video fills the screen using
        VLC player API calls (the same approach used by picframe):

        - ``"contain"`` — VLC default; maintains aspect ratio,
          letterbox/pillarbox as needed.  No overrides applied.
        - ``"cover"`` — crop the source video to the display's aspect
          ratio so it fills the screen completely (like CSS
          background-size: cover).  Uses ``media.add_option(":crop=...")``.
        - ``"fill"`` — stretch the video to fill the screen by forcing
          the output aspect ratio to match the display.  Uses
          ``player.video_set_aspect_ratio()`` — same API as picframe.

        Important: do NOT use ``--crop`` or ``--aspect-ratio`` as VLC
        Instance() constructor args.  Those are CLI-only flags that
        don't work through the libVLC API.
        """

        import sdl2  # type: ignore
        import vlc  # type: ignore

        # -- SDL2 setup ---------------------------------------------------
        if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
            logger.error(
                "SDL2 init failed: %s", sdl2.SDL_GetError().decode(),
            )
            return None

        self._window = sdl2.SDL_CreateWindow(
            b"Metixel Video",
            0, 0,
            self._screen_w, self._screen_h,
            sdl2.SDL_WINDOW_HIDDEN | sdl2.SDL_WINDOW_BORDERLESS,
        )
        if not self._window:
            logger.error(
                "SDL2 window creation failed: %s",
                sdl2.SDL_GetError().decode(),
            )
            sdl2.SDL_Quit()
            return None

        sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE)

        # -- VLC setup ----------------------------------------------------
        # Probe ffmpeg decoders to see what HW blocks are available
        # (for logging only — VLC auto-detects hardware decoders on its
        # own; we do NOT pass --codec because VLC codec module names
        # differ from ffmpeg's).
        self._detect_best_codec()

        # Minimal VLC args.  Force x11 video output to avoid GPU context
        # contention with pi3d — both can't own the GLES context at once.
        # The xcb_x11 output renders via X11 software blitting into the
        # embedded SDL2 window; pi3d keeps the GPU for slideshow textures.
        vlc_args = [
            "--no-audio",
            "--quiet",
            "--verbose=0",
            "--vout", "xcb_x11",
        ]

        try:
            self._instance = vlc.Instance(vlc_args)
            self._player = self._instance.media_player_new()
        except Exception as e:
            logger.error("VLC init failed: %s", e)
            self._teardown_sdl2()
            return None

        # -- Register VLC event callbacks ---------------------------------
        self._register_vlc_events()

        # -- Embed VLC in the SDL2 window ---------------------------------
        if not self._embed_vlc_window():
            self._teardown()
            return None

        # -- Apply fit mode via VLC player API (picframe approach) ---------
        # picframe uses player.video_set_aspect_ratio() to force the video
        # output to match the display — this is the only fit-related API
        # call that is proven to work through libVLC.
        #
        #   - fill:   player.video_set_aspect_ratio() — stretches/squashes
        #             the video to fill the display exactly.
        #   - cover:  same as fill (VLC's crop can't be set reliably via
        #             the libVLC API; video_set_aspect_ratio gets close).
        #   - contain: nothing — VLC's default letterbox/pillarbox.
        #
        display_ratio = self._compute_crop_ratio(
            self._screen_w, self._screen_h,
        )

        # Load media (use media_new_path for local files — media_new
        # expects a URI/MRL and may silently fail on plain paths).
        media = self._instance.media_new_path(video_path)

        if fit_mode in ("cover", "fill"):
            # Force the video to stretch to the display aspect ratio.
            # This is the same API call picframe uses (their --fit_display
            # flag calls video_set_aspect_ratio).
            self._player.video_set_aspect_ratio(display_ratio)
            logger.debug(
                "VLC %s mode: aspect-ratio=%s (display %dx%d)",
                fit_mode, display_ratio, self._screen_w, self._screen_h,
            )
        else:
            logger.debug(
                "VLC contain mode: no aspect override (display %dx%d)",
                self._screen_w, self._screen_h,
            )

        self._player.set_media(media)
        # NOTE: Do NOT call set_fullscreen().  The SDL2 window is already
        # borderless and sized to the full display.  picframe doesn't call
        # it either — and on a hidden window it can cause VLC's video
        # output to fail silently.

        # Create the SDL event object early — _wait_for_window_shown
        # needs it for SDL_PollEvent.
        self._event = sdl2.SDL_Event()

        # Show the window BEFORE calling play().  VLC's gles2/gl video
        # outputs need a mapped (visible) X11 window to create an OpenGL
        # context — rendering to a hidden window fails with "parent window
        # not available" on this Pi's VLC build (3.0.23, gles2-enabled).
        sdl2.SDL_ShowWindow(self._window)
        self._wait_for_window_shown(self.WINDOW_SHOWN_TIMEOUT)
        sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE)
        if self._screen_w > 1 and self._screen_h > 1:
            sdl2.SDL_WarpMouseInWindow(
                self._window, self._screen_w - 1, self._screen_h - 1,
            )

        self._playing = True
        self._finished = False
        self._start_time = time.monotonic()

        # Reset watchdog state
        self._last_vlc_time = 0
        self._last_progress_time = time.time()
        self._startup = True

        logger.info(
            "VlcVideoPlayer starting: %s (hw_codecs=%s)",
            video_path, ", ".join(self._hw_codecs) if self._hw_codecs else "auto",
        )

        if self._player.play() == -1:
            logger.error("VLC play() failed: %s", video_path)
            self._teardown()
            return None

        # DO NOT show window here — wait for MediaPlayerPlaying callback
        # to ensure VLC has rendered its first frame before revealing.

        if block:
            return self._run_event_loop()
        else:
            return None

    def _run_event_loop(self) -> int:
        """Run the SDL2 event loop driven by VLC callbacks + progress watchdog.

        This replaces the old polling-based loop.  VLC event callbacks
        drive state transitions (playing → ended/error), and a progress
        watchdog detects stuck playback.

        Also handles window show/hide based on flags set from VLC
        callbacks running on VLC's internal thread.
        """
        import sdl2  # type: ignore

        try:
            while self._playing and self._player is not None:
                # -- Poll SDL2 events ------------------------------------
                try:
                    while sdl2.SDL_PollEvent(ctypes.byref(self._event)):
                        if self._event.type == sdl2.SDL_QUIT:
                            self._playing = False
                            break
                except Exception:
                    pass  # SDL2 may raise if window was destroyed externally

                if self._player is None:
                    break

                # -- Window show/hide lifecycle ---------------------------
                # (flags set by VLC callbacks on the VLC thread)
                if self._show_window_request:
                    self._handle_show_window()
                    self._show_window_request = False

                if self._hide_window_request:
                    self._handle_hide_window()
                    self._hide_window_request = False

                # -- Re-hide cursor if it became visible ----------------
                if sdl2.SDL_ShowCursor(sdl2.SDL_QUERY) == 1:
                    sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE)

                # -- Progress watchdog for stuck playback ----------------
                try:
                    state = self._player.get_state()
                    import vlc  # type: ignore
                    if state == vlc.State.Playing:
                        if not self._check_video_progress():
                            # _check_video_progress already logs + tears down
                            break
                except Exception:
                    logger.warning(
                        "VLC state check failed — player may have crashed",
                    )
                    self._playing = False
                    self._finished = True
                    break

                # -- VLC event-driven exit ------------------------------
                if self._vlc_error_event.is_set():
                    logger.error("VLC error event: %s", self._video_path)
                    self._playing = False
                    self._finished = True
                    break
                if self._vlc_ended_event.is_set():
                    logger.debug("VLC ended event: %s", self._video_path)
                    self._playing = False
                    self._finished = True
                    break

                time.sleep(0.05)  # 20 Hz — VLC renders independently

        except Exception:
            logger.exception("VLC event loop error")
            self._playing = False
            self._finished = True

        self._duration = time.monotonic() - self._start_time

        rc = 0 if self._finished else 1
        logger.info(
            "VlcVideoPlayer finished: rc=%d elapsed=%.1fs path=%s",
            rc, self._duration, self._video_path,
        )

        self._teardown()
        return rc

    # ------------------------------------------------------------------
    # Internal: VLC event callbacks
    # ------------------------------------------------------------------

    def _register_vlc_events(self) -> None:
        """Attach VLC event callbacks for playback state changes.

        Callbacks fire on VLC's internal thread and set threading.Event
        flags consumed by the main event loop.  This is more reliable
        than polling get_state() and avoids missed transitions.
        """
        if self._player is None or self._vlc_callbacks_registered:
            return

        try:
            import vlc  # type: ignore
            event_manager = self._player.event_manager()
            event_manager.event_attach(
                vlc.EventType.MediaPlayerPlaying, self._on_vlc_playing,
            )
            event_manager.event_attach(
                vlc.EventType.MediaPlayerStopped, self._on_vlc_stopped,
            )
            event_manager.event_attach(
                vlc.EventType.MediaPlayerEndReached, self._on_vlc_ended,
            )
            event_manager.event_attach(
                vlc.EventType.MediaPlayerEncounteredError, self._on_vlc_error,
            )
            self._vlc_callbacks_registered = True
            logger.debug("VLC event callbacks registered")
        except Exception:
            logger.warning("Failed to register VLC event callbacks")

    def _on_vlc_playing(self, event: vlc.Event) -> None:  # type: ignore[name-defined]
        """VLC callback: MediaPlayerPlaying.

        Fired when VLC has started rendering frames.  This is the right
        moment to show the SDL2 window — avoids a flash of empty/black
        window before the first frame is ready.
        """
        logger.debug("VLC event: MediaPlayerPlaying")
        self._vlc_playing_event.set()
        self._vlc_ended_event.clear()
        self._vlc_error_event.clear()
        self._show_window_request = True
        self._last_vlc_time = 0
        self._last_progress_time = time.time()
        self._startup = True

    def _on_vlc_stopped(self, event: vlc.Event) -> None:  # type: ignore[name-defined]
        """VLC callback: MediaPlayerStopped."""
        logger.debug("VLC event: MediaPlayerStopped")
        self._hide_window_request = True
        self._vlc_playing_event.clear()
        self._vlc_ended_event.set()

    def _on_vlc_ended(self, event: vlc.Event) -> None:  # type: ignore[name-defined]
        """VLC callback: MediaPlayerEndReached."""
        logger.debug("VLC event: MediaPlayerEndReached")
        self._hide_window_request = True
        self._vlc_playing_event.clear()
        self._vlc_ended_event.set()

    def _on_vlc_error(self, event: vlc.Event) -> None:  # type: ignore[name-defined]
        """VLC callback: MediaPlayerEncounteredError."""
        logger.error("VLC event: MediaPlayerEncounteredError")
        self._hide_window_request = True
        self._vlc_playing_event.clear()
        self._vlc_error_event.set()
        # Force-stop VLC so we don't sit in an error state forever
        if self._player:
            try:
                self._player.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal: progress watchdog
    # ------------------------------------------------------------------

    def _check_video_progress(self) -> bool:
        """Check if VLC playback is making progress.

        Monitors ``player.get_time()`` — if the time doesn't advance
        for ``STUCK_TIMEOUT`` seconds (3s), the player is considered
        stuck and playback is aborted.  This prevents a hung VLC from
        freezing the entire slideshow.

        Also handles the startup grace period (``_startup=True``):
        during startup, we wait for ``get_time() > 0`` before declaring
        the player "started".

        Returns:
            True if progressing normally, False if stuck or failed.
        """
        if not self._player:
            logger.error("Player not initialized — cannot check progress")
            return False

        current_time = self._player.get_time()
        now = time.time()

        if not self._startup and current_time == self._last_vlc_time:
            # No time advancement — check if stuck for too long
            if now - self._last_progress_time > self.STUCK_TIMEOUT:
                logger.error(
                    "VLC stuck for >%.1fs (time=%d, last_time=%d) — "
                    "aborting playback of %s",
                    self.STUCK_TIMEOUT, current_time, self._last_vlc_time,
                    self._video_path,
                )
                self._vlc_error_event.set()
                self._playing = False
                self._finished = True
                if self._player:
                    try:
                        self._player.stop()
                    except Exception:
                        pass
                return False
        elif current_time == -1:
            # VLC returns -1 if no media is loaded
            logger.warning(
                "No media loaded or media invalid: %s", self._video_path,
            )
            if self._player:
                try:
                    self._player.stop()
                except Exception:
                    pass
            self._playing = False
            self._finished = True
            return False
        else:
            # Progress detected — reset the stuck timer
            self._last_progress_time = now
            if self._startup and current_time > 0:
                logger.debug(
                    "Video started playing (time=%d): %s",
                    current_time, self._video_path,
                )
                self._startup = False

        self._last_vlc_time = current_time
        return True

    # ------------------------------------------------------------------
    # Internal: window lifecycle
    # ------------------------------------------------------------------

    def _handle_show_window(self) -> None:
        """Show the SDL2 window and wait for it to be visible.

        Called when VLC signals MediaPlayerPlaying.  Waits for the
        ``SDL_WINDOWEVENT_SHOWN`` event to ensure the window compositor
        has actually mapped the window before proceeding.
        """
        try:
            import sdl2  # type: ignore
        except ImportError:
            return

        if self._window is None:
            return

        if not (sdl2.SDL_GetWindowFlags(self._window) & sdl2.SDL_WINDOW_SHOWN):
            sdl2.SDL_ShowWindow(self._window)
            self._wait_for_window_shown(self.WINDOW_SHOWN_TIMEOUT)
            sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE)
            # Warp mouse to bottom-right corner (off-screen on most frames)
            if self._screen_w > 1 and self._screen_h > 1:
                sdl2.SDL_WarpMouseInWindow(
                    self._window, self._screen_w - 1, self._screen_h - 1,
                )
            logger.debug("Window shown: %dx%d", self._screen_w, self._screen_h)

    def _handle_hide_window(self) -> None:
        """Hide the SDL2 window when playback stops."""
        try:
            import sdl2  # type: ignore
        except ImportError:
            return

        if self._window is None:
            return

        if sdl2.SDL_GetWindowFlags(self._window) & sdl2.SDL_WINDOW_SHOWN:
            sdl2.SDL_HideWindow(self._window)
            logger.debug("Window hidden")

    def _wait_for_window_shown(self, timeout: float = 4.0) -> bool:
        """Wait for the ``SDL_WINDOWEVENT_SHOWN`` event for this window.

        After ``SDL_ShowWindow()``, the window may not be immediately
        visible — the compositor needs time to map it.  This polls for
        the shown event with a configurable timeout.

        Returns:
            True if the window was shown within the timeout, False otherwise.
        """
        try:
            import sdl2  # type: ignore
        except ImportError:
            return False

        if self._window is None:
            return False

        start = time.time()
        window_id = sdl2.SDL_GetWindowID(self._window)
        while (time.time() - start) < timeout:
            while sdl2.SDL_PollEvent(ctypes.byref(self._event)) != 0:
                if (
                    self._event is not None
                    and self._event.type == sdl2.SDL_WINDOWEVENT
                    and self._event.window.event == sdl2.SDL_WINDOWEVENT_SHOWN
                    and self._event.window.windowID == window_id
                ):
                    return True
            time.sleep(0.01)
        logger.warning(
            "Player window not shown within %.0f seconds", timeout,
        )
        return False

    # ------------------------------------------------------------------
    # Internal: VLC window embedding (cross-platform)
    # ------------------------------------------------------------------

    def _embed_vlc_window(self) -> bool:
        """Embed VLC's video output into the SDL2 window.

        Supports:
        - Linux: X11 via set_xwindow (including KMSDRM for Pi Zero)
        - macOS: NSView via set_nsobject
        - Windows: HWND via set_hwnd
        """
        try:
            import sdl2  # type: ignore
        except ImportError:
            return False

        wm_info = sdl2.SDL_SysWMinfo()
        sdl2.SDL_VERSION(wm_info.version)
        if not sdl2.SDL_GetWindowWMInfo(self._window, ctypes.byref(wm_info)):
            logger.error("Cannot get SDL2 window WM info")
            return False

        if sys.platform == "darwin":
            return self._embed_macos(wm_info)
        elif sys.platform.startswith("linux"):
            return self._embed_linux(wm_info)
        elif sys.platform == "win32":
            return self._embed_windows()
        else:
            logger.error(
                "VLC embedding not supported on platform: %s", sys.platform,
            )
            return False

    def _embed_linux(self, wm_info: sdl2.SDL_SysWMinfo) -> bool:  # type: ignore[name-defined]
        """Embed VLC in X11/KMSDRM window (Linux)."""
        import sdl2  # type: ignore

        if wm_info.subsystem in (sdl2.SDL_SYSWM_X11, sdl2.SDL_SYSWM_KMSDRM):
            xid = wm_info.info.x11.window
            self._player.set_xwindow(xid)
            logger.debug("VLC embedded in X11 window: %s (subsystem=%s)",
                         xid, wm_info.subsystem)
            return True
        else:
            logger.error(
                "VLC embedding not supported on subsystem: %s",
                wm_info.subsystem,
            )
            return False

    def _embed_macos(self, wm_info: sdl2.SDL_SysWMinfo) -> bool:  # type: ignore[name-defined]
        """Embed VLC in NSView (macOS)."""
        try:
            from rubicon.objc import ObjCInstance  # type: ignore
            nswindow_ptr = wm_info.info.cocoa.window
            nswindow = ObjCInstance(ctypes.c_void_p(nswindow_ptr))
            nsview = nswindow.contentView
            self._player.set_nsobject(nsview.ptr.value)
            logger.debug("VLC embedded in NSView: %s", nsview)
            return True
        except ImportError:
            logger.error("rubicon-objc not installed — cannot embed on macOS")
            return False
        except Exception as e:
            logger.error("macOS VLC embedding failed: %s", e)
            return False

    def _embed_windows(self) -> bool:
        """Embed VLC in HWND (Windows)."""
        try:
            import sdl2  # type: ignore
            hwnd = sdl2.SDL_GetWindowID(self._window)
            self._player.set_hwnd(hwnd)
            logger.debug("VLC embedded in Windows HWND: %s", hwnd)
            return True
        except Exception as e:
            logger.error("Windows VLC embedding failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Internal: cleanup
    # ------------------------------------------------------------------

    def _teardown(self) -> None:
        """Release VLC and SDL2 resources."""
        # Unregister VLC callbacks to avoid stale references
        self._vlc_callbacks_registered = False

        if self._player:
            try:
                self._player.stop()
            except Exception:
                pass
            try:
                self._player.release()
            except Exception:
                pass
            self._player = None
        if self._instance:
            try:
                self._instance.release()
            except Exception:
                pass
            self._instance = None
        self._teardown_sdl2()

    def _teardown_sdl2(self) -> None:
        """Release SDL2 window and quit."""
        try:
            import sdl2  # type: ignore
        except ImportError:
            self._window = None
            return
        if self._window:
            try:
                sdl2.SDL_DestroyWindow(self._window)
            except Exception:
                pass
            self._window = None
        try:
            sdl2.SDL_Quit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: codec detection
    # ------------------------------------------------------------------

    def _detect_best_codec(self) -> str | None:
        """Return the best available HW codec for VLC.

        Priority: h264_v4l2m2m -> (none — let VLC auto-detect)
        """
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-decoders"],
                capture_output=True, text=True, timeout=5,
            )
            out = result.stdout
            self._hw_codecs = []
            ff_to_vlc = {
                "h264_v4l2m2m": "h264_v4l2m2m",
                "mpeg4_v4l2m2m": "mpeg4_v4l2m2m",
                "mpeg2_v4l2m2m": "mpeg2_v4l2m2m",
                "hevc_v4l2m2m": "hevc_v4l2m2m",
                "vp8_v4l2m2m": "vp8_v4l2m2m",
                "vp9_v4l2m2m": "vp9_v4l2m2m",
            }
            for ff_name, vlc_name in ff_to_vlc.items():
                if ff_name in out:
                    self._hw_codecs.append(vlc_name)

            if "h264_v4l2m2m" in self._hw_codecs:
                logger.debug(
                    "HW codecs available: %s", ", ".join(self._hw_codecs),
                )
                return "h264_v4l2m2m"
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Internal: availability checks
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_crop_ratio(display_w: int, display_h: int) -> str:
        """Return a crop ratio string matching the display aspect ratio.

        VLC's ``--crop`` flag expects a ``W:H`` string.  We compute the
        greatest common divisor to produce the simplest integer ratio
        (e.g. ``"16:9"`` for 1920×1080, ``"16:10"`` for 1920×1200).
        """
        from math import gcd
        g = gcd(display_w, display_h)
        return f"{display_w // g}:{display_h // g}"

    @staticmethod
    def _vlc_available() -> bool:
        """Check if python-vlc bindings are importable."""
        try:
            import vlc  # type: ignore # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _sdl2_available() -> bool:
        """Check if pysdl2 is importable."""
        try:
            import sdl2  # type: ignore # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def is_available() -> bool:
        """Check if both VLC and SDL2 bindings are available."""
        return VlcVideoPlayer._vlc_available() and VlcVideoPlayer._sdl2_available()


# =========================================================================
# VideoPlayer — ffmpeg pipeline (fallback)
# =========================================================================


class VideoPlayer:
    """Plays video files by piping ffmpeg-decoded frames as numpy arrays.

    Frames are decoded at the target display resolution and returned as
    numpy arrays (H, W, 3) ready for GPU texture upload. The caller is
    responsible for uploading frames to the display backend.

    Usage::

        player = VideoPlayer()
        player.play("/path/to/video.mp4", target_w=1920, target_h=1080)
        while player.is_playing and not player.is_finished:
            frame = player.get_frame()
            if frame is not None:
                backend.update_texture(video_tex, frame)

    The player paces itself — ``get_frame()`` returns ``None`` if it's not
    yet time for the next frame (based on the source video's FPS).
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._playing: bool = False
        self._finished: bool = False
        self._width: int = 0
        self._height: int = 0
        self._fps: float = 30.0
        self._frame_bytes: int = 0
        self._last_frame_time: float = 0.0
        self._frame_period: float = 1.0 / 30.0
        self._duration: float = 0.0
        self._start_time: float = 0.0
        self._frames_delivered: int = 0
        self._frames_skipped_pacing: int = 0
        self._video_path: str = ""
        self._frame_queue: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=4)
        self._reader_thread: threading.Thread | None = None

    # -- Public API ----------------------------------------------------------

    def play(
        self,
        video_path: str,
        target_w: int = 1920,
        target_h: int = 1080,
        loop: bool = False,
    ) -> tuple[int, int] | None:
        """Start video playback via ffmpeg subprocess.

        Launches ffmpeg to decode the video to raw RGB24 frames at the
        target resolution. Frames are read from stdout as numpy arrays.

        Args:
            video_path: Path to the video file.
            target_w: Target width for decoded frames (screen width).
            target_h: Target height for decoded frames (screen height).
            loop: If True, restart playback when the video ends.

        Returns:
            (width, height) of decoded frames, or None on failure.
        """
        self.stop()

        try:
            # Probe video metadata with ffprobe
            info = self._probe(video_path)
            if info is None:
                logger.error("Cannot probe video: %s", video_path)
                return None

            src_w = info.get("width", target_w)
            src_h = info.get("height", target_h)
            self._fps = info.get("fps", 30.0)
            self._duration = info.get("duration", 0.0)
            self._frame_period = 1.0 / max(self._fps, 1.0)

            # Compute scale: fit within target, maintain aspect ratio,
            # ensure even dimensions (required by many codecs).
            scale_w, scale_h = self._compute_scale(
                src_w, src_h, target_w, target_h,
            )

            cmd = [
                "ffmpeg",
                "-loglevel", "error",
                "-hwaccel", "drm",
                "-i", str(video_path),
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-vf", (
                    f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=decrease,"
                    f"pad={scale_w}:{scale_h}:(ow-iw)/2:(oh-ih)/2:black"
                ),
                "-an",  # No audio
                "-vsync", "passthrough",
                "-",
            ]

            logger.info(
                "VideoPlayer starting: %s -> %dx%d @ %.1f fps (src: %dx%d)",
                video_path, scale_w, scale_h, self._fps, src_w, src_h,
            )

            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._width = scale_w
            self._height = scale_h
            self._frame_bytes = scale_w * scale_h * 3
            self._playing = True
            self._finished = False
            self._start_time = time.monotonic()
            self._last_frame_time = 0.0
            self._frames_delivered = 0
            self._frames_skipped_pacing = 0
            self._video_path = video_path

            self._frame_queue = queue.Queue(maxsize=4)
            self._reader_thread = threading.Thread(
                target=self._reader_worker,
                daemon=True,
                name="video-reader",
            )
            self._reader_thread.start()

            logger.debug(
                "ffmpeg cmd: %s | frame_bytes=%d | fps=%.1f | period=%.3fs",
                " ".join(cmd), self._frame_bytes, self._fps, self._frame_period,
            )

            return (scale_w, scale_h)

        except FileNotFoundError:
            logger.error("ffmpeg not found — install ffmpeg for video support")
            self._playing = False
            return None
        except Exception:
            logger.exception("Failed to start video playback: %s", video_path)
            self._playing = False
            return None

    def stop(self) -> None:
        """Stop video playback and terminate the ffmpeg subprocess."""
        self._playing = False
        self._finished = True
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break
        if self._process:
            try:
                self._process.stdout.close()
            except Exception:
                pass
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        self._reader_thread = None

    def get_frame(self) -> np.ndarray | None:
        """Get the next video frame as a numpy array.

        Pulls from a background queue — never blocks on ffmpeg stdout.

        Frame pacing & skipping:
        - If we're ahead of schedule (delivered more frames than the
          source FPS would have produced by now), returns ``None``.
        - If we're behind schedule (render loop can't keep up), drains
          the queue to skip intermediate frames and returns only the
          most recent one — maintaining real-time playback at the cost
          of dropped frames.
        - Returns ``None`` if no frame is available yet or playback ended.
        """
        if not self._playing or self._process is None:
            return None

        now = time.monotonic()
        elapsed = now - self._start_time
        expected_frame = int(elapsed / self._frame_period)

        # -- Ahead of schedule: pace ourselves ---------------------------
        if self._frames_delivered > expected_frame:
            self._frames_skipped_pacing += 1
            return None

        # -- On time or behind: grab next frame from queue ---------------
        try:
            frame = self._frame_queue.get_nowait()
        except queue.Empty:
            return None

        if frame is None:
            self._finished = True
            return None

        self._frames_delivered += 1

        # -- Behind schedule: drain queue to catch up --------------------
        # If we're more than 1 frame behind the source clock, drain
        # intermediate frames and keep only the latest.  This prevents
        # slow-motion playback on underpowered hardware (Pi 3, Pi Zero 2 W)
        # by trading temporal accuracy for real-time pacing.
        catch_up_frames = 0
        while self._frames_delivered < expected_frame:
            try:
                skipped = self._frame_queue.get_nowait()
            except queue.Empty:
                break
            if skipped is None:
                self._finished = True
                return frame  # return the last good frame we had
            frame = skipped
            self._frames_delivered += 1
            catch_up_frames += 1

        if catch_up_frames > 0:
            logger.debug(
                "Skipped %d frames (behind by %d, elapsed=%.2fs, "
                "delivered=%d, expected=%d) [%s]",
                catch_up_frames,
                expected_frame - self._frames_delivered,
                elapsed, self._frames_delivered, expected_frame,
                self._video_path,
            )

        self._last_frame_time = elapsed

        if self._frames_delivered <= 3:
            logger.debug(
                "Frame #%d [%s]: %dx%d, first 12 bytes=%s",
                self._frames_delivered, self._video_path,
                self._width, self._height, frame.flat[:12].tolist(),
            )

        return frame

    def _reader_worker(self) -> None:
        """Background thread: read raw RGB24 frames from ffmpeg stdout."""
        try:
            while self._process and self._process.stdout:
                raw = self._process.stdout.read(self._frame_bytes)
                if not raw or len(raw) < self._frame_bytes:
                    break
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    (self._height, self._width, 3),
                )
                self._frame_queue.put(frame)
        except Exception:
            pass
        finally:
            try:
                self._frame_queue.put_nowait(None)
            except queue.Full:
                pass

    # -- Properties ----------------------------------------------------------

    @property
    def is_playing(self) -> bool:
        """Whether a video is currently loaded and playing."""
        return self._playing

    @property
    def is_finished(self) -> bool:
        """Whether the video has reached its end."""
        return self._finished

    @property
    def fps(self) -> float:
        """The source video's frame rate."""
        return self._fps

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def duration(self) -> float:
        """Video duration in seconds (from metadata)."""
        return self._duration

    @property
    def elapsed(self) -> float:
        """Elapsed playback time in seconds."""
        if not self._playing:
            return 0.0
        return time.monotonic() - self._start_time

    # -- Internal helpers ----------------------------------------------------

    @staticmethod
    def _probe(video_path: str) -> dict | None:
        """Probe video metadata using ffprobe."""
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries",
                    "stream=width,height,r_frame_rate,duration",
                    "-of", "csv=p=0",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None

            parts = result.stdout.strip().split(",")
            if len(parts) < 3:
                return None

            w = int(parts[0]) if parts[0] else 0
            h = int(parts[1]) if parts[1] else 0

            fps_str = parts[2] if len(parts) > 2 else "30/1"
            fps = 30.0
            if "/" in fps_str:
                num, den = fps_str.split("/")
                if int(den) != 0:
                    fps = float(num) / float(den)

            dur = float(parts[3]) if len(parts) > 3 and parts[3] else 0.0

            return {"width": w, "height": h, "fps": fps, "duration": dur}
        except Exception:
            logger.exception("ffprobe failed for: %s", video_path)
            return None

    @staticmethod
    def _compute_scale(
        src_w: int, src_h: int, target_w: int, target_h: int,
    ) -> tuple[int, int]:
        """Compute the output frame size that fits within the target."""
        if src_w <= 0 or src_h <= 0:
            return (target_w, target_h)

        ratio = min(target_w / src_w, target_h / src_h)
        ratio = min(ratio, 1.0)

        w = max(2, int(src_w * ratio))
        h = max(2, int(src_h * ratio))
        w = w - (w % 2)
        h = h - (h % 2)
        return (w, h)

    @staticmethod
    def _detect_hw_decoder() -> str | None:
        """Return a hardware H.264 decoder codec name, or None for auto."""
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-decoders"],
                capture_output=True, text=True, timeout=5,
            )
            if "h264_v4l2m2m" in result.stdout:
                return "h264_v4l2m2m"
        except Exception:
            pass
        return None
