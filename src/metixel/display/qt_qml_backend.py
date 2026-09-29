# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Qt Quick / Qt Multimedia display backend — GPU-composited, vsync-paced.

This is the hardware-accelerated replacement for :mod:`metixel.display.qt_backend`.
It renders the same :class:`~metixel.framing.layout.RenderPlan` through a Qt Quick
scene graph (``qml/Frame.qml``) instead of a raster ``QWidget``, and plays video
through Qt Multimedia instead of libmpv.

Why it exists — measured on a Pi 5, both renderers on the SAME 60 fps clip:

    |                        | Qt Quick + Qt Multimedia | libmpv software render |
    |------------------------|--------------------------|------------------------|
    | presented              | 59.91 fps                | 60 draws/s             |
    | vsync-locked           | 99.72%                   | none (timer-driven)    |
    | dropped                | 2 / 1805                 | n/a                    |
    | CPU                    | 35.5% of one core        | 182% of one core       |

The mechanism is not "Qt is faster"; it is *where the copies happen*. The software
path does hardware decode -> COPY BACK to system memory -> software scale+convert
-> upload to GL: three copies per frame, all on the CPU. Here the decoder's frame
is uploaded and the **GPU** scales it, via ``Image.sourceRect`` and the scene graph.
That is a ~5x CPU reduction on identical content, and it is what leaves the frame
enough headroom to hold vsync instead of juddering.

It also removes the descriptor leak entirely rather than working around it: the
leak was libplacebo's GL draw path (reached through ``vo=libmpv``), and
libplacebo is not in this stack at all — video goes through Qt Multimedia's FFmpeg
backend.

Three further consequences worth knowing before editing anything here:

1. **``swap_buffers()`` is a no-op, and that is the point.** The scene graph
   renders on the compositor's vsync clock. The raster backend was driven by
   ``display.fps_limit``, which is 30 on the frame — so the old path could not
   present 60 fps video smoothly no matter how fast the machine was.
2. **The video "hole" no longer exists.** `FrameCanvas` left ``plan.artwork_dst``
   unpainted and relied on a sibling widget showing through, which needed
   ``QRegion.subtracted``, ``setClipRegion``, ``WA_TranslucentBackground`` /
   ``WA_OpaquePaintEvent`` toggling, explicit ``raise_()`` and a reveal step that
   withheld the hole until the first frame decoded. In one scene graph the video is
   simply an item declared below the rings, so there is no unpainted region, and
   therefore no reveal ordering to get wrong. The poster sits under the video, so a
   video with no frame yet shows the poster rather than black.
3. **Handles are URL strings**, not QPixmaps. ``load_image`` decodes to a ``QImage``
   and registers it with an image provider, returning an ``image://metixel/<key>``
   URL. Decoding happens in ``load_image`` (on the caller's thread, off the scene
   thread) rather than inside the scene graph, because a 12 MP JPEG decoded on the
   scene thread stalls a frame — the same hazard the raster path avoided by
   pre-scaling to a pixmap. ``QImage`` is reentrant, so this is thread-safe; a
   ``QPixmap`` would not be.

Standard library and core imports only at module scope: PySide6 is imported lazily
so that ``detect_backend()`` can probe for this backend, and so this module stays
importable on a machine with no Qt at all (the desktop dev path).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import numpy as np

from metixel.display.ambient_blur import BackdropRequest, BackdropRunner
from metixel.display.backend import DisplayBackend
from metixel.display.geometry import int_rect
from metixel.display.overlay_element import OverlayElement
from metixel.framing.layout import RenderPlan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication, QImage
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow

logger = logging.getLogger(__name__)

#: Name of the image provider QML asks for artwork through. QML refers to a
#: handle as ``image://<PROVIDER_ID>/<key>``.
PROVIDER_ID = "metixel"

#: Fallback frame rate when ``display.fps_limit`` is missing or non-positive.
#: Only the *scene tick* is paced by this — Qt Quick presents on vsync — but the
#: tick is what drives the slideshow state machine, so it must never be 0.
#: See :func:`tick_interval_ms`.
DEFAULT_FPS_LIMIT = 30

#: Where the scene lives, relative to this module.
QML_PATH = Path(__file__).parent / "qml" / "Frame.qml"


def tick_interval_ms(fps_limit: int | None) -> int:
    """Return the render timer's interval in milliseconds for *fps_limit*.

    ``QTimer.setInterval(0)`` does not mean "as fast as possible" — it means "on
    every event-loop pass", which measured 56 fps against a configured 30 on a Pi 5
    and nearly doubled the frame budget for nothing. A non-positive or unparseable
    limit therefore falls back to :data:`DEFAULT_FPS_LIMIT` rather than to 0.

    Separate from the class so the arithmetic is testable without Qt.
    """
    if fps_limit is None:
        return int(1000 / DEFAULT_FPS_LIMIT)
    try:
        fps = int(fps_limit)
    except (TypeError, ValueError):
        fps = DEFAULT_FPS_LIMIT
    if fps <= 0:
        fps = DEFAULT_FPS_LIMIT
    # round(), not int(): truncating 1000/24 gives 41 ms and 1000/60 gives 16 ms,
    # both of which are FASTER than requested and quietly over-run the frame
    # budget. test_tick_interval caught this against the original implementation.
    return max(1, round(1000 / fps))


def _rect(pxrect: Any) -> dict[str, float]:
    """A ``PxRect`` (x, y, w, h) as a JS-friendly object.

    QML's ``Repeater`` delivers each entry of a ``var`` model as ``modelData``, so
    the ring layers are three arrays of these rather than three lists of
    coordinate arrays — named keys keep the QML side readable.
    """
    x, y, w, h = pxrect
    return {"x": float(x), "y": float(y), "w": float(w), "h": float(h)}


def _colour(value: Any) -> str:
    """Coerce a plan colour to a string QML's ``color`` accepts.

    Plans carry these as strings, but the framing layer has been known to hold a
    tuple, and ``Rectangle.color`` silently renders nothing for a malformed value —
    which would look like a missing mat rather than a bad colour.
    """
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        r, g, b = (int(max(0.0, min(1.0, float(c))) * 255) for c in value[:3])
        return f"#{r:02x}{g:02x}{b:02x}"
    return "#000000"


class QmlBackend(DisplayBackend):
    """GPU-composited display backend built on Qt Quick.

    Selected by ``detect_backend()`` on a Raspberry Pi when Qt Quick and Qt
    Multimedia are available, and forceable with
    ``METIXEL_DISPLAY_BACKEND=qml`` for A/B testing on a real frame.
    """

    def __init__(self) -> None:
        self._app: QGuiApplication | None = None
        self._engine: QQmlApplicationEngine | None = None
        self._root: QQuickWindow | None = None
        self._timer: QTimer | None = None

        self._width = 1920
        self._height = 1200
        self._fps_limit = DEFAULT_FPS_LIMIT
        self._running = False
        self._background = (0.0, 0.0, 0.0, 1.0)

        # Image provider store. Guarded because `load_image` may be called from the
        # preload worker while the scene graph is reading the same dict.
        self._images: dict[str, QImage] = {}
        self._images_lock = threading.Lock()

        # Video
        self._video: Any = None
        self._video_ready = False
        self._video_source: Path | None = None

        # Ambient backdrop (blur), mirroring the raster canvas's model: one request
        # derived in one place, so readiness, warming and adoption cannot disagree.
        self._backdrop_runner: BackdropRunner | None = None
        self._backdrop_pending: BackdropRequest | None = None
        self._backdrop_adopted: dict[str, Path] = {}
        self._backdrop_failed: set[str] = set()
        self._backdrop_path: Path | None = None
        self._prev_backdrop_path: Path | None = None

        self._display_power: Any = None
        self._wlr_output: Any = None

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
        """Build the QGuiApplication, load the scene and show the window.

        QGuiApplication, not QApplication: this backend is built on Qt Quick and
        imports no QtWidgets at all (the raster canvas that needed QWidget is not
        part of 2.0.0).  The distinction is load-bearing because it is what lets
        `python3-pyside6.qtwidgets` stay out of requirements-system.txt.

        Import failures are reported through ``create()`` rather than escaping as an
        ImportError, so a missing Qt package surfaces as a diagnosable startup error
        the OTA health gate can act on.
        """
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtQuick import QQuickWindow

        self._width = int(width) or 1920
        self._height = int(height) or 1200
        self._fps_limit = fps_limit

        if QGuiApplication.instance() is None:
            self._app = QGuiApplication([])
        else:  # pragma: no cover - only on a second backend in one process
            self._app = QGuiApplication.instance()  # type: ignore[assignment]

        engine = QQmlApplicationEngine()
        self._engine = engine

        # Warnings are logged rather than swallowed: a QML type that fails to
        # resolve would otherwise leave a window that renders nothing, with no
        # explanation anywhere.
        def _on_warnings(items: list[Any]) -> None:
            for item in items:
                logger.error("QML: %s", item.toString())

        engine.warnings.connect(_on_warnings)

        if not QML_PATH.exists():  # pragma: no cover - packaging error
            raise RuntimeError(f"QML scene missing: {QML_PATH}")

        # Registered BEFORE load(): the scene's Image elements resolve
        # `image://metixel/<key>` as soon as they are created, so a provider added
        # afterwards would leave every artwork layer blank.
        engine.addImageProvider(PROVIDER_ID, _image_provider(self))

        engine.load(QML_PATH.as_uri())
        roots = engine.rootObjects()
        if not roots:
            raise RuntimeError(f"QML scene failed to load: {QML_PATH}")

        root = roots[0]
        if not isinstance(root, QQuickWindow):
            raise RuntimeError("Frame.qml root object is not a Window")
        self._root = root

        root.setProperty("screenW", self._width)
        root.setProperty("screenH", self._height)
        root.setProperty("artworkSrcW", 0.0)
        root.setProperty("artworkSrcH", 0.0)
        root.setProperty("backColour", self._background_colour())
        root.setProperty("videoVisible", False)
        root.setProperty("overlayElements", [])

        if fullscreen:
            root.setProperty("visibility", QQuickWindow.Visibility.FullScreen)
        else:
            root.setProperty("width", self._width)
            root.setProperty("height", self._height)

        # hide_cursor: cage is started with `-d` (see metixel-cage.service), so the
        # compositor never draws a cursor and there is nothing for us to hide.
        # `metixel-cursor-hider.service` covers the remaining case. Recorded here so
        # the parameter's absence is visibly deliberate rather than forgotten.
        _ = hide_cursor
        # rotation: not a transform on the scene. The framing engine already
        # accounts for `display.rotation` when it computes the plan, so rotating
        # here as well would apply it twice.
        _ = rotation

        self._running = True
        logger.info(
            "QmlBackend ready: %dx%d, scene=%s, rhi=%s",
            self._width,
            self._height,
            QML_PATH.name,
            self._graphics_api(),
        )

    def destroy(self) -> None:
        """Release the video pipeline, backdrop child and scene."""
        self.stop_video()
        if self._backdrop_runner is not None:
            self._backdrop_runner.close()
            self._backdrop_runner = None
        self._running = False
        self._timer = None
        self._root = None
        self._engine = None
        with self._images_lock:
            self._images.clear()

    def loop_running(self) -> bool:
        return self._running

    def quit(self) -> None:
        """Ask Qt to leave the event loop. Safe from another thread."""
        self._running = False
        if self._app is not None:
            self._app.quit()

    def _graphics_api(self) -> str:
        """The RHI backend actually in use, for the log line at startup."""
        try:
            iface = self._root.rendererInterface() if self._root is not None else None
            return str(iface.graphicsApi()) if iface is not None else "unknown"
        except Exception:  # noqa: BLE001 - diagnostics must never break startup
            return "unknown"

    # -- Frame scheduling ----------------------------------------------------

    def schedule(self, tick: Callable[[], bool]) -> None:
        """Drive *tick* from a QTimer while Qt owns the main thread.

        Qt MUST run its own event loop or nothing is delivered, so the backend
        cannot own a ``while`` loop the way tkinter can. ``tick`` returning ``False``
        stops the timer and quits.

        The timer only paces the slideshow state machine. Presentation is not
        gated on it: the scene graph renders when the compositor is ready, which is
        exactly why this backend can present 60 fps video with a 30 fps tick.
        """
        from PySide6.QtCore import QTimer

        if self._app is None:  # pragma: no cover - misuse
            logger.error("schedule() called before create() — no QGuiApplication")
            return

        timer = QTimer()
        timer.setInterval(tick_interval_ms(self._fps_limit))
        self._timer = timer

        def _on_tick() -> None:
            try:
                if not tick():
                    timer.stop()
                    self._running = False
                    self._app.quit()  # type: ignore[union-attr]
            except Exception:
                # An exception escaping into Qt's loop would be swallowed and leave
                # a frozen window with no trace, which is the worst possible failure
                # on a wall-mounted frame.
                logger.exception("Frame tick failed — stopping the render loop")
                timer.stop()
                self._running = False
                self._app.quit()  # type: ignore[union-attr]

        timer.timeout.connect(_on_tick)
        timer.start()
        self._app.exec()

    def swap_buffers(self) -> None:
        """No-op — deliberately.

        The scene graph presents on the compositor's vsync clock. Anything done here
        would be a second presentation mechanism racing the first, and the platform
        does not need to be told when to swap.
        """

    # -- Frame presentation --------------------------------------------------

    def present(
        self,
        plan: RenderPlan,
        image: Any = None,
        alpha: float = 1.0,
        backdrop_source: Any = None,
    ) -> None:
        """Write *plan* into the scene.

        This is a property update, not a paint: QML re-renders on its own clock, and
        an unchanged frame costs nothing (measured 0.9% of a core for an idle Qt
        event loop, against 83% to recomposite an unchanging 1920x1200 raster frame
        at 31 fps).

        Single-layer entry point: the previously drawn artwork is superseded, so the
        outgoing layer is cleared. Use :meth:`present_transition` to blend.
        """
        root = self._root
        if root is None:
            return

        self._apply_plan(root, plan)
        root.setProperty("artworkSource", self._url(image))
        root.setProperty("artworkOpacity", float(alpha))
        root.setProperty("prevArtworkOpacity", 0.0)
        self._apply_backdrop(root, plan, backdrop_source)

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
        """Composite the outgoing and incoming frames in ONE repaint.

        QML does this natively and on the GPU: two ``Image`` items whose opacities
        sum to 1, so nothing dims through the middle. The raster backend had to
        blend two full-canvas layers per repaint in software, which is where the
        crossfade judder came from.

        Measured on a Pi 5 with a *continuous* 2.5 s crossfade at 60 fps:
        99.67% of frames vsync-locked for 5.7% of one core — 0.3% more than the same
        scene with no blend at all.
        """
        root = self._root
        if root is None:
            return

        # The incoming plan drives the rings/ambient for the whole transition: the
        # frame geometry is the destination's, and the outgoing artwork is clipped
        # to its own rect below.
        self._apply_plan(root, plan)
        root.setProperty("artworkSource", self._url(image))
        root.setProperty("artworkOpacity", float(alpha))

        if prev_plan is None:
            root.setProperty("prevArtworkOpacity", 0.0)
        else:
            root.setProperty("prevArtworkSource", self._url(prev_image))
            root.setProperty("prevArtworkOpacity", float(prev_alpha))
            self._apply_rect(root, "prevArtwork", prev_plan.artwork_dst)
            self._apply_source_rect(root, "prevArtwork", prev_plan.artwork_src)

        self._apply_backdrop(root, plan, backdrop_source)
        if prev_backdrop_source is not None:
            self._apply_prev_backdrop(root, prev_plan, prev_backdrop_source)

    def present_overlay(self, elements: list[OverlayElement]) -> None:
        """Replace the overlay layer.

        An empty list is meaningful: it clears the overlay, so a dismissed message
        does not stay painted.
        """
        root = self._root
        if root is None:
            return
        root.setProperty("overlayElements", [_overlay_entry(e) for e in elements])

    def _apply_plan(self, root: QQuickWindow, plan: RenderPlan) -> None:
        """Push the plan's geometry and colours into the scene."""
        root.setProperty("screenW", int(self._width))
        root.setProperty("screenH", int(self._height))
        root.setProperty("ambientColour", _colour(plan.ambient_colour))
        root.setProperty("whitespaceColour", _colour(plan.whitespace_colour))
        root.setProperty("matteColour", _colour(plan.matte_colour))
        root.setProperty("mouldingColour", _colour(plan.matte_colour))
        root.setProperty("ambientVisible", plan.ambient_strategy != "none")

        if plan.ambient is None:
            root.setProperty("ambientVisible", False)
        else:
            self._apply_rect(root, "ambient", plan.ambient)

        self._apply_rect(root, "artwork", plan.artwork_dst)
        self._apply_source_rect(root, "artwork", plan.artwork_src)

        # Rings: disjoint rectangles, drawn in the order the framing spec mandates.
        root.setProperty("whitespaceRects", [_rect(r) for r in plan.whitespace])
        root.setProperty("matteRects", [_rect(r) for r in plan.matte])
        root.setProperty("mouldingRects", [_rect(r) for r in plan.moulding])

    def _apply_rect(self, root: QQuickWindow, prefix: str, pxrect: Any) -> None:
        x, y, w, h = pxrect
        root.setProperty(f"{prefix}X", float(x))
        root.setProperty(f"{prefix}Y", float(y))
        root.setProperty(f"{prefix}W", float(w))
        root.setProperty(f"{prefix}H", float(h))

    def _apply_source_rect(self, root: QQuickWindow, prefix: str, src: Any) -> None:
        """The source crop for the fit mode, fed to ``Image.sourceClipRect``.

        Qt crops in the scene graph for free; the raster backend instead pre-scaled
        every slide to a pixmap and cached it, which cost memory per slide and a
        rescale on every cache miss.
        """
        x, y, w, h = src
        root.setProperty(f"{prefix}SrcX", float(x))
        root.setProperty(f"{prefix}SrcY", float(y))
        root.setProperty(f"{prefix}SrcW", float(w))
        root.setProperty(f"{prefix}SrcH", float(h))

    # -- Artwork -------------------------------------------------------------

    def load_image(self, path: Path | np.ndarray | bytes) -> Any:
        """Decode an image and return an ``image://`` handle.

        Decoding happens HERE, on the calling thread, rather than inside the scene
        graph: the renderer preloads off-thread, and a full-resolution JPEG decoded
        on the scene thread drops a frame. Returning a URL rather than a pixmap also
        keeps the handle a plain string, so it crosses the worker/main boundary as a
        value instead of as a GUI-thread resource.

        Returns ``None`` when the payload cannot be decoded, which callers already
        treat as "no artwork".
        """
        image = self._decode(path)
        if image is None or image.isNull():
            return None

        key = uuid4().hex
        with self._images_lock:
            self._images[key] = image
        return f"image://{PROVIDER_ID}/{key}"

    def _decode(self, path: Path | np.ndarray | bytes) -> QImage | None:
        from PySide6.QtGui import QImage

        try:
            if isinstance(path, np.ndarray):
                arr = np.ascontiguousarray(path)
                height, width = arr.shape[0], arr.shape[1]
                channels = arr.shape[2] if arr.ndim == 3 else 1
                fmt = {
                    1: QImage.Format.Format_Grayscale8,
                    3: QImage.Format.Format_RGB888,
                    4: QImage.Format.Format_RGBA8888,
                }.get(channels)
                if fmt is None:
                    logger.warning("Unsupported array shape for artwork: %s", arr.shape)
                    return None
                # .copy() because the QImage would otherwise reference `arr`, which
                # is free to be collected while the texture upload is still queued.
                return QImage(arr.data, width, height, arr.strides[0], fmt).copy()
            if isinstance(path, bytes):
                img = QImage()
                img.loadFromData(path)
                return img
            img = QImage()
            img.load(str(path))
            if img.isNull():
                logger.warning("Could not decode artwork: %s", path)
                return None
            return img
        except Exception:
            logger.exception("Artwork decode failed for %r", type(path).__name__)
            return None

    def unload_image(self, handle: Any) -> None:
        """Drop an ``image://metixel/<key>`` handle from the provider store."""
        key = self._key_of(handle)
        if key is None:
            return
        with self._images_lock:
            self._images.pop(key, None)

    @staticmethod
    def _key_of(handle: Any) -> str | None:
        if not isinstance(handle, str):
            return None
        marker = f"image://{PROVIDER_ID}/"
        if not handle.startswith(marker):
            return None
        return handle[len(marker) :]

    @staticmethod
    def _url(handle: Any) -> Any:
        """A handle as a QUrl for the scene. Empty handles clear the layer."""
        from PySide6.QtCore import QUrl

        if handle is None:
            return QUrl()
        if isinstance(handle, str):
            return QUrl(handle)
        return QUrl.fromLocalFile(str(handle))

    # -- Ambient backdrop (blur) ---------------------------------------------

    def backdrop_request(self, plan: RenderPlan, source: Any) -> BackdropRequest | None:
        """Derive the backdrop request for *plan*.

        The ONE place a request is built, so the readiness check, the warm request
        and the adoption cannot disagree about what a backdrop is — a disagreement
        there would hold every slide forever. Mirrors ``FrameCanvas.backdrop_request``.
        """
        if plan.ambient_strategy != "blur" or source is None:
            return None
        _, _, width, height = int_rect(plan.screen)
        return BackdropRequest.build(
            source,
            width,
            height,
            float(plan.ambient_blur_radius),
            str(plan.ambient_blur_filter),
        )

    def backdrop_ready(self, plan: RenderPlan, source: Any) -> bool:
        """Whether *plan*'s blurred backdrop is available.

        Asked before a transition starts, so a slide is held until its backdrop
        exists rather than a crossfade beginning against a backdrop that is not
        there yet. Cheap by design: a dict lookup, no pixel work and no disk.

        Always ``True`` for a non-blur plan, and for one whose backdrop cannot be
        built at all — there is nothing to wait for, and holding would be a
        permanent stall rather than a short delay.
        """
        request = self.backdrop_request(plan, source)
        if request is None or request.job_id in self._backdrop_failed:
            return True
        return request.job_id in self._backdrop_adopted

    def warm_backdrop(self, plan: RenderPlan, source: Any, handle: Any = None) -> None:
        """Start building *plan*'s backdrop in a throttled subprocess.

        Called one slide AHEAD of the item that needs it, so the work happens in the
        idle time the current slide provides. Non-blocking and idempotent.
        """
        request = self.backdrop_request(plan, source)
        if request is None or request.job_id in self._backdrop_failed:
            return
        if request.job_id in self._backdrop_adopted:
            return
        if self._backdrop_pending is not None and self._backdrop_pending == request:
            return  # already in flight

        if self._backdrop_runner is None:
            self._backdrop_runner = BackdropRunner()
        self._backdrop_pending = request
        self._backdrop_runner.start(request)

    def collect_warm_backdrop(self) -> bool:
        """Adopt a finished backdrop. Returns True when the scene changed.

        Unlike the raster path this needs NO decode: the child already wrote a JPEG,
        and the scene loads it by URL. So there is no ``QPixmap`` to construct on the
        GUI thread and nothing for the caller to schedule around.
        """
        runner = self._backdrop_runner
        if runner is None:
            return False
        finished = runner.take_finished()
        if finished is None:
            return False

        job_id, path = finished
        request = self._backdrop_pending
        self._backdrop_pending = None

        if path is None or request is None or request.job_id != job_id:
            runner.release(job_id)
            self._backdrop_failed.add(job_id)
            return False

        # The previous backdrop becomes the outgoing layer so a crossfade does not
        # leave the ambient band snapping to the new colour while the artwork is
        # still fading.
        self._prev_backdrop_path = self._backdrop_path
        self._backdrop_path = path
        self._backdrop_adopted[job_id] = path
        return True

    def _apply_backdrop(self, root: QQuickWindow, plan: RenderPlan, source: Any) -> None:
        """Point the ambient layer at the blurred JPEG, or fall back to flat colour."""
        request = self.backdrop_request(plan, source)
        path = self._backdrop_adopted.get(request.job_id) if request is not None else None
        if path is None:
            # Flat ambient fill. Correct, not a failure: it is what a non-blur plan
            # asks for, and what a plan whose blur cannot be built falls back to.
            root.setProperty("ambientSource", self._url(None))
            root.setProperty("ambientVisible", True)
            return
        root.setProperty("ambientSource", self._url(path))
        root.setProperty("ambientVisible", True)
        root.setProperty("prevAmbientSource", self._url(self._prev_backdrop_path))
        root.setProperty("prevAmbientOpacity", 0.0 if self._prev_backdrop_path is None else 1.0)

    def _apply_prev_backdrop(
        self, root: QQuickWindow, prev_plan: RenderPlan | None, source: Any
    ) -> None:
        if prev_plan is None:
            root.setProperty("prevAmbientVisible", False)
            return
        request = self.backdrop_request(prev_plan, source)
        path = self._backdrop_adopted.get(request.job_id) if request is not None else None
        if path is None:
            root.setProperty("prevAmbientVisible", False)
            return
        root.setProperty("prevAmbientSource", self._url(path))
        self._apply_rect(root, "prevAmbient", prev_plan.ambient or prev_plan.screen)
        root.setProperty("prevAmbientVisible", True)

    # -- Video ---------------------------------------------------------------

    def play_video(self, path: Path, plan: RenderPlan) -> bool:
        """Start playback through Qt Multimedia into the scene's ``VideoOutput``.

        Qt Multimedia hardware-decodes HEVC on a Pi 5 through V4L2 stateless
        (``/dev/video19``, DMABuf in and out). Note the frames come back as
        ``HandleType.NoHandle`` system-memory images: Qt 6.8's Linux GPU-texture
        interop is VAAPI-only and a Pi has no VAAPI, so there is one read-back and
        re-upload per frame. That is still far cheaper than the software path,
        because the GPU does the scaling and there is no software colour convert.
        """
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QMediaPlayer

        root = self._root
        if root is None:
            return False

        if self._video is None:
            self._video = QMediaPlayer()
            sink = None
            try:
                # The sink is created lazily, so reading it once at construction can
                # legitimately return null — treat that as "not ready yet", not as
                # "no hardware path".
                sink = self._video.videoSink()
            except Exception:  # noqa: BLE001 - older/newer binding differences
                sink = None
            if sink is not None:
                sink.videoFrameChanged.connect(self._on_first_frame)
            self._video.mediaStatusChanged.connect(self._on_media_status)
            self._video.errorOccurred.connect(
                lambda err, msg: logger.error("Video error %s: %s", err, msg)
            )
            vo = self._find_video_output(root)
            if vo is not None:
                self._video.setVideoOutput(vo)

        self._video_source = Path(path)
        self._video_ready = False
        root.setProperty("videoVisible", False)
        self._apply_video_geometry(root, plan)

        self._video.setSource(QUrl.fromLocalFile(str(path)))
        self._video.play()
        return True

    def _find_video_output(self, root: QQuickWindow) -> Any:
        """Locate the scene's VideoOutput by objectName."""
        try:
            from PySide6.QtCore import QObject

            found = root.findChild(QObject, "videoOut")
            return found
        except Exception:  # noqa: BLE001
            logger.exception("Could not locate VideoOutput in the scene")
            return None

    def _on_first_frame(self, frame: Any) -> None:
        """Flip the video visible once a real frame exists.

        The poster artwork sits beneath the video, so this is a cosmetic switch
        rather than the hole-reveal the raster backend needed — showing the video
        one frame early would show black over the poster, not undefined content.
        """
        if self._video_ready:
            return
        self._video_ready = True
        if self._root is not None:
            self._root.setProperty("videoVisible", True)
        _ = frame

    def _on_media_status(self, status: Any) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if status == QMediaPlayer.MediaStatus.BufferedMedia and not self._video_ready:
            # Fallback for a player whose sink never reported a frame; the video is
            # buffered and about to present, and the poster keeps something on screen
            # in the meantime.
            self._on_first_frame(None)

    def _apply_video_geometry(self, root: QQuickWindow, plan: RenderPlan) -> None:
        """Position the video over the artwork rectangle."""
        self._apply_rect(root, "video", plan.artwork_dst)

    def stop_video(self) -> None:
        """Stop playback and release the player. Idempotent."""
        if self._video is not None:
            try:
                self._video.stop()
                self._video.setSource(self._url(None))
            except Exception:  # noqa: BLE001 - teardown must never raise
                logger.debug("Video teardown raised", exc_info=True)
        self._video_ready = False
        self._video_source = None
        if self._root is not None:
            self._root.setProperty("videoVisible", False)

    def pause_video(self, paused: bool = True) -> None:
        """Pause or resume without tearing the pipeline down."""
        if self._video is None:
            return
        if paused:
            self._video.pause()
        else:
            self._video.play()

    def video_playing(self) -> bool:
        from PySide6.QtMultimedia import QMediaPlayer

        if self._video is None:
            return False
        return bool(self._video.playbackState() == QMediaPlayer.PlaybackState.PlayingState)

    def video_finished(self) -> bool:
        from PySide6.QtMultimedia import QMediaPlayer

        if self._video is None:
            return False
        return bool(self._video.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia)

    def video_ready(self) -> bool:
        """Whether a decoded frame is on screen yet.

        Distinct from ``video_playing``: playback can have started while the first
        frame is still being decoded.
        """
        return self._video_ready

    # -- Display Control -----------------------------------------------------

    def _background_colour(self) -> str:
        r, g, b, _a = self._background
        return _colour((r, g, b))

    def set_background(self, color: tuple[float, float, float, float]) -> None:
        """Set the scene's background colour (RGBA, 0.0-1.0)."""
        self._background = color
        if self._root is not None:
            self._root.setProperty("backColour", self._background_colour())

    def clear(self) -> None:
        """Clear the scene to the background colour."""
        if self._root is None:
            return
        self._root.setProperty("artworkSource", self._url(None))
        self._root.setProperty("prevArtworkOpacity", 0.0)
        self._root.setProperty("whitespaceRects", [])
        self._root.setProperty("matteRects", [])
        self._root.setProperty("mouldingRects", [])
        self._root.setProperty("overlayElements", [])

    def _wlr(self) -> Any:
        """The shared :class:`WlrOutput` adapter, built once."""
        if self._wlr_output is None:
            from metixel.display.hardware import WlrOutput

            self._wlr_output = WlrOutput()
        return self._wlr_output

    def display_power(self, on: bool) -> None:
        """Turn the panel on or off via the shared tiered fallback chain.

        Delegates to :class:`metixel.display.hardware.DisplayPower` rather than
        reimplementing ``wlr-randr``/DPMS/``vcgencmd`` here, so there is one owner of
        that chain. Never raises: failing to sleep the panel must not take the frame
        down.
        """
        try:
            if self._display_power is None:
                from metixel.display.hardware import DisplayPower

                # DisplayPower takes the WlrOutput it delegates the Wayland half of
                # the fallback chain to; it is not constructible on its own.
                self._display_power = DisplayPower(self._wlr())
            self._display_power.set(on)
        except Exception:  # noqa: BLE001 - must never raise
            logger.warning("Display power %s failed", "on" if on else "off", exc_info=True)

    # -- Diagnostics ---------------------------------------------------------

    def connected_output(self) -> str | None:
        """The connected output name (e.g. ``HDMI-A-2``), or ``None``."""
        try:
            name = str(self._wlr().resolve())
            return name or None
        except Exception:  # noqa: BLE001 - diagnostics only
            return None

    def list_modes(self) -> list[dict[str, Any]]:
        """Display modes the monitor and host both support."""
        try:
            return [dict(mode) for mode in self._wlr().list_modes()]
        except Exception:  # noqa: BLE001 - diagnostics only
            return []


def _image_provider(backend: QmlBackend) -> Any:
    """Build the ``QQuickImageProvider`` that serves decoded artwork.

    ``load_image`` decodes to a ``QImage`` and stores it under a key; the scene asks
    for it by URL. Serving already-decoded images is the point: if QML were given a
    file path instead, Qt would decode on the scene thread and a full-resolution
    JPEG would drop a frame at exactly the moment the slide changes.

    ``requestImage`` returns the stored image directly rather than a copy — Qt
    uploads it into a texture and does not take ownership, and copying a 12 MP image
    per request would defeat the purpose of pre-decoding it.
    """

    def _factory() -> Any:
        # QQuickImageProvider lives in QtQuick, NOT QtQml. Importing it from QtQml
        # raises ImportError while the scene is being built, which takes down
        # startup entirely — it is not a missing-feature fallback, it is a crash
        # loop. Verified against the frame's PySide6 6.8.2.
        from PySide6.QtGui import QImage
        from PySide6.QtQuick import QQuickImageProvider

        class _Store(QQuickImageProvider):  # type: ignore[misc]
            def __init__(self) -> None:
                super().__init__(QQuickImageProvider.ImageType.Image)

            def requestImage(  # noqa: N802 - Qt naming
                self, image_id: str, size: Any, requested_size: Any
            ) -> Any:
                with backend._images_lock:  # noqa: SLF001 - owner-private by design
                    image = backend._images.get(image_id)  # noqa: SLF001
                if image is None:
                    # A missing key returns a null image, which QML renders as
                    # nothing. That is the correct outcome for an unloaded handle,
                    # and it must not raise into the scene graph.
                    return QImage()
                if size is not None:
                    size.setWidth(image.width())
                    size.setHeight(image.height())
                _ = requested_size
                return image

        return _Store()

    return _factory()


def _overlay_entry(element: OverlayElement) -> dict[str, Any]:
    """An :class:`OverlayElement` as a JS-friendly object for the scene.

    Read with ``getattr`` and defaulted because the overlay element type carries
    more fields than the scene draws, and a missing optional must not blank the
    whole overlay layer.
    """
    return {
        "text": str(getattr(element, "text", "") or ""),
        "x": float(getattr(element, "x", 0.0) or 0.0),
        "y": float(getattr(element, "y", 0.0) or 0.0),
        "size": int(getattr(element, "size", 0) or 0) or 24,
        "opacity": float(getattr(element, "opacity", 1.0) or 0.0),
        "colour": _colour(getattr(element, "colour", None) or getattr(element, "color", None)),
        "align": int(getattr(element, "align", 0) or 0),
    }
