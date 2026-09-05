# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""VLC subprocess video player for Metixel Photoframe.

Launches the ``vlc`` CLI as a separate process so it does not contend
with pi3d for the GPU/GLES context (in-process libVLC fails for that
reason).  The caller polls the returned ``Popen`` and queries playback
status via VLC's RC TCP interface.  Recommended for Phase 1 (Pi).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

logger = logging.getLogger(__name__)


class VlcVideoPlayer:
    """Play videos by spawning the ``vlc`` CLI as a subprocess.

    A separate VLC process gets its own GPU context (or X11 software
    rendering) and renders via XWayland under cage — the same model used
    by the PicFrame project.  ``h264_v4l2m2m`` hardware decoding is
    available on Raspberry Pi.  Playback status is queried over VLC's RC
    TCP interface rather than by polling an in-process player.

    Usage::

        player = VlcVideoPlayer()
        proc = player.play("/path/to/video.mp4", block=False)
        while proc is not None and proc.poll() is None:
            if player.is_playing:
                # ... render loop / state machine ...
                pass
            time.sleep(0.05)
        player.stop()

    On Raspberry Pi, install::

        sudo apt install -y vlc
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        self._playing: bool = False
        self._finished: bool = False
        self._video_path: str = ""
        self._duration: float = 0.0
        self._start_time: float = 0.0
        self._screen_w: int = 1920
        self._screen_h: int = 1080
        self._hw_codecs: list[str] = []

        # RC interface for querying VLC playback status via TCP.
        # Port 0 means "not configured" — VLC hasn't been launched yet.
        self._rc_port: int = 0

    def play(
        self,
        video_path: str,
        *,
        screen_w: int = 1920,
        screen_h: int = 1080,
        block: bool = True,
        loop: bool = False,
        fit_mode: str = "contain",
    ) -> int | subprocess.Popen[bytes] | None:
        """Start video playback via the VLC CLI subprocess.

        Args:
            video_path: Path to the video file.
            screen_w: Screen width in pixels (for crop/fill fit modes).
            screen_h: Screen height in pixels (for crop/fill fit modes).
            block: If True, block until VLC exits and return its exit code.
                   If False, start playback and return immediately with the
                   ``Popen`` handle — the caller polls it and calls
                   :meth:`stop`.
            loop: Unused (VLC is launched with ``--play-and-exit``).
            fit_mode: How to fit the video to the screen.
                      ``"contain"`` (default) — letterbox/pillarbox,
                      ``"cover"`` — crop to fill,
                      ``"fill"`` — stretch to fill (distorts AR).

        Returns:
            Exit code (0 = success) when ``block=True``, a ``Popen``
            handle when ``block=False``, or ``None`` if VLC is
            unavailable or playback could not be started.
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
                video_path,
                block=block,
                fit_mode=fit_mode,
            )
        except Exception:
            logger.exception("VLC playback failed: %s", video_path)
            self._teardown()
            return None

    def stop(self) -> None:
        """Stop playback and release VLC resources."""
        self._playing = False
        self._finished = True
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
        import socket as _socket

        # Find the VLC binary
        vlc_bin = shutil.which("vlc")
        if not vlc_bin:
            logger.error("VLC binary not found on PATH")
            return None

        # Probe HW codecs for logging only
        self._detect_best_codec()

        display_ratio = self._compute_crop_ratio(
            self._screen_w,
            self._screen_h,
        )

        # Pick a free TCP port for VLC's RC interface.
        # VLC 3.x LUA CLI uses --rc-host (TCP), not --rc-unix (Unix).
        # Bind port 0 → OS assigns a free port → close → pass to VLC.
        _tmp = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _tmp.bind(("127.0.0.1", 0))
        self._rc_port = _tmp.getsockname()[1]
        _tmp.close()

        cmd = [
            vlc_bin,
            "--no-audio",
            "--play-and-exit",
            "--no-video-title-show",
            "--intf",
            "dummy",  # No interactive interface
            "--extraintf",
            "rc",  # LUA CLI for status queries
            "--rc-host",
            f"localhost:{self._rc_port}",
            "--rc-fake-tty",  # No TTY needed
            # In portrait only, size VLC's window to the rotated canvas.
            # In landscape the video native size already equals the canvas
            # (e.g. 1920x1080), so VLC's window maps at the right size with
            # no post-map resize — exactly the 1.2.4 behaviour.  In portrait
            # the video (e.g. 1080x1920) is smaller than the rotated root
            # (1200x1920), so we set the window size up front.  We must NOT
            # use --fullscreen: that maps at the video native size first then
            # asks the WM to stretch — that post-map resize flashes black.
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

        if self._screen_h > self._screen_w:
            # Portrait: constrain the window to the rotated canvas so it
            # maps already at the final size (no resize flash).
            cmd.insert(1, "--video-y=0")
            cmd.insert(1, "--video-x=0")
            cmd.insert(1, f"--height={self._screen_h}")
            cmd.insert(1, f"--width={self._screen_w}")

        logger.debug(
            "VlcVideoPlayer (subprocess): %s (hw_codecs=%s, fit=%s)",
            video_path,
            ", ".join(self._hw_codecs) if self._hw_codecs else "auto",
            fit_mode,
        )
        logger.debug("VLC command: %s", " ".join(cmd))

        self._finished = False
        self._start_time = time.monotonic()
        # _playing is NOT preset to True — the is_playing property
        # queries VLC's RC Unix socket for real playback status.
        # VLC may take 1-3 seconds to create the socket and start
        # rendering, which is handled by the engine's WAITING state.

        try:
            env = os.environ.copy()
            # Launch VLC with the same inherited environment in every
            # orientation — identical to the 1.2.4 landscape behaviour.
            # We do NOT strip $WAYLAND_DISPLAY: doing so forces VLC onto
            # the X11 vout, which pops a blank window over the poster
            # before the first frame (the flash).  Keeping the env
            # untouched lets VLC use the native Wayland path, the same
            # one landscape uses and the same one that runs cleanly
            # when VLC is launched manually.
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
                    rc,
                    self._duration,
                    video_path,
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

        In the subprocess model the caller owns the ``Popen`` returned
        from :meth:`play` and polls it directly; there is no in-process
        player to query here.  Returns ``None`` when no playback is
        active.
        """
        return None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_playing(self) -> bool:
        """Whether VLC is actually rendering frames.

        Queries VLC's RC TCP interface for the canonical playback
        status.  Returns ``False`` if VLC hasn't started its RC
        listener yet, the query fails, or VLC reports it's not playing.
        """
        if not self._rc_port:
            return False
        try:
            return self._query_rc("is_playing") == "1"
        except (OSError, TimeoutError, ValueError):
            return False

    def _query_rc(self, command: str, timeout: float = 0.5) -> str:
        """Send a command to VLC's RC TCP interface and return the response.

        VLC 3.x LUA CLI listens on a TCP port (``--rc-host``).  The
        protocol is line-based: connect, read the welcome banner
        (discard lines until the ``> `` prompt), send the command, and
        read the first non-empty response line.

        Args:
            command: RC command to send (e.g. ``"is_playing"``).
            timeout: Socket timeout in seconds.

        Returns:
            The first line of the response, stripped of whitespace.

        Raises:
            OSError: If VLC isn't listening on the expected port.
            TimeoutError: If VLC doesn't respond within *timeout*.
            ValueError: If the response is unexpected.
        """
        import socket as _socket

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(("127.0.0.1", self._rc_port))
            # ── Consume welcome banner until prompt ──────────────────
            banner = b""
            while b"> " not in banner:
                chunk = sock.recv(256)
                if not chunk:
                    raise OSError("VLC closed RC connection during banner")
                banner += chunk
            # ── Send command ─────────────────────────────────────────
            sock.sendall((command + "\n").encode())
            # ── Read response ────────────────────────────────────────
            response = b""
            while True:
                try:
                    sock.settimeout(0.3)
                    chunk = sock.recv(256)
                    if not chunk:
                        break
                    response += chunk
                    if b"\n" in response:
                        break
                except TimeoutError:
                    break
        finally:
            sock.close()

        # Parse: first non-empty, non-prompt line
        for line in response.decode(errors="replace").splitlines():
            line = line.strip()
            if line and line != ">" and not line.startswith(">"):
                return line
        raise ValueError(f"Empty RC response for '{command}'")

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
    # Internal: cleanup
    # ------------------------------------------------------------------

    def _teardown(self) -> None:
        """Release VLC resources.

        In the subprocess model there is no in-process VLC/SDL2 state to
        release — the caller owns the ``Popen`` and terminates it.  We
        only reset the RC port so a subsequent :meth:`play` allocates a
        fresh one.
        """
        self._rc_port = 0

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
                capture_output=True,
                text=True,
                timeout=5,
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
                    "HW codecs available: %s",
                    ", ".join(self._hw_codecs),
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
        """Check if the ``vlc`` binary is on PATH (the subprocess player)."""
        import shutil

        return shutil.which("vlc") is not None

    @staticmethod
    def _sdl2_available() -> bool:
        """Check if pysdl2 is importable (kept for API compatibility)."""
        try:
            import sdl2  # type: ignore # noqa: F401

            return True
        except ImportError:
            return False

    @staticmethod
    def is_available() -> bool:
        """Check if the VLC CLI subprocess player is available."""
        return VlcVideoPlayer._vlc_available()
