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

_DISPLAY_DIR = Path(__file__).resolve().parents[3] / "src" / "metixel" / "display"
_BACKEND = _DISPLAY_DIR / "qt_backend.py"
_CANVAS = _DISPLAY_DIR / "qt_canvas.py"
_MPV = _DISPLAY_DIR / "qt_mpv.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestVideoMatteComposition:
    """The video must appear inside the same virtual mat as a photo.

    This has now been wrong in three different ways, each with a symptom that did
    not point at its cause, so the guards below pin the STRUCTURE that makes the
    matte work rather than any particular idiom:

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

    The working structure is the prototype's: a plain layout, and the widget that
    renders the video also paints its own matte.
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

    def test_the_mpv_widget_paints_its_own_matte(self) -> None:
        """One widget owning video AND matte is the whole point.

        Compositing the ring from a separate widget above the video requires a
        transparent hole, which Qt renders as black.  If this ever moves back out
        of MpvRenderWidget, the black rectangle returns.
        """
        source = _source(_MPV)
        assert "def _paint_matte_over_video" in source
        assert "def set_matte" in source
        # And it is actually called from the video paint path.
        assert "self._paint_matte_over_video(w, h)" in source

    def test_matte_bands_exclude_the_artwork_rect(self) -> None:
        """Only the ring layers are painted; the artwork rect is the video hole."""
        source = _source(_BACKEND)
        # The band builder must cover exactly the three ring layers.
        for layer in ("plan.whitespace", "plan.matte", "plan.moulding"):
            assert layer in source, f"{layer} must paint over the video"
        # artwork_dst must NOT be painted, or the video would be hidden.
        assert "_add(plan.artwork_dst" not in source

    def test_stop_video_clears_the_matte_bands(self) -> None:
        """A stale ring must not survive into the next photo."""
        source = _source(_BACKEND)
        assert "set_matte(None)" in source, (
            "stop_video must clear the bands, or the video's frame is left "
            "painted over the following photo"
        )

    def test_present_does_not_raise_the_canvas_over_video(self) -> None:
        """Raising the canvas during video would cover the frame."""
        source = _source(_BACKEND)
        # The video branch of present() must raise the mpv widget, not the canvas.
        assert "self._mpv_widget.raise_()" in source, (
            "present() must keep mpv on top while a video plays"
        )


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
        assert "set_overlay_only(False)" in body
        assert "self._canvas.update()" in body
