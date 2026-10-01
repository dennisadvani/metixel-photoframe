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
from collections import OrderedDict
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

#: Maximum decoded images the provider store retains before it evicts the least
#: recently used one.
#:
#: This is a backstop, not a cache policy. The store holds a *strong* reference to
#: every image it hands out: Qt uploads it into a texture and does not take
#: ownership, and nothing reference-counts on our behalf, so a handle that is never
#: passed to :meth:`QmlBackend.unload_image` is never freed. Callers are therefore
#: expected to release, and this cap only catches the ones that cannot — the
#: presenter loads a video's last frame deliberately WITHOUT caching it, so no
#: cache eviction can ever see that handle.
#:
#: Sized against what the scene can hold at once: artwork and prevArtwork during a
#: crossfade, each with an ambient backdrop built from the *same* handle, plus the
#: presenter's decode-ahead item. Six is twice that live set, so an eviction can
#: only ever target an image several slides old. Unbounded, this store consumed
#: ~1 GB in ten minutes on a Pi 5 until the OOM killer took the frontend
#: (``Out of memory: Killed process <frontend> anon-rss:1021456kB``, once per
#: slideshow pass, forever).
MAX_STORED_IMAGES = 6

#: How many recently-requested images stay pinned against eviction and release.
#:
#: The cap alone is not enough, and that is the whole reason this exists. QML
#: re-requests a source for as long as the item is on screen, and ``prevArtwork``
#: keeps requesting its source for the *whole* of a crossfade — so dropping a
#: handle the presenter has finished with blanks a layer that is still being
#: painted. Observed on the frame as, repeatedly,
#:
#:     QML Image: Failed to get image from provider: image://metixel/d0d3dad8…
#:     ... Frame.qml:227:5
#:
#: and line 227 is ``Image { id: prevArtwork }``, the outgoing layer of a fade.
#: Pinning what the scene asked for is what makes that impossible.
SERVED_WINDOW = 6

#: Fallback frame rate when ``display.fps_limit`` is missing or non-positive.
#: Only the *scene tick* is paced by this — Qt Quick presents on vsync — but the
#: tick is what drives the slideshow state machine, so it must never be 0.
#: See :func:`tick_interval_ms`.
DEFAULT_FPS_LIMIT = 30

#: Tick rate used while a transition is on screen.
#:
#: The configured ``display.fps_limit`` (30 on the frame) is the right pace for a
#: still slide: nothing changes between ticks, and the idle cost is what the limit
#: exists to save. A fade changes on every tick, and at 30 Hz a slow dissolve steps
#: visibly — the panel refreshes at ~60 Hz, so each value is shown twice.
#:
#: 60 matches the panel's own refresh, so no tick is wasted and none is missing.
#: Affordable because these are property writes rather than paints: a measured
#: continuous 60 fps crossfade on a Pi 5 held 99.67% of frames vsync-locked for
#: 5.7% of one core.
TRANSITION_FPS = 60

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


class ArtworkStore:
    """Bounded store of decoded artwork that the scene may still be showing.

    Three rules, and the third is the one that actually makes a blank layer
    impossible:

    * **Bounded.** At most ``max_images`` entries are retained, so a caller that
      never releases cannot grow the process without limit. That was a real
      failure: roughly one image-sized leak per slide change reached ~1 GB in ten
      minutes and the OOM killer ended the frontend, over and over
      (``Out of memory: Killed process … anon-rss:1021456kB``).
    * **Never dropped while the scene can still ask for it.** Eviction by age
      alone is not enough — ``prevArtwork`` re-requests its source for the whole
      of a crossfade, so a handle the presenter has finished with must outlive
      its own eviction by ``served_window`` requests. See :data:`SERVED_WINDOW`.
    * **Recoverable.** The window above is a *heuristic*: it covers the requests
      the scene is known to make, and it is not a proof that the scene will never
      name a key again. It cannot be, because ``ImageCache`` (3 entries) and this
      store (6) are bounded independently and neither knows the other's budget —
      so a release can arrive from a direction the window never anticipated.
      Every entry therefore retains the **source** it was decoded from, and
      :meth:`serve` re-decodes on a miss instead of returning nothing.

    The third rule is what the first two could never guarantee. Without it the
    failure is not a dropped frame: the miss returns a null image, Qt retries the
    same key, and the layer stays blank until its ``source`` changes. Retaining a
    ``Path``/``bytes`` per entry is a few dozen bytes against a multi-megabyte
    ``QImage``, so the cost is negligible and the ceiling still holds.

    Values are opaque, which is deliberate: it makes the whole policy testable
    without Qt, so it runs in CI rather than only on a frame with PySide6.
    """

    def __init__(
        self,
        max_images: int = MAX_STORED_IMAGES,
        served_window: int = SERVED_WINDOW,
        decode: Callable[[Any], Any] | None = None,
    ) -> None:
        self._max_images = max_images
        self._served_window = served_window
        self._decode = decode
        self._images: OrderedDict[str, Any] = OrderedDict()
        self._sources: dict[str, Any] = {}
        self._served: OrderedDict[str, None] = OrderedDict()
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    # -- queries -------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._images)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._images

    # -- mutations -----------------------------------------------------------

    def add(self, key: str, value: Any, source: Any = None) -> None:
        """Store *value* under *key*, keeping *source* for a later re-decode."""
        with self._lock:
            self._images[key] = value
            self._images.move_to_end(key)
            if source is not None:
                self._sources[key] = source
            self._trim()

    def serve(self, key: str) -> Any | None:
        """Return *key*'s value and pin it as recently requested.

        On a miss the entry is re-decoded from the source retained by :meth:`add`
        and served as if it had never gone. ``None`` is therefore returned only
        when the key was never known, or has no source to recover from — which
        makes a blank layer impossible for any handle the presenter legitimately
        holds.

        Pinning here is what stops a live layer from being dropped.
        """
        with self._lock:
            value = self._images.get(key)
            if value is None:
                recovered = self._recover(key)
                if recovered is None:
                    return None
                value = recovered
            self._images.move_to_end(key)
            self._served[key] = None
            self._served.move_to_end(key)
            while len(self._served) > self._served_window:
                self._served.popitem(last=False)
            self._trim()
            return value

    def release(self, key: str) -> None:
        """Hand *key* back, unless the scene has served it recently.

        A served key becomes *pending* and is dropped when it leaves the served
        window; anything nobody asked for goes immediately. A dropped key keeps
        its source, so :meth:`serve` can still recover it.
        """
        with self._lock:
            if key in self._served:
                self._pending.add(key)
                return
            self._images.pop(key, None)

    def clear(self) -> None:
        """Drop everything, pins and sources included (teardown only)."""
        with self._lock:
            self._images.clear()
            self._sources.clear()
            self._served.clear()
            self._pending.clear()

    # -- policy --------------------------------------------------------------

    def _recover(self, key: str) -> Any | None:
        """Re-decode *key* from its retained source. Caller holds the lock.

        The decode callback is supplied by the backend, so this class stays free
        of Qt and testable without it. A source that has become unreadable (the
        file was deleted, the cache was cleared) simply yields ``None`` again,
        which is the same outcome as before this existed.
        """
        source = self._sources.get(key)
        if source is None or self._decode is None:
            return None
        try:
            value = self._decode(source)
        except Exception:
            logger.debug("Could not re-decode artwork %s", key, exc_info=True)
            return None
        if value is None:
            return None
        self._images[key] = value
        logger.debug("Artwork store re-decoded %s on demand", key)
        return value

    def _trim(self) -> None:
        """Honour deferred releases, then the cap. Caller holds the lock."""
        for key in [k for k in self._pending if k not in self._served]:
            self._pending.discard(key)
            self._images.pop(key, None)
        while len(self._images) > self._max_images:
            victim = next((k for k in self._images if k not in self._served), None)
            if victim is None:
                # Every entry is pinned, so the served window is the bound instead:
                # it is at most `served_window` images, not an unbounded set.
                break
            del self._images[victim]
            logger.debug("Artwork store evicted %s (cap %d)", victim, self._max_images)


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
        # Insertion-ordered, capped AND scene-aware — see ArtworkStore. The store
        # owns a strong reference to every image it hands out, so it holds its own
        # lock and enforces both the cap and the served window.
        # ``decode`` is what makes a miss recoverable rather than blank: the store
        # hands the retained source back through the SAME decoder, so a recovered
        # image is identical to the one that was evicted.
        self._images = ArtworkStore(decode=self._decode)

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

        # The incoming ambient layer's source is resolved ONCE per backdrop and
        # then held, so a blur that finishes mid-fade cannot swap the background
        # out from under the image fading in over it.  Keyed by job_id so a real
        # change of item still re-resolves — see ``_apply_backdrop``.
        self._ambient_pin_job: str | None = None
        self._ambient_pin_path: Path | None = None

        # Whether the last presented frame was a blend, which ``_pace_tick`` uses
        # to choose the tick rate.  Set by the presentation entry points so the
        # backend does not have to ask the presenter about its own timing.
        self._transition_active = False

        # Tick intervals, resolved in ``schedule()`` from ``fps_limit``.
        self._tick_slow_ms = int(1000 / DEFAULT_FPS_LIMIT)
        self._tick_fast_ms = int(1000 / TRANSITION_FPS)
        self._tick_is_fast = False

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

        # Rotation is the COMPOSITOR's job, and nothing used to do it.  The old
        # comment here claimed the framing engine had already applied the rotation,
        # which is only true of the *layout* — `LayoutEngine` picks the portrait
        # preset, but the wlroots output still presents its native landscape mode,
        # so the whole frame rendered sideways and the setting appeared dead.
        #
        # Both halves are required and they are independent:
        #
        # * ``--transform`` rotates the compositor output (and therefore the video
        #   surface, which Qt cannot transform);
        # * Qt then needs the ROTATED size, or the scene is laid out for the
        #   unrotated panel and letterboxes inside the turned output.
        #
        # Applied here rather than on a config change because the change comes with
        # a backend restart already (``routes/config.py`` restarts on any
        # ``_DISPLAY_MODE_KEYS`` change, and clears the optimised-media cache because
        # every cached image was scaled for the old canvas) — so this runs once, on
        # the fresh process, at exactly the moment the new geometry takes effect.
        if rotation % 360 in (90, 270):
            self._width, self._height = self._height, self._width
        self._apply_rotation(rotation)

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

        **The interval is not fixed.** ``display.fps_limit`` (30 on the frame) is
        the right pace for a slide showing a still image — nothing changes between
        ticks, and the idle cost is what that limit exists to save. During a fade
        something changes on every tick, and at 30 Hz a slow dissolve visibly
        steps: the panel refreshes at ~60 Hz, so each opacity value is presented
        for two refreshes while the Python side recomputes it once. The symptom is
        proportional to the fade length — a 2.5 s window looks smooth, a 5 s one
        shows the steps — which is exactly what Dennis reported.

        So the tick accelerates to :data:`TRANSITION_FPS` while a transition is
        running and returns to the configured limit afterwards. That is affordable
        precisely because these are *property writes*, not paints: the measured
        cost of a continuous 60 fps crossfade on a Pi 5 is 99.67% of frames
        vsync-locked for 5.7% of one core, only 0.3% above the same scene with no
        blend at all. The higher rate is bounded to the fade window.
        """
        from PySide6.QtCore import QTimer

        if self._app is None:  # pragma: no cover - misuse
            logger.error("schedule() called before create() — no QGuiApplication")
            return

        timer = QTimer()
        self._timer = timer
        self._tick_slow_ms = tick_interval_ms(self._fps_limit)
        self._tick_fast_ms = tick_interval_ms(TRANSITION_FPS)
        self._tick_is_fast = False
        timer.setInterval(self._tick_slow_ms)

        def _on_tick() -> None:
            try:
                self._pace_tick(timer)
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

    def _pace_tick(self, timer: Any) -> None:
        """Raise the tick rate while a transition is on screen, lower it after.

        Called at the top of every tick, before the tick itself runs, so the rate
        for *this* frame already reflects whether the last one painted a blend.

        ``setInterval`` on a running ``QTimer`` restarts the current period, so the
        interval is only touched when the desired rate actually changes — otherwise
        every tick would reset its own timer and the period would stretch.
        """
        want_fast = self._transition_active
        if want_fast == self._tick_is_fast:
            return
        self._tick_is_fast = want_fast
        timer.setInterval(self._tick_fast_ms if want_fast else self._tick_slow_ms)
        logger.debug(
            "Tick rate %s (%d ms) — %s",
            "raised" if want_fast else "restored",
            self._tick_fast_ms if want_fast else self._tick_slow_ms,
            "a transition is running" if want_fast else "no transition",
        )

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
        # A single layer means no fade is running, so the tick can go back to the
        # configured rate — see ``_pace_tick``.
        self._transition_active = False
        # Unpinned: a backdrop arriving after this frame is still adopted, which is
        # what stops a Next press onto an un-built blur leaving a flat band up.
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
            root.setProperty("prevBackdropOpacity", 0.0)
        else:
            root.setProperty("prevArtworkSource", self._url(prev_image))
            # The outgoing GROUP fades with ``prev_alpha`` — the transition
            # engine's own value for the outgoing layer.
            #
            # This used to be forced to 0.0 whenever the media differed, on the
            # reasoning that the outgoing item "stays opaque and is covered, not
            # dissolved".  Collapsing to 0.0 does the opposite of covering: it
            # deletes the outgoing item outright, so from the first frame of the
            # fade the only thing left under the incoming image is the flat
            # ``background`` Rectangle.  The incoming BLUR then fades in with its
            # group while the flat colour underneath does not move at all — which
            # is the reported defect, "the blurred background is transitioning at a
            # different rate to the main image".
            #
            # Using ``prev_alpha`` is what the scene's Layer 2 note describes: for
            # a crossfade it is 1.0 for the whole window (the outgoing layer is
            # meant to stay opaque and be covered from above), and for
            # ``fade_through_black`` it genuinely ramps down to 0.  So one value
            # covers both styles with no special case — the group fades exactly
            # when, and as much as, the engine says the outgoing layer should.
            root.setProperty("prevBackdropOpacity", float(prev_alpha))
            root.setProperty("prevArtworkOpacity", float(prev_alpha))
            self._apply_rect(root, "prevArtwork", prev_plan.artwork_dst)
            self._apply_source_rect(root, "prevArtwork", prev_plan.artwork_src)

        # A blend is on screen, so run the tick at the display's refresh rate until
        # it is not — a 30 Hz tick makes a long dissolve step visibly.  See
        # ``_pace_tick``.
        self._transition_active = True

        # Pinned: this runs on every frame of the fade, so the background must not
        # change identity part-way through it.
        self._apply_backdrop(root, plan, backdrop_source, pin=True)
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
        # The source travels WITH the image.  It is a ``Path``, ``bytes`` or numpy
        # array — tens of bytes against a multi-megabyte ``QImage`` — and it is what
        # lets the store re-decode if the scene asks for a key that has been
        # released, instead of handing back a null image and leaving the layer
        # blank.
        self._images.add(key, image, source=path)
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
        """Release an ``image://metixel/<key>`` handle.

        The store decides when it can actually go: a handle the scene has served
        recently is held until it leaves the served window, so releasing one
        cannot blank a layer that is still on screen (see :class:`ArtworkStore`).
        """
        key = self._key_of(handle)
        if key is None:
            return
        self._images.release(key)

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

    def _apply_backdrop(
        self,
        root: QQuickWindow,
        plan: RenderPlan,
        source: Any,
        *,
        pin: bool = False,
    ) -> None:
        """Point the ambient layer at the blurred JPEG, or fall back to flat colour.

        When *pin* is set a **fade is in flight**, and the source is frozen for the
        rest of it: a blur finishing part-way through must not swap the background
        out from under the image fading in over it.  ``present_transition`` passes
        ``pin=True`` on every frame of the fade, so without this a re-resolve would
        repoint the layer mid-fade — the "blurred background pops in while the
        image is still fading in" defect.

        A plain ``present`` leaves ``pin`` false, which is what lets a backdrop
        that arrives AFTER a cut still be adopted.  Freezing it there too would
        leave the flat band on screen for the rest of the slide, which is worse
        than the pop it was avoiding.

        The pin is keyed to the request's ``job_id``, so a genuine change of item
        re-resolves immediately even mid-fade — only the outcome for one backdrop
        is frozen.
        """
        # The canvas background follows the INCOMING item, so once the outgoing
        # group's opacity reaches 0 the frame is standing on the new colour rather
        # than the old one.  While the outgoing item is still opaque it covers this
        # completely, which is why it is safe to move at the start of the fade.
        root.setProperty("ambientColour", _colour(plan.ambient_colour))
        request = self.backdrop_request(plan, source)
        job_id = request.job_id if request is not None else None

        if not pin or job_id != self._ambient_pin_job:
            # Either nothing is fading (follow the live state, and keep the pin in
            # step so the next fade starts from what is actually on screen), or the
            # item changed under a running fade (the old pin describes a different
            # backdrop, so re-resolve for this one).
            self._ambient_pin_job = job_id
            self._ambient_pin_path = (
                self._backdrop_adopted.get(job_id) if job_id is not None else None
            )

        path = self._ambient_pin_path
        if path is None:
            # Flat ambient fill. Correct, not a failure: it is what a non-blur plan
            # asks for, and what a plan whose blur cannot be built falls back to.
            root.setProperty("ambientSource", self._url(None))
            root.setProperty("ambientVisible", True)
            return
        root.setProperty("ambientSource", self._url(path))
        root.setProperty("ambientVisible", True)

    def _apply_prev_backdrop(
        self, root: QQuickWindow, prev_plan: RenderPlan | None, source: Any
    ) -> None:
        if prev_plan is None:
            root.setProperty("prevAmbientVisible", False)
            return
        # Colour AND geometry both follow the outgoing plan: the group is only
        # faded, never re-sourced, so the backdrop it was built for has to still be
        # the one it paints.
        root.setProperty("prevAmbientColour", _colour(prev_plan.ambient_colour))
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

    def _apply_rotation(self, rotation: int) -> None:
        """Rotate the compositor output, or put it back to normal.

        Never raises and never aborts startup.  ``reconcile.sh`` owns persistent
        host state, so a rotation applied from the application is a runtime action
        the user asked for — and a frame that cannot rotate (no wlr-randr, no
        Wayland socket, an output that will not accept the transform) must still
        come up.  The failure mode is the same in every case: the panel stays
        unrotated, and that is what the log records.
        """
        if rotation % 360 not in (0, 90, 180, 270):
            logger.warning("Ignoring unusable display rotation %r", rotation)
            return
        try:
            applied = self._wlr().set_mode(rotation=rotation)
        except Exception:  # noqa: BLE001 - startup must not fail on this
            logger.warning("Display rotation %d failed", rotation, exc_info=True)
            return
        if not applied:
            logger.warning(
                "Display rotation %d could not be applied — the panel will stay "
                "unrotated (is wlr-randr available and the output connected?)",
                rotation,
            )

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
    per request would defeat the purpose of pre-decoding it.  "Does not take
    ownership" is also why the store is capped: the decoded image outlives every
    request for it until :meth:`QmlBackend.unload_image` drops that handle.
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
                # serve() pins the id as recently requested AND returns the stored
                # image. That pin is what keeps a layer the scene is still painting
                # from being evicted underneath it -- see ArtworkStore.
                image = backend._images.serve(image_id)  # noqa: SLF001 - owner-private
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

    All three kinds cross the boundary, because the scene draws all three:
    ``kind`` selects which delegate draws, ``rect`` becomes the x/y/w/h the scene
    positions with, and ``source``/``rotation`` carry the boot logo and the
    spinner.

    This mapping is the whole overlay: get it wrong and the failure is silent and
    total.  An earlier version read ``getattr(element, "x")`` and ``"opacity"`` —
    fields :class:`OverlayElement` does not have — so every element arrived with
    x=0, y=0 and zero geometry, and the scene drew each one as a ``Text`` whose
    string was empty.  The boot screen is a black rect, a logo, a rotating spinner
    and two progress rects — **no text at all** — so it painted nothing whatsoever,
    and a notification lost its panel and degenerated to bare text piled up in the
    top-left corner.  Neither raised: a blank overlay is a perfectly valid frame.

    ``element.image`` is whatever ``DisplayBackend.load_image`` returned, which for
    this backend is an ``image://`` URL string.  Anything else is not something QML
    can resolve, so it maps to an empty source and blanks only its own element
    rather than the layer.
    """
    x, y, width, height = element.rect
    source = element.image if isinstance(element.image, str) else ""
    return {
        "kind": str(element.kind),
        "x": float(x),
        "y": float(y),
        "w": float(width),
        "h": float(height),
        "colour": _colour(element.colour),
        # ``alpha``, not ``opacity``: the field is named alpha on the element and
        # the scene reads it as opacity.  ``__post_init__`` guarantees it is a
        # float in 0..1, so this needs no defaulting — and must not use ``or``,
        # which would turn a deliberately invisible element into a solid one.
        "alpha": float(element.alpha),
        "text": str(element.text or ""),
        "size": int(element.size or 0) or 24,
        "source": source,
        "rotation": float(element.rotation),
    }
