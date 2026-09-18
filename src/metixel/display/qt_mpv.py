# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""mpv render widget — libmpv OpenGL render API inside a ``QOpenGLWidget``.

Embeds mpv using **python-mpv's** :class:`mpv.MpvRenderContext`, the libmpv
render API, rather than ``wid`` embedding.  ``wid`` hands mpv a native window
handle, which is X11-only; under cage (Wayland) the render API is the supported
path.  It also gives Qt ownership of the framebuffer, so the canvas can paint the
matte over mpv's output in the same widget tree.

Five details here are load-bearing and each one fails in a confusing way if lost.
They were established experimentally on a Pi 5 under cage:

1. ``vo="libmpv"`` — required for the render API; ``gpu`` needs its own window.
2. ``opengl_fbo`` must use ``defaultFramebufferObject()``, **not** ``0``.  The
   widget renders into a texture-backed FBO, so 0 draws to a framebuffer that is
   never presented — a black screen with video "playing".
3. ``ensure_gl_init()`` must run **before** ``play()``.  Without an active render
   context mpv drops the video track entirely ("Video: no video").
4. ``report_swap()`` is deferred with ``QTimer.singleShot(0, ...)`` so it runs
   *after* Qt presents the frame rather than immediately after ``render()``.
   Calling it too early breaks frame pacing, most visibly on GPUs with no swap
   control (the Pi 3's VC4).
5. ``locale.setlocale(LC_NUMERIC, "C")`` must be applied **after**
   ``QApplication`` is constructed — see :mod:`metixel.display.qt_backend`.

``osd_level=0`` disables mpv's on-screen text, which would otherwise be baked
into the frame and appear under the canvas's ring layers.

Hardware decoding: `drm-copy`, NOT `v4l2m2m`
--------------------------------------------
Measured on a Pi 5 (idle system, `vo=libmpv` via the render API, 1080p):

    codec   --hwdec        result                        CPU
    HEVC    no             software                      49%
    HEVC    auto           hardware (drm-copy)           13%
    HEVC    drm-copy       hardware (drm-copy)           12%
    HEVC    drm            software (silent fallback)    48%
    HEVC    v4l2m2m        software — "Could not find a valid device"
    H.264   (any)          software                      44-46%

Three conclusions, each of which was the opposite of what the design assumed:

1. **`v4l2m2m` does not work at all on Pi 5** — for *either* codec. The plan's
   premise that H.264/v4l2m2m was the verified-good path does not hold here.
2. **HEVC is the codec with working hardware decode**, via `drm-copy`, which
   drives the `rpi-hevc-dec` kernel device at ``/dev/video19`` with DMABuf in and
   out ("Hwaccel V4L2 HEVC stateless V4"). The ``-copy`` suffix names the interop
   layer, not a software path — do not "optimise" it away to plain ``drm``, which
   **silently** falls back to software.
3. Therefore ``PROFILES["pi5"]`` correctly transcodes to H.265, and switching it
   to H.264 (the obvious reading of "H.264 is the verified path") would have
   removed hardware decode entirely.

The default is ``auto`` so mpv negotiates per codec, which is what it gets right
on both. A Pi 3 does NOT share this answer — VC4 lacks the dmabuf interop that
drm-copy needs — so its value must be measured, not inherited.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

logger = logging.getLogger(__name__)


class MpvRenderWidget(QOpenGLWidget):
    """Renders mpv video frames into a Qt OpenGL widget.

    The widget is a sibling of the frame canvas and sits underneath it.  The
    backend sizes it to the plan's artwork rectangle, and the canvas leaves that
    same rectangle unpainted (``FrameCanvas.set_video_surface``), so the video
    shows through the hole while the canvas paints the ambient and the rings
    around it.

    It therefore paints no frame of its own beyond the video: the ring geometry
    lives in the canvas, and giving it a second owner would let the two disagree.
    """

    #: Emitted from mpv's thread; connected to ``update()`` so the repaint
    #: happens on the GUI thread.
    frame_ready = Signal()

    def __init__(self, parent: QWidget | None = None, *, hwdec: str = "auto") -> None:
        super().__init__(parent)
        self._mpv: Any = None
        self._ctx: Any = None
        self._proc_addr_fn: Any = None
        self._gl_inited = False
        self._hwdec = hwdec
        self._video_size: tuple[int, int] | None = None
        self._paused = False
        self._eof = False
        self._playing = False
        # Readiness for revealing the canvas's artwork hole.  mpv must have BOTH
        # configured the video and rendered a frame; until then the widget's
        # framebuffer is undefined and revealing the hole would show black.  That
        # is measured, not assumed: the PHASE-0 spike's mpv_idle case rendered
        # solid black because paintGL returns early with no render context.
        self._params_known = False
        self._rendered = False

        self.frame_ready.connect(self.update)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    # -- Lifecycle -----------------------------------------------------------

    def _create_mpv(self) -> bool:
        """Create the mpv handle.  Safe to call more than once."""
        if self._mpv is not None:
            return True
        try:
            import mpv as mpvlib
        except ImportError:
            logger.error(
                "python-mpv is not installed — video playback is unavailable. "
                "Install the mpv packages listed in requirements-system.txt."
            )
            return False

        try:
            self._mpv = mpvlib.MPV(
                vo="libmpv",
                hwdec=self._hwdec,
                mute=True,
                loop=False,
                keep_open="no",
                osc=False,
                osd_level=0,  # no on-screen text; the matte paints over frames
                input_default_bindings=False,
                input_vo_keyboard=False,
                loglevel="warn",
                log_handler=self._mpv_log,
            )
        except Exception:
            logger.exception("Could not create the mpv handle")
            self._mpv = None
            return False

        self._mpv.observe_property("eof-reached", self._on_eof_reached)
        self._mpv.observe_property("video-params", self._on_video_params)
        self._mpv.observe_property("pause", self._on_pause_changed)
        return True

    def _mpv_log(self, level: str, component: str, message: str) -> None:
        if level in ("error", "warn"):
            logger.debug("[mpv:%s] %s", component, message.strip())

    # -- GL context ----------------------------------------------------------

    def initializeGL(self) -> None:  # noqa: N802 - Qt naming
        self._init_gl()

    def _init_gl(self) -> None:
        """Create the libmpv render context bound to the current Qt GL context."""
        if self._gl_inited:
            return
        if not self._create_mpv():
            return

        from PySide6.QtGui import QOpenGLContext

        ctx = QOpenGLContext.currentContext()
        if ctx is None:
            logger.debug("No current GL context yet — deferring mpv init")
            return

        import mpv as mpvlib

        def _get_proc_address(_ctx: Any, name: Any) -> int:
            """libmpv's GL loader shim: Qt owns symbol resolution."""
            name_str = name if isinstance(name, bytes) else str(name).encode("utf-8")
            addr = ctx.getProcAddress(name_str)
            return int(addr) if addr is not None else 0

        self._proc_addr_fn = mpvlib.MpvGlGetProcAddressFn(_get_proc_address)
        self._ctx = mpvlib.MpvRenderContext(
            self._mpv,
            "opengl",
            opengl_init_params={"get_proc_address": self._proc_addr_fn},
        )
        self._ctx.update_cb = self._on_mpv_frame
        self._gl_inited = True
        logger.info("mpv render context initialised (hwdec=%s)", self._hwdec)

    def ensure_gl_init(self) -> None:
        """Force the render context into existence before ``play()``.

        Called explicitly because a widget that has not yet been shown has no
        current GL context — and starting playback without one makes mpv discard
        the video track.
        """
        if self._gl_inited:
            return
        self.makeCurrent()
        self._init_gl()
        self.doneCurrent()

    # -- Playback ------------------------------------------------------------

    def play(self, path: str) -> None:
        """Begin playback.  Requires a render context (see ``ensure_gl_init``)."""
        if not self._create_mpv():
            return
        self._eof = False
        self._paused = False
        self._playing = True
        self._params_known = False
        self._rendered = False
        self._mpv.play(path)
        logger.debug("mpv playing %s", path)

    def stop(self) -> None:
        """Stop playback.  Idempotent — called on advance, reset and shutdown.

        Resets ``_eof`` as well as the play flags.  Leaving ``_eof`` set after a
        stop makes the NEXT video appear to be already finished: ``is_finished()``
        returns True immediately, so the presenter calls ``_video_finished()`` on
        its first tick and skips the item entirely.  That reads as "playback locks
        up when a video ends" rather than as a stale flag, because the visible
        symptom is the next slide never playing.
        """
        self._playing = False
        self._paused = False
        self._eof = False
        self._params_known = False
        self._rendered = False
        if self._mpv is None:
            return
        try:
            self._mpv.command("stop")
            # A paint after stop clears the last frame out of the FBO; without it
            # the widget can keep showing the final frame of the video it just
            # stopped until something else forces a repaint.
            self.update()
        except Exception:
            logger.debug("mpv stop failed", exc_info=True)

    def set_paused(self, paused: bool) -> None:
        """Pause or resume without tearing the pipeline down."""
        if self._mpv is None:
            return
        try:
            self._mpv.pause = paused
            self._paused = paused
        except Exception:
            logger.debug("mpv pause=%s failed", paused, exc_info=True)

    def is_playing(self) -> bool:
        return self._playing and not self._paused and not self._eof

    def is_finished(self) -> bool:
        return self._eof

    def destroy_mpv(self) -> None:
        """Release the render context and the mpv handle."""
        try:
            if self._ctx is not None:
                self._ctx.free()
        except Exception:
            logger.debug("Error freeing mpv render context", exc_info=True)
        finally:
            self._ctx = None
            self._gl_inited = False
        try:
            if self._mpv is not None:
                self._mpv.terminate()
        except Exception:
            logger.debug("Error terminating mpv", exc_info=True)
        finally:
            self._mpv = None
            self._playing = False

    # -- mpv callbacks (invoked from mpv's thread) ---------------------------

    def _on_eof_reached(self, _name: str, value: Any) -> None:
        """mpv reached the end of the stream.

        Emits ``frame_ready`` so a repaint is scheduled: without it the widget
        keeps displaying the last decoded frame and nothing runs on the GUI
        thread, so the presenter's poll can be the only thing making progress and
        the surface appears frozen at the end of every video.
        """
        if value:
            self._eof = True
            self._playing = False
            self.frame_ready.emit()

    def _on_pause_changed(self, _name: str, value: Any) -> None:
        self._paused = bool(value)

    def _on_video_params(self, _name: str, value: Any) -> None:
        if isinstance(value, dict) and value.get("w") and value.get("h"):
            self._video_size = (int(value["w"]), int(value["h"]))
            self._params_known = True

    def _on_mpv_frame(self) -> None:
        """mpv has a new frame — hop to the GUI thread to repaint."""
        self.frame_ready.emit()

    # -- Painting ------------------------------------------------------------

    def paintGL(self) -> None:  # noqa: N802 - Qt naming
        if self._ctx is None:
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return

        try:
            # Render into THIS widget's framebuffer, not framebuffer 0.
            self._ctx.render(
                opengl_fbo={
                    "fbo": self.defaultFramebufferObject(),
                    "w": w,
                    "h": h,
                    "internal_format": 0,
                },
                flip_y=True,
            )
        except Exception:
            logger.debug("mpv render failed", exc_info=True)
            return

        # A rendered frame is half of the readiness signal; the other half is mpv
        # having configured the video.  See video_ready.
        self._rendered = True

        # Defer report_swap until after Qt has presented the frame; calling it
        # here would make mpv believe frames are shown earlier than they are and
        # wreck frame pacing on GPUs without swap control.
        QTimer.singleShot(0, self._report_swap)

    def video_ready(self) -> bool:
        """Whether there is a picture to show yet.

        The canvas leaves the artwork rectangle unpainted while a video plays, so
        until this is true, revealing that hole would show the widget's undefined
        framebuffer — black.  Both halves are required: mpv must have configured
        the video AND rendered a frame into this widget.
        """
        return self._params_known and self._rendered

    def set_panscan(self, enabled: bool) -> None:
        """Fill (crop) the widget with the video instead of letterboxing it.

        Maps mpv's ``panscan`` onto the framing engine's ``cover``: the artwork
        rectangle is then the whole panel and the overflowing edges must be
        sampled away, which for a centred crop is exactly what ``panscan = 1.0``
        does.  ``contain`` needs nothing — mpv's default letterboxes inside the
        widget, and the backend has already sized that widget to the contained
        rect, so the two agree without further work.
        """
        if self._mpv is None:
            return
        try:
            self._mpv.panscan = 1.0 if enabled else 0.0
        except Exception:
            logger.debug("mpv panscan=%s failed", enabled, exc_info=True)

    def _report_swap(self) -> None:
        """Tell mpv the frame reached the display."""
        if self._ctx is not None:
            try:
                self._ctx.report_swap()
            except Exception:
                logger.debug("report_swap failed", exc_info=True)
