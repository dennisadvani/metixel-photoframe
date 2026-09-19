# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for the Qt + mpv surface composition.

These assert invariants that were each violated by a real bug found on a Pi 5,
and each bug had a symptom that did not point at its cause:

* video filled the whole screen with no virtual mat (the canvas was hidden);
* the recorded resolution was intermittently wrong (a single early read);
* playback appeared to lock up when a video ended (a stale EOF flag).

They are **structural** assertions over the source rather than rendered output,
because CI runs with no Qt installed at all. That is a deliberate trade: a
structural test cannot prove the matte looks right, but it does prove the
stacking mode and the z-order that make the matte possible are still in place,
which is exactly the part that silently regressed.

Anything genuinely needing Qt must use ``pytest.importorskip`` so CI keeps
working.
"""

from __future__ import annotations

import ast
from pathlib import Path

from metixel.display.geometry import int_rect
from metixel.display.qt_backend import _artwork_rect
from metixel.framing.layout import RenderPlan

_DISPLAY_DIR = Path(__file__).resolve().parents[3] / "src" / "metixel" / "display"
_BACKEND = _DISPLAY_DIR / "qt_backend.py"
_CANVAS = _DISPLAY_DIR / "qt_canvas.py"
_MPV = _DISPLAY_DIR / "qt_mpv.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestVideoSurfaceComposition:
    """A video must sit inside the SAME frame a photo would.

    Wrong in four different ways, each with a symptom that did not point at its
    cause, so the guards below pin the STRUCTURE rather than any particular
    idiom:

    1. ``QStackedLayout(StackOne)`` — only the current widget is visible, so
       bringing the mpv widget forward hid the canvas that painted the ring.
       Video filled the surface with no matte; photos were unaffected because
       they never switch pages.
    2. ``QStackedLayout(StackAll)`` — both visible, but the swap path wedged: the
       main thread sat in ``QWaylandWindow::waitForFrameSync`` burning a core, so
       the event loop never returned to the presenter and the slideshow froze on
       the first video.
    3. Canvas painting the ring over mpv through a transparent hole — a
       ``WA_OpaquePaintEvent`` widget that leaves part of itself unpainted gives
       undefined framebuffer content, which Qt rendered as a solid black
       rectangle where the video should be.
    4. Working around (3) by having the mpv widget paint its own matte.  That
       restored the ring but put the frame geometry in TWO places and left the
       video unable to carry the ambient fill, the fit modes or a crossfade: a
       video-shaped hole with a ring around it, not a framed item.

    The resolution is that (3) was the paint ATTRIBUTE, not the architecture.  A
    PHASE-0 spike on a Pi 5 (cage/Wayland, Qt 6.8.2) rendered a partial hole
    correctly over a raster sibling, over a ``QOpenGLWidget``, and over the real
    ``MpvRenderWidget`` while playing — see ``scripts/dev/_spike_video_hole.py``.

    So the canvas owns the frame again: it paints every layer except the artwork
    rectangle, and the mpv widget is sized to exactly that rectangle underneath.
    """

    def test_uses_no_layout_manager(self) -> None:
        """Both surfaces must OVERLAY, which a layout manager cannot do here.

        A QVBoxLayout *allocates* space, so two visible children tile vertically
        at half height each — that was "the slideshow is drawn halfway down the
        screen", and it put the canvas's artwork hole nowhere near the video so
        the matte read as solid black.

        A QStackedLayout has the opposite problem: it makes children mutually
        exclusive pages, which hid the canvas entirely (no matte) and then wedged
        the Wayland swap path when both were forced visible.

        So neither is used: geometry is set explicitly in _relayout() and z-order
        is decided by raise_().
        """
        source = _source(_BACKEND)
        assert "QVBoxLayout" not in source or "# A QVBoxLayout" in source, (
            "QVBoxLayout tiles children instead of overlaying them"
        )
        assert "QStackedLayout(" not in source, "QStackedLayout makes pages, not an overlay"
        assert "def _relayout" in source, "geometry must be set explicitly"
        assert "setGeometry(rect)" in source

    def test_both_surfaces_get_the_full_container_geometry(self) -> None:
        """_relayout must size BOTH children, not just the canvas."""
        source = _source(_BACKEND)
        # Locate the relayout body.  The window must be generous: the docstring
        # alone is longer than a naive slice, which made an earlier version of
        # this test fail against correct code.
        start = source.index("def _relayout")
        body = source[start : start + 2500]
        assert "self._mpv_widget" in body, "the mpv widget must be sized too"
        assert "self._canvas" in body, "the canvas must be sized"
        assert "target.rect()" in body

    def test_the_mpv_widget_paints_no_ring(self) -> None:
        """The widget owns the video and nothing else.

        Ring geometry has exactly one owner — the canvas.  A second painter in
        the mpv widget would be positioned from the same plan by different code,
        and the two would eventually disagree.
        """
        source = _source(_MPV)
        assert "set_matte" not in source
        assert "_paint_matte_over_video" not in source
        # What it must expose instead: readiness for the hole, and the fit.
        assert "def video_ready" in source
        assert "def set_panscan" in source

    def test_the_canvas_holes_the_artwork_while_a_video_plays(self) -> None:
        """The base must be clipped out of the artwork rect and the artwork skipped."""
        body = _method_body(_CANVAS, "paintEvent")
        assert "self._video_surface" in body, "paintEvent must honour the mode"
        assert "subtracted" in body, "the base must be clipped out of the hole"
        assert body.count("if not self._video_surface:") == 2, (
            "BOTH artwork draws (outgoing and incoming) must be skipped, or the "
            "hole is painted over"
        )
        # The rings are still painted: they are disjoint annuli, so they belong
        # on top of the video exactly as they are on top of a photo.
        for layer in ("plan.whitespace", "plan.matte", "plan.moulding"):
            assert layer in body, f"{layer} must still paint over the video"

    def test_stop_video_releases_the_hole(self) -> None:
        """A stale hole must not survive into the next photo.

        Left in video-surface mode, the following photo would be painted around an
        unpainted rectangle — showing whatever the mpv widget left behind.
        """
        body = _method_body(_BACKEND, "stop_video")
        assert "set_video_surface(False)" in body
        assert "_video_geometry = None" in body, "the artwork-sized rect must be dropped"

    def test_the_hole_opens_only_once_the_video_has_a_frame(self) -> None:
        """Opening it at play() time showed a black rectangle until the first frame.

        ``video_ready()`` was written for exactly this and then never called by
        anything, so the hole opened immediately and the mpv widget's uninitialised
        framebuffer showed through — the black flash at the start of every video.
        The poster is already painted, so holding the hole until mpv has a frame
        costs nothing and removes the flash.
        """
        play = _method_body(_BACKEND, "play_video")
        assert "set_video_surface(True)" not in play, "the hole must not open at play time"
        assert "_video_revealed = False" in play

        assert "_reveal_video_surface_when_ready()" in _method_body(_BACKEND, "present")

        reveal = _method_body(_BACKEND, "_reveal_video_surface_when_ready")
        assert "video_ready()" in reveal
        assert "set_video_surface(True)" in reveal

    def test_the_mpv_widget_covers_exactly_the_artwork_rect(self) -> None:
        """The widget must FILL the canvas's hole, not letterbox inside it.

        They are siblings, so any disagreement is a black seam at the artwork
        edge.  ``panscan`` is not only "cover": the widget is placed at
        ``int_rect(artwork_dst)``, which rounds the far edge OUTWARD, so the rect
        can be a pixel wider than the media's exact fit.  For a portrait video on
        a 1200px-tall panel the fit is 675.0px and the rect comes out 676px;
        letterboxing that leaves one black column at the right edge (measured on
        the Pi: x=1297 pure black, ambient blur resuming at x=1298).  Filling the
        rect instead costs a 0.15% scale difference and removes the seam.
        """
        body = _method_body(_BACKEND, "_apply_video_geometry")
        assert "setGeometry(*rect)" in body, "the widget is placed at the artwork rect"
        assert "_artwork_rect(plan)" in body
        assert "set_panscan(True)" in body, "the video must FILL the hole, not letterbox in it"
        # And the rect is the shared conversion, not a second copy of the rule.
        assert "int_rect(plan.artwork_dst)" in _source(_BACKEND)


class TestSurfaceSizeDetection:
    """The reported resolution must not latch a placeholder value.

    Regression: ``create()`` read ``container.width()/height()`` once, immediately
    after ``show()``.  A Wayland surface is configured asynchronously, so that
    read can observe a placeholder (seen on a Pi 5 as ``200x100``).  Nothing
    updated the value afterwards, so the wrong size drove mat geometry and the
    dashboard's Display card until the next restart.
    """

    def test_size_settles_before_being_trusted(self) -> None:
        """The settle loop must actually be *called*, not merely defined.

        Asserting the name appears in the file is not enough: the helper stays
        defined when its call is removed, so that version of the test passed even
        with the bug reintroduced.  Check for the call site.
        """
        source = _source(_BACKEND)
        assert "self._wait_for_stable_size(container)" in source, (
            "create() must call the settle loop; defining it without calling it "
            "leaves the single-early-read bug in place"
        )
        assert "def _wait_for_stable_size" in source

    def test_size_is_re_read_on_resize(self) -> None:
        """A late-configured surface must still be picked up.

        Regression: the first attempt installed an event filter with
        ``container.installEventFilter(self)``. The backend is NOT a ``QObject``,
        so PySide6 raised

            TypeError: installEventFilter called with wrong argument types
              PySide6.QtCore.QObject.installEventFilter(PySide6Backend)
              Supported signatures: installEventFilter(QObject, /)

        and because ``create()`` runs before the render loop, that escaped as a
        startup failure and the service crash-looped 39 times.

        The fix is a QObject factory, so the invariant is about the ARGUMENT, not
        about whether ``installEventFilter`` is called at all.
        """
        source = _source(_BACKEND)

        assert "_make_resize_filter" in source, (
            "the resize filter must be built by the QObject factory"
        )
        assert "class _ResizeFilter(QObject)" in source, "the filter must be a QObject"
        assert "installEventFilter(self)" not in source, (
            "passing the backend (a plain class) as an event filter raises "
            "TypeError at startup and crash-loops the service"
        )

        # With no layout manager, the resize handler must keep both children
        # container-sized explicitly.
        assert "def _relayout" in source
        assert "setGeometry(rect)" in source

    def test_no_qt_object_is_used_as_an_event_filter(self) -> None:
        """PySide6Backend must not become a QObject just to satisfy a filter.

        It derives from ``DisplayBackend``, an ABC.  Making it a QObject would
        drag the Qt object model into a class that must stay importable without
        Qt — CI has none, and ``qt_backend`` is imported from the display factory.
        """
        tree = ast.parse(_source(_BACKEND))
        backend_cls = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "PySide6Backend"
        )
        base_names = {b.id for b in backend_cls.bases if isinstance(b, ast.Name)} | {
            b.attr for b in backend_cls.bases if isinstance(b, ast.Attribute)
        }
        assert "QObject" not in base_names, (
            "PySide6Backend must not derive from QObject; route resize through "
            "the QObject filter factory instead"
        )

    def test_absurd_size_is_treated_as_unknown(self) -> None:
        """A tiny placeholder must not be accepted as the real resolution."""
        source = _source(_BACKEND)
        assert "_read_surface_size" in source
        # The guard against a too-small reading must exist.
        assert "w < 100 or h < 100" in source

    def test_resize_republishes_display_info(self) -> None:
        """A settled size must reach the daemon and the web UI."""
        source = _source(_BACKEND)
        assert "_publish_display_info" in source
        # Atomic write, per the project's config rule — never a partial file.
        assert "replace(" in source


class TestVideoEofHandling:
    """Reaching EOF must not wedge the next item.

    Regression: ``stop()`` cleared the play flags but not ``_eof``, so
    ``is_finished()`` kept returning True.  The presenter then ran
    ``_video_finished()`` again immediately, and the visible symptom was playback
    appearing to lock up rather than a stale boolean.
    """

    def test_stop_clears_the_eof_flag(self) -> None:
        """A stopped player must not report itself finished."""
        tree = ast.parse(_source(_MPV))
        stop_fn = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "stop"
            ),
            None,
        )
        assert stop_fn is not None, "MpvRenderWidget.stop must exist"
        assigns = {
            target.attr
            for node in ast.walk(stop_fn)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }
        assert "_eof" in assigns, (
            "stop() must reset _eof, or the NEXT video reports itself finished "
            "immediately and is skipped"
        )
        assert "_playing" in assigns

    def test_eof_triggers_a_repaint(self) -> None:
        """Without a repaint the widget keeps showing the last decoded frame."""
        tree = ast.parse(_source(_MPV))
        eof_fn = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "_on_eof_reached"
            ),
            None,
        )
        assert eof_fn is not None
        emitted = [
            node
            for node in ast.walk(eof_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "emit"
        ]
        assert emitted, "the EOF callback must emit frame_ready so a repaint happens"

    def test_no_explicit_quit_is_sent_to_mpv(self) -> None:
        """Plain ``quit`` destroys the handle; only shutdown may do that."""
        source = _source(_MPV)
        # The current design uses stop() for both advance and EOF and terminate()
        # at shutdown, so an unbracketed `command("quit")` would break restarting.
        assert 'command("quit")' not in source


def test_qt_backend_is_importable_without_qt() -> None:
    """``qt_backend`` must stay importable on a machine with no Qt.

    This is the invariant that actually matters, and it is narrower than "no
    module-scope Qt imports anywhere".  ``qt_canvas.py`` and ``qt_mpv.py`` DO
    import PySide6 at module scope, and that is safe and intended: nothing
    reaches them until ``PySide6Backend.create()`` imports them inside the
    method, so they are only ever loaded when Qt is present.

    ``qt_backend`` is different — ``display/__init__.py`` imports it from within
    the factory functions, so it must survive being *parsed* by mypy and the
    import-graph tests on a Qt-free CI runner.  A top-level Qt import there would
    break CI, which is the signal this guards.
    """
    tree = ast.parse(_source(_BACKEND))
    for node in tree.body:  # module scope only
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("PySide6") for a in node.names), (
                "qt_backend.py must not import PySide6 at module scope — CI has "
                "no Qt and must still be able to import this module"
            )
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("PySide6"), (
                "qt_backend.py must not import PySide6 at module scope"
            )

    # And the canvas/mpv imports must stay inside create(), not hoisted.
    source = _source(_BACKEND)
    for lazy in (
        "from metixel.display.qt_canvas import FrameCanvas",
        "from metixel.display.qt_mpv import MpvRenderWidget",
    ):
        line = next(ln for ln in source.splitlines() if lazy in ln)
        indent = len(line) - len(line.lstrip())
        assert indent > 0, f"{lazy!r} must be indented (inside create()), not at module scope"


def _method_body(path: Path, name: str) -> str:
    """Source of one method, by name, from *path*."""
    source = _source(path)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} not found in {path.name}")


class TestRepaintIsRequestedOnlyOnChange:
    """Qt repaints when told to, and only then.

    So nothing may ask for a repaint it does not need, and nothing may take the
    repaint decision away from the object that knows what was last painted.
    Measured on a Pi 5, an unconditional 31 fps composite of an unchanging
    1920x1200 frame was 83% of a core, against 0.9% for an idle Qt event loop.
    """

    def test_canvas_setters_delegate_the_decision(self) -> None:
        """They must not assign the fields and repaint regardless."""
        for name in ("update_plan", "update_transition", "clear_plan"):
            body = _method_body(_CANVAS, name)
            assert "_store_layers(" in body, f"{name} must delegate to _store_layers"
            assert "self.update()" not in body, f"{name} must not repaint unconditionally"

    def test_canvas_overlay_compares_before_repainting(self) -> None:
        body = _method_body(_CANVAS, "update_overlay")
        assert "is self._overlay" in body, (
            "identity, not value: comparing OverlayElement images is per-pixel"
        )
        assert "self.update()" in body, "a changed overlay must repaint"

    def test_backend_never_repaints_or_restacks_unconditionally(self) -> None:
        for name in ("present", "present_transition", "present_overlay"):
            body = _method_body(_BACKEND, name)
            assert "self._canvas.update()" not in body, (
                f"{name} must let the canvas decide whether to repaint"
            )
            assert "self._canvas.raise_()" not in body, (
                f"{name} must not restack the surfaces every frame"
            )

    def test_an_empty_overlay_is_not_dropped(self) -> None:
        body = _method_body(_BACKEND, "present_overlay")
        assert "if not elements" not in body, (
            "an empty overlay is how the canvas is told to clear the last one"
        )

    def test_stop_video_still_repaints(self) -> None:
        """Leaving video mode must repaint, even if the mode was never toggled."""
        body = _method_body(_BACKEND, "stop_video")
        assert "set_video_surface(False)" in body
        assert "self._canvas.update()" in body


class TestVideoGeometry:
    """The mpv widget must cover exactly the rect the canvas leaves unpainted.

    They are SIBLING widgets, so a one-pixel disagreement is a black seam along
    the artwork edge — and a seam reads as a paint bug rather than an arithmetic
    one.  Both sides therefore use :func:`metixel.display.geometry.int_rect`.
    """

    @staticmethod
    def _plan(artwork_dst: tuple[float, float, float, float], overflow: str) -> RenderPlan:
        return RenderPlan(
            screen=(0.0, 0.0, 1920.0, 1200.0),
            ambient=None,
            artwork_dst=artwork_dst,
            artwork_src=(0.0, 0.0, 100.0, 100.0),
            whitespace=(),
            matte=(),
            moulding=(),
            matte_colour="#ffffff",
            whitespace_colour="#ffffff",
            ambient_colour="#101014",
            style="borderless",
            branch="virtual",
            overflow=overflow,
        )

    def test_the_rect_is_the_shared_rounding_of_the_artwork(self) -> None:
        plan = self._plan((7.4, 7.4, 1905.17, 1185.19), "crop")
        assert _artwork_rect(plan) == (7, 7, 1906, 1186)
        # Not a copy of the rule: the very same conversion the canvas uses.
        assert _artwork_rect(plan) == int_rect(plan.artwork_dst)
