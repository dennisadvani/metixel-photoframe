# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""mpv render widget — libmpv SOFTWARE render API inside a ``QOpenGLWidget``.

Embeds mpv using **python-mpv's** :class:`mpv.MpvRenderContext`, the libmpv
render API, rather than ``wid`` embedding.  ``wid`` hands mpv a native window
handle, which is X11-only; under cage (Wayland) the render API is the supported
path.  It also gives Qt ownership of the framebuffer, so the canvas can paint the
matte over mpv's output in the same widget tree.

Why the SOFTWARE render API
---------------------------
The render API's default ``opengl`` type leaks one ``anon_inode:sync_file``
descriptor per ``render()`` call on the Pi's stack — ~28-30/s, which is
``fd 1020 / limit 1024`` after ~27 s of cumulative playback, and the frontend then
dies of ``OSError: [Errno 24] Too many open files``.  The leak is per draw call
and independent of the decoder; a backported upstream libplacebo fix was built,
verified as genuinely recompiled, and measured to leak identically.

``MPV_RENDER_API_TYPE_SW`` never enters that code path: mpv converts the frame
into a CPU buffer we own, and Qt only uploads the finished image.  Measured
clean at four formats and two resolutions with hardware decoding still active.
See :mod:`metixel.display.sw_render` for the measurements and the buffer-cap
cost model, and ``sw_render_max_pixels_for_model`` for the per-board cap.

The cost is real and it is the price of correctness: ~80% of one Pi 5 core at a
1 Mpx buffer and 30 fps, against ~12% for the GL path.  That is why the buffer is
capped and the final upscale is left to the GPU rather than done in CPU.

Details that are load-bearing, each established experimentally on a Pi 5 under
cage and each failing in a confusing way if lost:

1. ``vo="libmpv"`` — required for the render API; ``gpu`` needs its own window.
2. ``api_type="sw"`` with **no** GL init parameters.  The software renderer needs
   no ``get_proc_address`` shim and no current GL context; mpv writes into our
   CPU buffer and Qt does the upload.
3. ``ensure_render_context()`` must run **before** ``play()``.  Without an active
   render context mpv drops the video track entirely ("Video: no video").
4. ``report_swap()`` is deferred with ``QTimer.singleShot(0, ...)`` so it runs
   *after* Qt presents the frame rather than immediately after ``render()``.
   Calling it too early breaks frame pacing, most visibly on GPUs with no swap
   control (the Pi 3's VC4).
5. ``locale.setlocale(LC_NUMERIC, "C")`` must be applied **after**
   ``QApplication`` is constructed — see :mod:`metixel.display.qt_backend`.
6. The ``QImage`` must **view** the buffer, not copy it.  A copying constructor
   would leave every frame showing the first one, with no error to explain it.
   (Verified on the Pi: writing the bytearray changes ``pixelColor()``.)
7. A **fresh ``QImage`` per frame** — see :meth:`_frame_image`.  Reusing one hands
   Qt's GL texture cache an unchanging ``cacheKey()`` for a buffer whose contents
   change, so it redraws the first (empty, therefore black) texture for ever.  The
   only symptom is a black video rectangle: no error, no warning, an active
   painter, a full buffer, and other drawing into the same widget working fine.
   It was found by drawing a magenta square beside the image — the square
   appeared, the image did not.

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

import ctypes
import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

from metixel.display.sw_render import (
    DEFAULT_SW_FORMAT,
    build_sw_params,
    bytes_per_pixel,
    install_sw_render_params,
    sw_target_size,
)
from metixel.shared.platform import detect_pi_model, sw_render_max_pixels_for_model

logger = logging.getLogger(__name__)


class MpvRenderWidget(QOpenGLWidget):
    """Renders mpv video frames into a Qt OpenGL widget via the software API.

    mpv converts each frame into a CPU buffer sized to the widget (capped by the
    board's pixel budget); ``QPainter`` then draws that buffer over the widget,
    with the GL paint engine doing the upload and the upscale.

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

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hwdec: str = "auto",
        sw_max_pixels: int | None = None,
        sw_format: str = DEFAULT_SW_FORMAT,
    ) -> None:
        super().__init__(parent)
        self._mpv: Any = None
        self._ctx: Any = None
        self._context_ready = False
        self._hwdec = hwdec
        # Buffer cap, in pixels.  The backend owns the board-derived decisions and
        # passes this in; deriving it here as well keeps a directly-constructed
        # widget sane on a Pi instead of silently using a desktop-sized buffer.
        self._sw_max_pixels = (
            sw_max_pixels
            if sw_max_pixels is not None
            else sw_render_max_pixels_for_model(detect_pi_model())
        )
        self._sw_format = sw_format
        # The software render target, and the QImage that views it.  Held as
        # instance state so a frame costs no allocation; the IMAGE is built fresh
        # per frame (see _frame_image) and is deliberately not cached here.
        self._pixels: bytearray | None = None
        self._backing: Any = None
        self._buffer_address = 0
        self._buffer_size: tuple[int, int] = (0, 0)
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
        self._init_render_context()

    def _init_render_context(self) -> None:
        """Create the libmpv render context for the SOFTWARE render API.

        ``api_type="sw"`` is the whole point of this module: it keeps mpv off the
        GL fence path that leaks one ``sync_file`` descriptor per draw call.  It
        needs no ``get_proc_address`` shim and no current GL context, because mpv
        converts into our own CPU buffer and Qt only uploads the finished image.
        """
        if self._context_ready:
            return
        if not self._create_mpv():
            return

        import mpv as mpvlib

        # python-mpv 1.0.7 — the version on the Pi — does not know the four sw_*
        # parameters, and raises ValueError before ever reaching libmpv.  This is
        # what makes render() reachable.
        install_sw_render_params(mpvlib)
        self._ctx = mpvlib.MpvRenderContext(self._mpv, "sw")
        self._ctx.update_cb = self._on_mpv_frame
        self._context_ready = True
        logger.info(
            "mpv render context initialised (api_type=sw, hwdec=%s, cap=%s px, format=%s)",
            self._hwdec,
            self._sw_max_pixels if self._sw_max_pixels > 0 else "none",
            self._sw_format,
        )

    def ensure_render_context(self) -> None:
        """Force the render context into existence before ``play()``.

        Starting playback without one makes mpv discard the video track
        ("Video: no video").  ``makeCurrent()`` is no longer needed by the render
        API itself — it is kept so the widget's GL context exists before the
        first paint, which the upload needs.
        """
        if self._context_ready:
            return
        self.makeCurrent()
        self._init_render_context()
        self.doneCurrent()

    # -- Playback ------------------------------------------------------------

    def play(self, path: str) -> None:
        """Begin playback.  Requires a render context (see ``ensure_render_context``)."""
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
            self._context_ready = False
            self._release_frame()
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

        # Convert into our own CPU buffer, capped by the board's pixel budget.
        # Capping is the only cost lever that measurably matters: the price is
        # mpv's scale-and-convert step, and rendering at the full artwork
        # rectangle cannot sustain 30 fps (110% of a Pi 5 core, 27 fps ceiling).
        buf_w, buf_h = sw_target_size(w, h, self._sw_max_pixels)
        if not self._ensure_frame_buffer(buf_w, buf_h):
            return

        try:
            self._ctx.render(**build_sw_params(buf_w, buf_h, self._buffer_address, self._sw_format))
        except Exception:
            logger.debug("mpv software render failed", exc_info=True)
            return

        # A rendered frame is half of the readiness signal; the other half is mpv
        # having configured the video.  See video_ready.
        self._rendered = True

        # Upload and upscale on the GPU.  QPainter's GL paint engine draws the
        # image as a textured quad, so the scale from the capped buffer to the
        # widget is linear-filtered by the GPU — which is the entire reason for
        # capping rather than paying for a full-resolution conversion in CPU.
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(self.rect(), self._frame_image(buf_w, buf_h))
        finally:
            painter.end()

        # Defer report_swap until after Qt has presented the frame; calling it
        # here would make mpv believe frames are shown earlier than they are and
        # wreck frame pacing on GPUs without swap control.
        QTimer.singleShot(0, self._report_swap)

    # -- Software render target ----------------------------------------------

    def _ensure_frame_buffer(self, width: int, height: int) -> bool:
        """Allocate or reuse the CPU render target for *width* x *height*.

        Allocated once per size and reused for every frame: the software path's
        cost is mpv's conversion, so a per-frame allocation would add a
        multi-megabyte copy on top of it.

        A ``bytearray`` is used rather than :func:`ctypes.create_string_buffer`
        because :class:`QImage` needs a writable Python buffer, while mpv needs a
        raw address.  ``from_buffer`` gives both views of the SAME memory, so
        there is one allocation and no copy between them.

        Note what is NOT cached here: no :class:`QImage`.  See
        :meth:`_frame_image` for why that would be a bug rather than an
        optimisation.
        """
        if self._buffer_size == (width, height) and self._pixels is not None:
            return True

        self._release_frame()
        try:
            self._pixels = bytearray(width * height * bytes_per_pixel(self._sw_format))
            self._backing = (ctypes.c_char * len(self._pixels)).from_buffer(self._pixels)
            self._buffer_address = ctypes.addressof(self._backing)
        except Exception:
            logger.warning("could not allocate the software render buffer", exc_info=True)
            self._release_frame()
            return False
        self._buffer_size = (width, height)
        return True

    def _frame_image(self, width: int, height: int) -> QImage:
        """Return a NEW :class:`QImage` viewing the frame buffer just rendered.

        **A fresh QImage is required for every frame, and this is not an
        optimisation opportunity.**  Qt's GL paint engine caches the uploaded
        texture keyed by ``QImage.cacheKey()``, and for an image built over a raw
        buffer that key changes only when the QImage *object* changes — writing
        the buffer does not change it.  Reusing one QImage therefore hands Qt the
        same key for ever, and it keeps redrawing the first texture it uploaded.

        That first texture is black, because on the very first paint the buffer
        is still empty (mpv has not produced a frame yet).  The symptom is
        maddening: a black video rectangle while every other signal is healthy —
        ``paintGL`` runs at 30 Hz, the buffer is full of real pixels, the
        ``QPainter`` is active, nothing is logged, and other drawing into the same
        widget (a plain ``fillRect``) renders fine.  It was diagnosed on the Pi by
        painting a magenta square alongside the image: the square appeared, the
        image did not.

        The QImage *views* the buffer rather than copying it, so this costs an
        allocation of the image header, not of the pixels.
        """
        fmt = self._sw_format
        image_format = (
            QImage.Format.Format_RGB16 if fmt == "rgb565" else QImage.Format.Format_RGBX8888
        )
        assert self._pixels is not None  # noqa: S101 - guarded by _ensure_frame_buffer
        return QImage(
            self._pixels,
            width,
            height,
            width * bytes_per_pixel(fmt),
            image_format,
        )

    def _release_frame(self) -> None:
        """Drop the render target so its memory returns to the system.

        Only called on shutdown.  The buffer is bounded by the board's pixel cap
        (about 4 MB on a Pi 5), and it must NOT be freed on ``stop()``: a stopped
        mpv still gets a paint to clear the surface, which would immediately
        reallocate it.
        """
        self._pixels = None
        self._backing = None
        self._buffer_address = 0
        self._buffer_size = (0, 0)

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
