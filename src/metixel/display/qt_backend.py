# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""PySide6 display backend — the production backend for Raspberry Pi.

Runs under **cage** (a minimal wlroots Wayland compositor) with Qt's ``wayland``
QPA plugin, and embeds **mpv** through the libmpv render API
(:class:`mpv.MpvRenderContext`) rather than the X11-only ``wid`` embedding path.

Design
------
The backend owns exactly two surfaces:

* :class:`~metixel.display.qt_canvas.FrameCanvas` — a single ``QWidget`` that
  paints a :class:`~metixel.framing.layout.RenderPlan` (ambient fill, artwork,
  whitespace, mat, moulding) in one pass.
* :class:`~metixel.display.qt_mpv.MpvRenderWidget` — a ``QOpenGLWidget`` that
  renders mpv's output, used only while a video plays.

Frame composition is therefore **retained mode**: nothing re-derives geometry per
frame, and the matte is painted *over* the video by the same canvas that paints
it over a photo.  That is what removes the old two-texture ping-pong, the
per-frame ``draw_*`` calls, and the GL depth ordering the pi3d backend needed.

Layering note — the one thing that must not be "simplified"
----------------------------------------------------------
mpv renders into the widget's own framebuffer, and the matte is then painted on
top with ``QPainter``.  A second framebuffer plus ``glBlitFramebuffer`` is the
obvious-looking alternative and it **segfaults on the Pi** (blitting between a
depth-attached FBO and the default FBO).  Do not reintroduce it.

Startup ordering — all three are load-bearing
---------------------------------------------
1. ``QApplication`` is constructed **first**, then ``LC_NUMERIC`` is reset to
   ``"C"``.  Qt's constructor resets the locale to the system value, and libmpv's
   ``mpv_create()`` returns NULL under a non-C numeric locale.
2. ``ensure_gl_init()`` runs **before** ``play()``.  Without it mpv deselects the
   video track ("Video: no video") because no render context exists yet.
3. ``opengl_fbo`` uses ``defaultFramebufferObject()``, **not** ``0``.  A
   ``QOpenGLWidget`` renders into a texture-backed FBO; passing 0 draws to a
   framebuffer nobody presents, i.e. a black screen.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from metixel.display.backend import DisplayBackend
from metixel.display.hardware import DisplayPower, WlrOutput
from metixel.display.overlay_element import OverlayElement
from metixel.framing.layout import RenderPlan

logger = logging.getLogger(__name__)

#: Set by :meth:`PySide6Backend.create` once ``QApplication`` exists, so the
#: heartbeat thread (started outside Qt) can post repaints safely if needed.
_QT_READY = False


class PySide6Backend(DisplayBackend):
    """Qt + mpv display backend for the Raspberry Pi.

    Imports of PySide6 and mpv are deferred to :meth:`create` so that importing
    this module never requires Qt.  CI runs with no Qt installed, and the layout
    maths plus the presenter must stay testable there.
    """

    def __init__(self) -> None:
        self._app: Any = None
        self._window: Any = None
        self._canvas: Any = None
        self._mpv_widget: Any = None
        self._running: bool = False
        self._width: int = 1920
        self._height: int = 1200
        self._bg_color: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
        self._rotation: int = 0
        self._refresh_rate: int = 0
        self._fps_limit: int = 30
        self._wlr = WlrOutput()
        self._display_power = DisplayPower(self._wlr)
        self._video_path: Path | None = None

    # -- Properties ----------------------------------------------------------

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def supports_video(self) -> bool:
        return True

    # -- Lifecycle -----------------------------------------------------------

    def create(
        self,
        width: int = 1920,
        height: int = 1080,
        fullscreen: bool = True,
        hide_cursor: bool = True,
        fps_limit: int = 30,
        refresh_rate: int = 0,
        rotation: int = 0,
        **kwargs: Any,
    ) -> None:
        """Build the Qt application and the full-screen surface.

        Construction order is deliberate and documented in the module docstring:
        ``QApplication`` first, then the ``LC_NUMERIC`` fix, then the canvas,
        then the mpv widget.  Reordering any of those produces either a NULL mpv
        handle or a black screen, neither of which is obvious from the symptom.
        """
        import locale

        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication

        from metixel.display.qt_canvas import FrameCanvas
        from metixel.display.qt_mpv import MpvRenderWidget

        self._rotation = rotation
        self._refresh_rate = refresh_rate
        self._fps_limit = fps_limit

        # 1. QApplication FIRST.
        self._app = QApplication.instance() or QApplication([])

        # 2. LC_NUMERIC AFTER app creation — Qt resets it in its constructor, and
        #    a non-C numeric locale makes mpv_create() return NULL.
        locale.setlocale(locale.LC_NUMERIC, "C")

        # Apply the requested display mode through the compositor before the
        # surface is sized, so Qt sees the final geometry on first paint.
        if width > 0 and height > 0:
            self._wlr.set_mode(
                width=width, height=height, refresh_rate=refresh_rate, rotation=rotation
            )

        self._canvas = FrameCanvas()
        self._mpv_widget = MpvRenderWidget()

        from PySide6.QtWidgets import QStackedLayout, QWidget

        # A plain container with a stacked layout, NOT a QStackedWidget: switching
        # pages in a QStackedWidget hides the mpv widget, which tears down its GL
        # context and forces a re-init on the next video (dropping the video
        # track).  Keeping both children alive and only changing which one is
        # raised avoids that entirely.
        container = QWidget()
        layout = QStackedLayout(container)
        layout.setStackingMode(QStackedLayout.StackingMode.StackOne)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._canvas)
        layout.addWidget(self._mpv_widget)

        self._window = container
        container.setWindowTitle("Metixel Photoframe")
        container.setStyleSheet("background-color: black;")
        if hide_cursor:
            QGuiApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)

        # 3. The mpv widget's GL context must exist before any play() call.
        self._mpv_widget.ensure_gl_init()

        if fullscreen:
            container.showFullScreen()
        else:
            container.resize(width or 1280, height or 720)
            container.show()
        self._app.processEvents()

        # Trust the surface Qt actually got, not the requested size: cage may
        # have given us a different mode, and every layout decision downstream
        # (mat geometry included) depends on this being the real size.
        self._width = int(container.width()) or (width or 1920)
        self._height = int(container.height()) or (height or 1200)
        self._running = True

        global _QT_READY
        _QT_READY = True
        logger.info(
            "PySide6Backend created: %dx%d (requested %dx%d, rotation=%d, refresh=%d)",
            self._width,
            self._height,
            width,
            height,
            rotation,
            refresh_rate,
        )

    def destroy(self) -> None:
        self._running = False
        try:
            if self._mpv_widget is not None:
                self._mpv_widget.destroy_mpv()
            if self._window is not None:
                self._window.close()
        except Exception:
            logger.debug("Error during Qt teardown", exc_info=True)
        finally:
            self._window = None
            self._canvas = None
            self._mpv_widget = None
            logger.info("PySide6Backend destroyed")

    def loop_running(self) -> bool:
        """Process one iteration of the Qt event loop.

        Qt owns the loop, so this is a single non-blocking pass rather than a
        ``QApplication.exec()``.  The presenter drives the frame rate; letting Qt
        block here would starve the slideshow timers.
        """
        if not self._running or self._app is None:
            return False
        self._app.processEvents()
        return bool(self._window is not None and self._window.isVisible())

    def swap_buffers(self) -> None:
        """No-op — Qt presents on its own schedule.

        Kept so the presenter's frame loop stays backend-agnostic; the pi3d
        backend needed an explicit present, whereas Qt composites and presents
        from the event loop.
        """

    # -- Frame presentation --------------------------------------------------

    def present(self, plan: RenderPlan, image: Any = None) -> None:
        """Paint *plan* on the canvas, raising it above the mpv surface.

        When a video is playing the canvas is still on top, painting only the
        ring layers over mpv's output — which is how the virtual mat composites
        over live video without a second framebuffer.
        """
        if self._canvas is None or self._window is None:
            return
        # Raise the canvas for the image path; for video the canvas must ALSO be
        # on top (it paints the matte), but its artwork layer is skipped by
        # passing image=None, so mpv's frames show through the middle.
        self._canvas.raise_()
        self._canvas.update_plan(plan, image)
        self._canvas.update()

    # -- Overlay -------------------------------------------------------------

    def present_overlay(self, elements: list[OverlayElement]) -> None:
        """Composite the overlay elements on the canvas.

        The canvas owns overlay compositing because the matte must paint over a
        playing video, and both live in the same widget.  Elements arrive
        already flattened and z-sorted by the overlay manager.
        """
        if self._canvas is None:
            return
        if not elements:
            return
        # Only force a raise when a video is up; otherwise the canvas is already
        # on top and raising every frame would be wasted work.
        if self._video_path is not None:
            self._canvas.raise_()
        self._canvas.update_overlay(elements)
        self._canvas.update()

    # -- Artwork -------------------------------------------------------------

    def load_image(self, path: Path | Any) -> Any:
        """Load an image into a ``QImage`` handle.

        Returns ``None`` on failure rather than raising: one unreadable photo
        must never stop the slideshow, and the presenter treats ``None`` as
        "advance".
        """
        try:
            from PySide6.QtGui import QImage

            if isinstance(path, QImage):
                return path
            if isinstance(path, (Path, str)):
                image = QImage(str(path))
                if image.isNull():
                    logger.debug("QImage could not read %s", path)
                    return None
                return image
            # numpy array (H, W, 3/4) — used by tests and generated frames.
            import numpy as np
            from PySide6.QtGui import QImage as _QImage

            arr = np.ascontiguousarray(path)
            if arr.ndim != 3 or arr.shape[2] not in (3, 4):
                return None
            h, w, channels = arr.shape
            fmt = _QImage.Format.Format_RGB888 if channels == 3 else _QImage.Format.Format_RGBA8888
            return _QImage(arr.data, w, h, channels * w, fmt).copy()
        except Exception:
            logger.debug("Failed to load image: %s", path, exc_info=True)
            return None

    def unload_image(self, handle: Any) -> None:
        """Release a ``QImage`` handle.

        Qt reference-counts implicitly, so dropping the reference is the whole
        operation — the method exists so callers have one symmetric lifecycle
        across backends.
        """

    # -- Video ---------------------------------------------------------------

    def play_video(self, path: Path, plan: RenderPlan) -> bool:
        """Start mpv playback and bring its widget to the front.

        Returns ``False`` when the mpv pipeline is unavailable so the presenter
        advances instead of waiting for frames that will never arrive.
        """
        if self._mpv_widget is None:
            return False
        try:
            self._video_path = Path(path)
            self._window.layout().setCurrentWidget(self._mpv_widget)
            self._mpv_widget.ensure_gl_init()
            self._mpv_widget.play(str(path))
            # Paint the frame's ring layers over the live video by presenting the
            # plan with no artwork.
            self.present(plan, image=None)
            return True
        except Exception:
            logger.warning("mpv failed to play %s", path, exc_info=True)
            return False

    def stop_video(self) -> None:
        """Stop playback and return the canvas to the front.  Idempotent."""
        if self._mpv_widget is None:
            return
        try:
            self._mpv_widget.stop()
        except Exception:
            logger.debug("Error stopping mpv", exc_info=True)
        finally:
            self._video_path = None
            if self._window is not None and self._canvas is not None:
                self._window.layout().setCurrentWidget(self._canvas)

    def pause_video(self, paused: bool = True) -> None:
        """Pause/resume mpv in place, keeping the decoder warm.

        Preferred over SIGSTOP: the process stays schedulable, so resume is
        immediate and no signal plumbing is needed.
        """
        if self._mpv_widget is None:
            return
        try:
            self._mpv_widget.set_paused(paused)
        except Exception:
            logger.debug("Error pausing mpv", exc_info=True)

    def video_playing(self) -> bool:
        if self._mpv_widget is None:
            return False
        try:
            return bool(self._mpv_widget.is_playing())
        except Exception:
            return False

    def video_finished(self) -> bool:
        if self._mpv_widget is None:
            return False
        try:
            return bool(self._mpv_widget.is_finished())
        except Exception:
            return False

    # -- Display Control -----------------------------------------------------

    def set_background(self, color: tuple[float, float, float, float]) -> None:
        self._bg_color = color
        if self._canvas is not None:
            self._canvas.set_background(color)

    def clear(self) -> None:
        if self._canvas is not None:
            self._canvas.clear_plan()

    def display_power(self, on: bool) -> None:
        """Sleep or wake the panel via the tiered hardware fallback chain."""
        try:
            self._display_power.set(on)
        except Exception:
            # Never let a failed DPMS call take down the frame.
            logger.warning("display_power(%s) failed", on, exc_info=True)

    # -- Diagnostics ---------------------------------------------------------

    def connected_output(self) -> str | None:
        try:
            return self._wlr.resolve()
        except Exception:
            return None

    def list_modes(self) -> list[dict[str, Any]]:
        try:
            return self._wlr.list_modes()
        except Exception:
            logger.debug("Could not list display modes", exc_info=True)
            return []

    # -- Qt loop helpers -----------------------------------------------------

    def run_event_loop(self) -> None:
        """Enter Qt's blocking event loop (used by the frontend's ``run()``)."""
        if self._app is not None:
            self._app.exec()

    def quit(self) -> None:
        """Ask Qt to leave the event loop."""
        self._running = False
        if self._app is not None:
            self._app.quit()


def qt_available() -> bool:
    """Whether PySide6 can be imported in this environment."""
    import importlib.util

    try:
        return importlib.util.find_spec("PySide6") is not None
    except (ImportError, ValueError):
        return False


def qt_platform() -> str:
    """Return the effective Qt platform plugin name, for logging and diagnosis.

    ``QT_QPA_PLATFORM`` is pinned to ``wayland`` by the systemd unit, so this
    normally reports ``wayland``.  Reading it back makes a silent fallback to
    ``xcb`` visible in the log instead of only on the panel.
    """
    return os.environ.get("QT_QPA_PLATFORM", "") or "(auto)"
