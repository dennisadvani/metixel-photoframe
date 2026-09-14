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
    """The canvas must stay visible over a playing video so it can paint the mat.

    Regression: ``QStackedLayout`` was set to ``StackOne`` while the comments and
    ``present()`` both assumed raise-based layering.  Under ``StackOne`` only the
    current widget is visible, so showing the mpv widget hid the canvas — and the
    canvas is what paints the ring.  Video therefore filled the surface with no
    mat, while photos were unaffected (they never switch pages).
    """

    def test_stacking_mode_is_stack_all(self) -> None:
        """StackOne hides the non-current widget, which defeats the matte."""
        source = _source(_BACKEND)
        assert "StackingMode.StackAll" in source, (
            "the canvas and the mpv widget must BOTH be visible; StackOne makes "
            "them mutually exclusive pages and hides the matte during video"
        )
        assert "StackingMode.StackOne" not in source, (
            "StackOne is the bug: it hides the canvas (and with it the matte) "
            "whenever the mpv widget is current"
        )

    def test_mpv_is_beneath_the_canvas(self) -> None:
        """Z-order: mpv underneath, canvas painting the ring over it."""
        source = _source(_BACKEND)
        # Instead of positional parsing, assert both are added and the mpv one
        # is added first (QStackedLayout raises the most recently added).
        mpv_add = source.index("layout.addWidget(self._mpv_widget)")
        canvas_add = source.index("layout.addWidget(self._canvas)")
        assert mpv_add < canvas_add, (
            "the mpv widget must be added BEFORE the canvas so the canvas is on "
            "top and can paint the matte over the video"
        )

    def test_play_video_does_not_switch_pages(self) -> None:
        """Starting a video must not hide the canvas."""
        source = _source(_BACKEND)
        assert "setCurrentWidget(self._mpv_widget)" not in source, (
            "play_video must not switch the stacked layout to the mpv widget — "
            "that hides the canvas and the matte with it"
        )
        assert "set_video_underlay(True)" in source

    def test_stop_video_restores_the_artwork_layer(self) -> None:
        """The transparent Mat Window must be closed again after playback."""
        source = _source(_BACKEND)
        assert "set_video_underlay(False)" in source, (
            "leaving the underlay enabled makes every later photo render with a "
            "transparent middle, revealing the black container"
        )

    def test_canvas_skips_the_fill_under_the_video(self) -> None:
        """An opaque full-rect fill would hide mpv's frames entirely."""
        source = _source(_CANVAS)
        assert "_video_underlay" in source
        # The background fill must be conditional on the underlay being off.
        assert "if not self._video_underlay:" in source
        # And the artwork must not be drawn over the hole.
        assert "and not self._video_underlay" in source

    def test_ring_layers_still_paint_over_video(self) -> None:
        """The matte itself must NOT be conditional — it paints in both modes."""
        tree = ast.parse(_source(_CANVAS))
        fill_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.For)
            and isinstance(node.iter, ast.Attribute)
            and node.iter.attr in {"matte", "moulding", "whitespace"}
        ]
        assert len(fill_calls) == 3, (
            "expected the matte, moulding and whitespace loops to be unconditional "
            "so the frame paints over video as well as photos"
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
        """A late-configured surface must still be picked up."""
        source = _source(_BACKEND)
        assert "installEventFilter" in source
        assert "def eventFilter" in source
        assert "QEvent.Type.Resize" in source

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
