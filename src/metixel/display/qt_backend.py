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
  renders mpv's output through the **software** render API, used only while a
  video plays.

Frame composition is therefore **retained mode**: nothing re-derives geometry per
frame, and the matte is painted *over* the video by the same canvas that paints
it over a photo.  That is what removes the old two-texture ping-pong, the
per-frame ``draw_*`` calls, and the GL depth ordering the pi3d backend needed.

Layering note — the one thing that must not be "simplified"
----------------------------------------------------------
The ring layers are painted over the video by the canvas ABOVE, which leaves
exactly the plan's artwork rectangle unpainted so the video shows through that
hole.  A second framebuffer plus ``glBlitFramebuffer`` is the obvious-looking
alternative and it **segfaults on the Pi** (blitting between a depth-attached FBO
and the default FBO).  Do not reintroduce it.

The hole rests on one Qt detail: while the canvas leaves a region unpainted it
must NOT claim ``WA_OpaquePaintEvent``, or that region shows undefined
framebuffer content — black.  ``FrameCanvas.set_video_surface`` owns that.

Startup ordering — both are load-bearing
----------------------------------------
1. ``QApplication`` is constructed **first**, then ``LC_NUMERIC`` is reset to
   ``"C"``.  Qt's constructor resets the locale to the system value, and libmpv's
   ``mpv_create()`` returns NULL under a non-C numeric locale.
2. ``ensure_render_context()`` runs **before** ``play()``.  Without it mpv
   deselects the video track ("Video: no video") because no render context
   exists yet.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from metixel.display.backend import DisplayBackend
from metixel.display.geometry import int_rect
from metixel.display.hardware import DisplayPower, WlrOutput
from metixel.display.overlay_element import OverlayElement
from metixel.framing.layout import RenderPlan
from metixel.shared.platform import (
    detect_pi_model,
    hwdec_for_model,
    sw_render_max_pixels_for_model,
)

logger = logging.getLogger(__name__)


def _artwork_rect(plan: RenderPlan) -> tuple[int, int, int, int]:
    """The plan's artwork rectangle, rounded exactly as the canvas rounds it.

    The mpv widget is a SIBLING of the canvas, not a child, so this rect and the
    hole the canvas leaves unpainted have to agree to the pixel; a one-pixel
    disagreement is a black seam down the edge of the video.  Both therefore go
    through :func:`metixel.display.geometry.int_rect`.
    """
    return int_rect(plan.artwork_dst)


#: Frames per second when ``display.fps_limit`` is missing or non-positive.
#: Matches the config default.
DEFAULT_FPS_LIMIT = 30

#: Set by :meth:`PySide6Backend.create` once ``QApplication`` exists, so the
#: heartbeat thread (started outside Qt) can post repaints safely if needed.
_QT_READY = False


def tick_interval_ms(fps_limit: int | None) -> int:
    """Return the render timer's interval in milliseconds for *fps_limit*.

    Separate from :meth:`PySide6Backend.schedule` so the arithmetic is testable
    without Qt, because the trap here is not obvious: ``QTimer.setInterval(0)``
    does **not** mean "no timer" or "as fast as needed" — it means "fire as soon
    as the event loop can drain", i.e. unbounded.  Measured on a Pi 5 that was
    56 fps against a configured 30, so the frame budget was nearly doubled for
    free.  The interval is therefore never below 1 ms, and a missing or
    non-positive limit falls back to :data:`DEFAULT_FPS_LIMIT` rather than to
    unbounded rendering.

    The cost of honouring the limit is up to one interval of input/IPC latency:
    33 ms at 30 fps, which is imperceptible.
    """
    try:
        fps = int(fps_limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # Absent, or a hand-edited config.json holding a non-number.
        fps = DEFAULT_FPS_LIMIT
    if fps <= 0:
        # A negative rate is nonsense; treat it as "unset" rather than clamping to
        # 1 fps, which would render one frame a second and look like a hang.
        fps = DEFAULT_FPS_LIMIT
    return max(1, round(1000 / fps))


def _make_resize_filter(callback: Any) -> Any:
    """Build a QObject that invokes ``callback`` on its parent's resize.

    ``installEventFilter`` requires a ``QObject``, and ``PySide6Backend`` is a
    plain class (it derives from ``DisplayBackend``, an ABC).  Passing the backend
    directly raised::

        TypeError: installEventFilter called with wrong argument types
          QObject.installEventFilter(PySide6Backend)

    which escaped from ``create()`` as a startup failure and crash-looped the
    service 39 times.  This factory is defined lazily because ``QObject`` only
    exists once Qt is importable — CI has no Qt and must still import this module.
    """
    from PySide6.QtCore import QEvent, QObject

    class _ResizeFilter(QObject):
        def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802
            if event.type() == QEvent.Type.Resize:
                try:
                    callback()
                except Exception:
                    logger.debug("relayout on resize failed", exc_info=True)
            return False

    return _ResizeFilter()


class PySide6Backend(DisplayBackend):
    """Qt + mpv display backend for the Raspberry Pi.

    Imports of PySide6 and mpv are deferred to :meth:`create` so that importing
    this module never requires Qt.  CI runs with no Qt installed, and the layout
    maths plus the presenter must stay testable there.
    """

    def __init__(self) -> None:
        self._app: Any = None
        self._window: Any = None
        self._container: Any = None
        self._resize_filter: Any = None
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
        # The artwork rect the mpv widget currently occupies, or ``None`` when no
        # video is playing.  Kept so _relayout() does not stretch the video back
        # over the whole container on a resize.
        self._video_geometry: tuple[int, int, int, int] | None = None
        # The plan the geometry was last applied from, so re-applying it on every
        # present() is free.  See _apply_video_geometry.
        self._video_plan: RenderPlan | None = None
        # Whether the artwork hole has been opened for the video that is playing.
        # The hole is NOT opened by play_video(): doing that revealed the widget's
        # unpainted buffer — a black rectangle — until mpv produced its first
        # frame.  It opens on the first present() after mpv reports a frame, so
        # the still poster covers the gap.  See _reveal_video_surface_when_ready.
        self._video_revealed: bool = False
        self._timer: Any = None

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
        # The hwdec value is derived from the BOARD, not left to mpv's `auto`.
        # Measured on hardware: `auto` never reaches the working decoder on a
        # Pi 3 (it exhausts CUDA/Vulkan/drm first and lands on software at 170%
        # CPU, worse than requesting none). See shared/platform.hwdec_for_model.
        model = detect_pi_model()
        hwdec = hwdec_for_model(model)
        # The software render API's price is CPU spent in mpv's scale-and-convert
        # step, and the buffer cap is the only lever that bounds it — so it is a
        # board decision, taken here alongside the decoder rather than left to
        # the widget to guess.  See platform.SWRenderPixelsByModel.
        sw_max_pixels = sw_render_max_pixels_for_model(model)
        logger.info("mpv video path: hwdec=%s, software render cap=%s px", hwdec, sw_max_pixels)
        self._mpv_widget = MpvRenderWidget(hwdec=hwdec, sw_max_pixels=sw_max_pixels)

        from PySide6.QtWidgets import QWidget

        # Two surfaces must OVERLAY, not tile.  This is the subtlety that took
        # three attempts to get right:
        #
        # A QVBoxLayout *allocates* space to each visible child, so two visible
        # widgets end up stacked vertically at half height each — which is
        # exactly what "the slideshow is drawn halfway down the screen" was.  The
        # canvas then occupied only the bottom half, so its artwork "hole" was
        # nowhere near the video and the matte read as solid black.
        #
        # The prototype never overlays them either: it shows exactly ONE widget at
        # a time (hide() the other, which frees its layout space) and raise_()s the
        # visible one.  But metixel needs BOTH visible at once, because the canvas
        # paints the overlay (clock, messages) above a playing video.
        #
        # So: no layout manager at all.  Both widgets are children of the
        # container and are given the full geometry explicitly by _relayout(),
        # with raise_() deciding z-order.  Manual geometry is the correct tool for
        # a deliberate overlay; a layout manager is the wrong tool for it.
        container = QWidget()
        self._mpv_widget.setParent(container)
        self._canvas.setParent(container)
        self._canvas.raise_()
        self._container = container

        # Keep both children exactly container-sized, on every resize.  The filter
        # must be a QObject, so it comes from a factory rather than being `self`.
        self._resize_filter = _make_resize_filter(self._on_container_resized)
        container.installEventFilter(self._resize_filter)

        self._window = container
        container.setWindowTitle("Metixel Photoframe")
        container.setStyleSheet("background-color: black;")
        if hide_cursor:
            QGuiApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)

        # 3. The mpv widget's render context must exist before any play() call.
        self._mpv_widget.ensure_render_context()

        if fullscreen:
            container.showFullScreen()
        else:
            container.resize(width or 1280, height or 720)
            container.show()

        # A Wayland surface is NOT necessarily at its final size when show()
        # returns: the compositor configures it asynchronously, so an immediate
        # read can return a placeholder (observed on a Pi 5 as "200x100").  Pump
        # the event loop until the size settles, rather than sampling once and
        # trusting it — every layout decision downstream, mat geometry included,
        # depends on this being the real size.
        #
        # The settle loop is bounded, and the value is also re-read whenever the
        # canvas reports a resize (see ``_on_surface_resized``), so a slow
        # compositor cannot latch a wrong size for the whole session.
        settled = self._wait_for_stable_size(container)
        if not settled:
            logger.warning(
                "Display surface size did not stabilise within the timeout "
                "(last seen %dx%d) — using it, but the reported resolution may "
                "be wrong until the next resize",
                container.width(),
                container.height(),
            )

        self._width, self._height = self._read_surface_size(container)
        # Both children must fill the container exactly; there is no layout
        # manager to do it, so do it explicitly now and on every resize.
        self._relayout(container)
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

    # -- Surface size --------------------------------------------------------

    def _relayout(self, container: Any = None) -> None:
        """Give both children the full container geometry.

        There is deliberately no layout manager.  A QVBoxLayout would tile the two
        widgets vertically (half height each), which is what made the slideshow
        render halfway down the screen and left the canvas's artwork hole
        somewhere other than over the video.  A QStackedLayout would make them
        mutually exclusive pages.  We need them to OVERLAY, so the geometry is set
        by hand and raise_() decides which is on top.

        The mpv widget is set first and the canvas second so the canvas stays the
        upper sibling for photos; z-order after that is owned by whoever changes
        the surface mode — FrameCanvas.set_video_surface() and play_video().
        """
        target = container if container is not None else self._container
        if target is None:
            return
        rect = target.rect()
        for widget in (self._mpv_widget, self._canvas):
            if widget is not None:
                widget.setGeometry(rect)
        # A playing video owns only the artwork rect, so a resize must not stretch
        # the mpv surface back over the whole container.  The presenter repaints
        # with a freshly computed plan, which re-applies the geometry for the new
        # size (see _apply_video_geometry) — so a stale value here lasts at most
        # one frame.
        if self._mpv_widget is not None and self._video_geometry is not None:
            self._mpv_widget.setGeometry(*self._video_geometry)

    def _read_surface_size(self, container: Any) -> tuple[int, int]:
        """Return the surface size, preferring the launcher's authoritative value.

        The compositor configures a Wayland surface asynchronously, so a read
        taken here can still be a placeholder.  ``cage_launch.sh`` has already
        queried the compositor's output geometry via ``wlr-randr``, so its value
        wins when present; otherwise fall back to the container, then to sensible
        defaults.
        """
        launch = self._launch_surface_size()
        if launch is not None:
            return launch
        w = int(container.width())
        h = int(container.height())
        # A zero or absurdly small size means the compositor has not configured
        # the surface yet; treat it as "unknown" rather than accepting it.
        if w < 100 or h < 100:
            return (w or 1920, h or 1200)
        return (w, h)

    def _wait_for_stable_size(self, container: Any, *, attempts: int = 20) -> bool:
        """Pump the event loop until the surface size settles.

        Returns True if the size settled, False if the attempt budget ran out.
        A Wayland surface is configured asynchronously, so the first read after
        ``show()`` can be a placeholder (observed as "200x100" and "640x480").

        When the launcher has told us the real output size (see
        :meth:`_launch_surface_size`), settling simply means the compositor has
        agreed with it.  Otherwise fall back to two consecutive identical reads,
        which is weak but only used on a system with no launcher to ask.
        """
        target = self._launch_surface_size()
        last: tuple[int, int] | None = None
        for _ in range(attempts):
            self._app.processEvents()
            current = (int(container.width()), int(container.height()))
            if target is not None:
                if current == target:
                    return True
            elif current == last and current[0] >= 100 and current[1] >= 100:
                return True
            last = current
        return False

    @staticmethod
    def _launch_surface_size() -> tuple[int, int] | None:
        """The output size the launcher resolved, or ``None`` if not provided.

        ``scripts/cage_launch.sh`` reads the compositor's real output geometry
        with ``wlr-randr`` — after disabling phantom outputs but *before* the
        frontend starts — and exports it as ``METIXEL_LAUNCH_WIDTH`` /
        ``METIXEL_LAUNCH_HEIGHT``.

        That is the authoritative answer, and it is why this class does not try
        to *infer* the size: the compositor configures a Wayland surface
        asynchronously, so Qt can legitimately observe a placeholder, whereas the
        launcher has already asked the compositor directly.  Preferring the
        launcher's value removes the guesswork (and the intermittent low-res
        top-left rendering it caused).
        """
        try:
            width = int(os.environ.get("METIXEL_LAUNCH_WIDTH", "") or 0)
            height = int(os.environ.get("METIXEL_LAUNCH_HEIGHT", "") or 0)
        except ValueError:
            logger.warning("Ignoring malformed METIXEL_LAUNCH_* size")
            return None
        if width < 100 or height < 100:
            return None
        return (width, height)

    def _on_container_resized(self) -> None:
        """Container resized: give both children the new full geometry."""
        if self._container is None:
            return
        self._relayout(self._container)
        width = int(self._container.width())
        height = int(self._container.height())
        if width < 100 or height < 100:
            return
        if (width, height) != (self._width, self._height):
            logger.info(
                "Display surface resized: %dx%d → %dx%d",
                self._width,
                self._height,
                width,
                height,
            )
            self._width, self._height = width, height
            self._publish_display_info()

    def _on_surface_resized(self, width: int, height: int) -> None:
        """Handle the canvas reporting a new surface size.

        Called from the GUI thread during ``resizeEvent``.  A too-small value is
        ignored: it means the compositor has not finished configuring the
        surface, not that the display shrank.

        The geometry is re-applied here as well as in ``_on_container_resized``.
        Without that, a surface that reaches its final size *after* ``create()``
        returns updates the reported resolution but leaves both children sized to
        the stale (placeholder) rectangle — the slideshow then paints at low
        resolution in the top-left corner even though the panel size is detected
        correctly.  Re-running ``_relayout`` is idempotent and cheap.
        """
        if width < 100 or height < 100:
            return
        if (width, height) == (self._width, self._height):
            return
        logger.info(
            "Display surface resized: %dx%d → %dx%d",
            self._width,
            self._height,
            width,
            height,
        )
        self._width, self._height = width, height
        # Give both children the new full geometry before the next paint, so the
        # correct size is what gets rendered rather than merely reported.
        self._relayout()
        self._publish_display_info()

    def _publish_display_info(self) -> None:
        """Best-effort re-publish of ``display_info.json`` after a resize.

        The backend daemon reads this file to size media optimisation and the web
        UI reads it for the Display card, so a stale size here is visible in both
        places.
        """
        try:
            import json

            from metixel.shared.paths import run_dir

            run_dir().mkdir(parents=True, exist_ok=True)
            payload = {
                "width": self._width,
                "height": self._height,
                "backend": type(self).__name__,
                "output": self.connected_output(),
                "rotation": self._rotation,
            }
            target = run_dir() / "display_info.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(target)
        except Exception:
            logger.debug("could not re-publish display_info.json", exc_info=True)

    def destroy(self) -> None:
        self._running = False
        try:
            if self._canvas is not None:
                # Stop any ambient-blur child still running: it is its own
                # session, so it would otherwise outlive the frontend.
                self._canvas.close_backdrops()
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

    def present(
        self,
        plan: RenderPlan,
        image: Any = None,
        alpha: float = 1.0,
        backdrop_source: Any = None,
    ) -> None:
        """Paint *plan* on the canvas.

        Two distinct cases:

        * **Photo** — the canvas paints the artwork *and* the rings, and is the
          only visible surface.
        * **Video** — the frames come from the mpv widget, which is a sibling
          BELOW the canvas.  The canvas keeps every layer it normally paints but
          leaves the artwork rectangle unpainted, so the video shows through that
          hole.  See ``FrameCanvas.set_video_surface``.

        ``alpha`` applies to the artwork, so two complementary calls produce the
        crossfade.  The rings always paint opaque.
        """
        if self._canvas is None or self._window is None:
            return

        if self._video_path is not None:
            # The plan is re-applied on every present so a config change (style,
            # ambient) and a resize both take effect without a restart.
            #
            # The artwork handle is passed through deliberately: it is what the
            # ambient backdrop is keyed to, so a video keeps the same STILL
            # blurred surround a photo of it would have.  The handle's own artwork
            # is never drawn — that rectangle is the hole.
            #
            # Z-order note: no raise_() here.  set_video_surface() owns the order,
            # and asking for it every frame would be a restack request to the
            # compositor 31 times a second for an order already in place.
            self._apply_video_geometry(plan)
            self._reveal_video_surface_when_ready()
            self._canvas.update_plan(plan, image, alpha, backdrop_source=backdrop_source)
            return

        # No raise_() and no update() here: the canvas raises itself when the
        # surface mode changes, and repaints only when the picture actually
        # changed.
        self._canvas.set_video_surface(False)
        self._canvas.update_plan(plan, image, alpha, backdrop_source=backdrop_source)

    def present_transition(
        self,
        plan: RenderPlan,
        image: Any,
        alpha: float,
        prev_plan: RenderPlan | None,
        prev_image: Any,
        prev_alpha: float,
        backdrop_source: Any = None,
        prev_backdrop_source: Any = None,
    ) -> None:
        """Composite both crossfade layers in a SINGLE repaint.

        :meth:`present` stores one layer and requests a repaint.  Calling it twice
        for a transition does not blend: Qt coalesces the two ``update()``
        requests into one ``paintEvent``, so only the second layer survives and
        the incoming photo fades up from the background — which reads as "fade to
        black, then the next slide appears".

        Storing both layers and repainting once is what makes the outgoing and
        incoming images genuinely mix.  Both plans are passed because the two
        items usually have different aspect ratios, so each must be drawn with the
        geometry its own layout produced.
        """
        if self._canvas is None or self._window is None:
            return

        # A crossfade is an image transition: the outgoing layer here is an image
        # handle, never a live video surface.  Leaving video-surface mode is
        # therefore part of the contract, and doing it here means a stale mode can
        # never leave the artwork unpainted over the following photo.
        self._canvas.set_video_surface(False)
        self._canvas.update_transition(
            plan,
            image,
            alpha,
            prev_plan,
            prev_image,
            prev_alpha,
            backdrop_source=backdrop_source,
            prev_backdrop_source=prev_backdrop_source,
        )

    # -- Ambient backdrops ---------------------------------------------------

    def backdrop_ready(self, plan: RenderPlan, source: Any) -> bool:
        """Whether *plan*'s blurred backdrop is loaded and usable.

        The presenter polls this to decide whether it may start a transition —
        see ``PresentationEngine._transition_ready``.  Cheap: an equality test,
        with no pixel work and no disk.
        """
        if self._canvas is None:
            return True
        return bool(self._canvas.backdrop_ready(plan, source))

    def warm_backdrop(self, plan: RenderPlan, source: Any, handle: Any = None) -> None:
        """Start building *plan*'s backdrop in a throttled subprocess.

        Non-blocking and idempotent: a backdrop already loaded or in flight is
        ignored.  The finished file is adopted by :meth:`collect_warm_backdrop`
        on the GUI thread, because ``QPixmap`` must not be created off it.
        """
        if self._canvas is None:
            return
        self._canvas.warm_backdrop(plan, source, handle)

    def collect_warm_backdrop(self) -> bool:
        """Adopt a finished backdrop.  Returns ``True`` if one landed."""
        if self._canvas is None:
            return False
        return bool(self._canvas.collect_warm_backdrop())

    # -- Overlay -------------------------------------------------------------

    def present_overlay(self, elements: list[OverlayElement]) -> None:
        """Composite the overlay elements above whatever is showing.

        The canvas paints the overlay in BOTH surface modes — over the artwork
        and rings for a photo, and over the rings for a video — so there is
        nothing to switch here.  Which surface mode is active is owned by
        ``play_video`` / ``present`` / ``stop_video`` via
        ``FrameCanvas.set_video_surface``; this method only replaces the element
        list.

        An EMPTY *elements* is meaningful and must be passed through: it is how
        the canvas learns to drop the previous overlay.  Returning early here
        left a dismissed message painted on screen for good — invisible only
        because the message had already slid off the edge.
        """
        if self._canvas is None:
            return
        self._canvas.update_overlay(elements)

    # -- Artwork -------------------------------------------------------------

    def load_image(self, path: Path | Any) -> Any:
        """Load an image into a ``QImage`` handle.

        Returns ``None`` on failure rather than raising: one unreadable photo
        must never stop the slideshow, and the presenter treats ``None`` as
        "advance".

        Accepts a path, encoded ``bytes`` (from the preload worker) or a numpy
        array.  The bytes form matters: ``QImage`` is constructed here, on the
        GUI thread, because Qt objects must not be created off-thread.
        """
        try:
            from PySide6.QtGui import QImage

            if isinstance(path, QImage):
                return path
            if isinstance(path, (bytes, bytearray)):
                image = QImage.fromData(bytes(path))
                return None if image.isNull() else image
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
        """Start mpv playback, positioned over the plan's artwork rectangle.

        Returns ``False`` when the mpv pipeline is unavailable, so the presenter
        advances instead of waiting for frames that will never arrive.

        The widget is a sibling BELOW the canvas and is sized to the artwork rect;
        the canvas leaves that same rect unpainted, so the video shows through it
        while the canvas paints the ambient and the rings around it.  That is what
        lets a video carry the same framing, ambient fill and overlay as a photo
        (see the PHASE-0 spike in ``scripts/dev/_spike_video_hole.py``).

        The plan and its artwork handle are left as the preceding ``present()``
        set them, so a video keeps the still blurred backdrop built from its
        poster; nothing here clears them.
        """
        if self._mpv_widget is None or self._canvas is None:
            return False
        try:
            self._video_path = Path(path)
            self._video_revealed = False
            self._apply_video_geometry(plan)
            # The hole is deliberately NOT opened here — see
            # _reveal_video_surface_when_ready.  Until mpv has a frame, the canvas
            # keeps painting the poster, so the slide never flashes black.
            self._mpv_widget.ensure_render_context()
            self._mpv_widget.play(str(path))
            return True
        except Exception:
            logger.warning("mpv failed to play %s", path, exc_info=True)
            return False

    def _apply_video_geometry(self, plan: RenderPlan) -> None:
        """Place the mpv widget over *plan*'s artwork rect and set its fit.

        The rect comes from the shared rounding helper because the canvas clips
        its artwork hole to the same rect — see ``_artwork_rect``.

        Called on every ``present()`` while a video plays, so it MUST be a no-op
        when nothing moved.  Only the plan IDENTITY is compared, which is both
        exact and free: the presenter caches the current plan and hands back the
        same object each tick, recomputing it only when the geometry or the fit
        settings actually change.

        Re-applying regardless is not merely wasteful: ``setGeometry`` pushes a
        resize through a ``QOpenGLWidget``, and mpv renders into that widget's
        framebuffer.  Doing it 30 times a second re-creates the surface mpv's
        hardware-decode interop is bound to.
        """
        if self._mpv_widget is None or plan is self._video_plan:
            return
        self._video_plan = plan
        rect = _artwork_rect(plan)
        self._video_geometry = rect
        self._mpv_widget.setGeometry(*rect)
        # The video must FILL this rect rather than letterbox inside it.  The rect
        # is already the fit the framing engine chose for this media, so filling
        # is what "show this video here" means; letterboxing adds a redundant
        # second fit.
        #
        # Redundant and actively harmful, because the two fits disagree by a
        # pixel: ``int_rect`` rounds the far edge OUTWARD, so a portrait video on
        # a 1200px-tall panel — exact fit 675.0px wide — gets a 676px rect and mpv
        # letterboxes the 675px inside it.  Measured on the Pi: one pure black
        # column at x=1297, ambient blur resuming at x=1298 — a hairline seam down
        # the right edge of every portrait video.  Landscape lands on integer
        # dimensions, which is why the defect looked orientation-specific.  Filling
        # costs at most one pixel in a thousand of scale.
        self._mpv_widget.set_panscan(True)

    def _reveal_video_surface_when_ready(self) -> None:
        """Open the artwork hole only once mpv has a frame to fill it with.

        ``play_video`` used to reveal the hole immediately, which showed the mpv
        widget's uninitialised framebuffer — a black rectangle — for the moment
        between starting playback and the first decoded frame.  The poster is
        already painted underneath, so waiting costs nothing: the slide simply
        keeps its still poster for a frame or two and the video then appears,
        instead of flashing black in between.

        Called on every ``present()`` while a video is active.  A player that
        never becomes ready therefore leaves the poster up for the whole slide,
        which is the same graceful degradation as a backend that cannot play
        video at all.
        """
        if self._video_revealed:
            return
        widget = self._mpv_widget
        if widget is None or self._canvas is None:
            return
        try:
            ready = bool(widget.video_ready())
        except Exception:
            logger.debug("video_ready() failed", exc_info=True)
            return
        if not ready:
            return
        self._video_revealed = True
        self._canvas.set_video_surface(True)
        logger.debug("Video surface revealed (first frame is on screen)")

    def stop_video(self) -> None:
        """Stop playback, release the artwork hole, and restore the canvas.

        Idempotent — called on item advance, on queue reset, and during shutdown.
        """
        if self._mpv_widget is None:
            return
        try:
            self._mpv_widget.stop()
        except Exception:
            logger.debug("Error stopping mpv", exc_info=True)
        finally:
            self._video_path = None
            self._video_geometry = None
            self._video_plan = None
            self._video_revealed = False
            if self._canvas is not None:
                # Leave video-surface mode: the canvas is the only surface again,
                # so it must paint the artwork and stop claiming transparency.
                # set_video_surface() raises and repaints when the mode toggles;
                # the explicit update() covers the case where playback never
                # actually entered the mode.
                self._canvas.set_video_surface(False)
                self._canvas.update()
            # Grow the mpv widget back to the container.  It is hidden by the
            # canvas either way, but a stale artwork-sized rect would be reused
            # by the next video before its own geometry is applied.
            self._relayout()

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

    def video_ready(self) -> bool:
        """Whether mpv has a frame to show yet (see ``MpvRenderWidget``).

        The canvas's artwork hole must not be revealed before this is true, or
        the hole shows the widget's undefined framebuffer — black.
        """
        if self._mpv_widget is None:
            return False
        try:
            return bool(self._mpv_widget.video_ready())
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

    # -- Diagnostics ---------------------------------------------------------

    def effective_platform(self) -> str:
        """Return the Qt platform plugin actually in use, for diagnosis.

        ``QT_QPA_PLATFORM`` is pinned to ``wayland`` by the systemd unit, so this
        normally reports ``wayland``.  Surfacing it in the log is the point: if a
        future change lets Qt fall back to ``xcb`` on an X11-less frame, that is a
        black screen, and a logged line is the difference between a five-minute
        diagnosis and a long one.
        """
        try:
            from PySide6.QtGui import QGuiApplication

            return QGuiApplication.platformName() or "(unknown)"
        except Exception:
            return os.environ.get("QT_QPA_PLATFORM", "") or "(unknown)"

    def quit(self) -> None:
        """Ask Qt to leave the event loop."""
        self._running = False
        if self._app is not None:
            self._app.quit()

    def schedule(self, tick: Callable[[], bool]) -> None:
        """Drive *tick* from a QTimer while Qt owns the event loop.

        Qt MUST run its own loop: without ``exec()`` it delivers no input, no
        timers, no window events and no paints.  So instead of the renderer
        owning a ``while`` loop, a ``QTimer`` calls the tick from inside the
        event loop and the renderer's work becomes a callback.

        The tick's ``False`` return stops the timer and quits the loop, which is
        how a window close or a shutdown request ends the process cleanly.
        """
        from PySide6.QtCore import QTimer

        if self._app is None:
            logger.error("schedule() called before create() — no QApplication")
            return

        # Honour ``display.fps_limit``.  See :func:`tick_interval_ms` for why the
        # interval is never 0 and why that mattered (it measured 56 fps against a
        # configured 30 on a Pi 5, nearly doubling the frame budget for nothing).
        timer = QTimer()
        timer.setInterval(tick_interval_ms(self._fps_limit))
        self._timer = timer

        def _on_tick() -> None:
            try:
                if not tick():
                    timer.stop()
                    self._running = False
                    self._app.quit()
            except Exception:
                # An exception escaping into Qt's event loop would be swallowed
                # and leave a frozen window with no trace.  Log and stop
                # instead, so the failure is visible and the OTA gate can see it.
                logger.exception("Frame tick failed — stopping the render loop")
                timer.stop()
                self._running = False
                self._app.quit()

        timer.timeout.connect(_on_tick)
        timer.start()
        self._app.exec()


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
