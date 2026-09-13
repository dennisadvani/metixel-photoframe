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
into the frame and appear behind the matte.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

logger = logging.getLogger(__name__)


class MpvRenderWidget(QOpenGLWidget):
    """Renders mpv video frames into a Qt OpenGL widget.

    The widget is a sibling of the frame canvas and sits underneath it, so the
    canvas can paint the matte ring over the video.
    """

    #: Emitted from mpv's thread; connected to ``update()`` so the repaint
    #: happens on the GUI thread.
    frame_ready = Signal()

    def __init__(self, parent: QWidget | None = None, *, hwdec: str = "v4l2m2m") -> None:
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
        self._mpv.play(path)
        logger.debug("mpv playing %s", path)

    def stop(self) -> None:
        """Stop playback.  Idempotent — called on advance, reset and shutdown."""
        self._playing = False
        self._paused = False
        if self._mpv is None:
            return
        try:
            self._mpv.command("stop")
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
        if value:
            self._eof = True
            self._playing = False

    def _on_pause_changed(self, _name: str, value: Any) -> None:
        self._paused = bool(value)

    def _on_video_params(self, _name: str, value: Any) -> None:
        if isinstance(value, dict) and value.get("w") and value.get("h"):
            self._video_size = (int(value["w"]), int(value["h"]))

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

        # Defer report_swap until after Qt has presented the frame; calling it
        # here would make mpv believe frames are shown earlier than they are and
        # wreck frame pacing on GPUs without swap control.
        QTimer.singleShot(0, self._report_swap)

        # mpv letterboxes within the full widget, so nothing is painted here.
        # The matte ring is drawn by FrameCanvas, which sits above this widget.
        painter = QPainter(self)
        painter.end()

    def _report_swap(self) -> None:
        """Tell mpv the frame reached the display."""
        if self._ctx is not None:
            try:
                self._ctx.report_swap()
            except Exception:
                logger.debug("report_swap failed", exc_info=True)
