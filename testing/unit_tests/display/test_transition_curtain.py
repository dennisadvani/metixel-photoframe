# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for the transition curtain in ``FrameCanvas``.

In ``contain`` the two items are laid out independently, so their artworks occupy
different rectangles.  The crossfade draws the outgoing layer once over the whole
of ITS rect at full opacity — it has to, because in the overlap it must stay
opaque or the blend double-counts transparency and the panel dims through the
middle.  That leaves the part of the outgoing artwork the incoming one never
reaches (its letterbox bars) at a steady 100% for the whole transition, and then
``_advance()`` drops the outgoing layer and the residue SNAPS away in one frame.

The curtain fixes that by covering the residue in the incoming item's ambient
colour at the incoming item's alpha: covering with colour ``c`` at alpha ``a`` is
equivalent to fading what is there by ``(1 - a)``.

Two properties matter, and both are easy to get wrong:

1. It must be CLIPPED to the incoming artwork's complement.  Unclipped it would
   sit in front of the outgoing layer in the overlap too, giving
   ``in*t + out*(1-t)^2`` — a 25% dim at the midpoint, measured on a Pi 5 as
   luminance 73.25 where 112.7 was correct.
2. It must be a NO-OP for a full-bleed frame, or ``cover`` (the default, and the
   pinned slideshow style) would pay for a fix it does not need.

These are structural assertions over the source, because CI runs with no Qt
installed.  Anything needing Qt uses ``pytest.importorskip``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_DISPLAY_DIR = Path(__file__).resolve().parents[3] / "src" / "metixel" / "display"
_CANVAS = _DISPLAY_DIR / "qt_canvas.py"
_GEOMETRY = _DISPLAY_DIR / "geometry.py"
_PRESENTER = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "metixel"
    / "frontend"
    / "presentation"
    / "presenter.py"
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _method_body(path: Path, name: str) -> str:
    source = _source(path)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} not found in {path.name}")


class TestCurtainIsClipped:
    """An unclipped curtain is the 25%-dim bug arriving by a different route."""

    def test_clipped_to_the_incoming_artwork_complement(self) -> None:
        body = _method_body(_CANVAS, "_paint_transition_curtain")
        assert "setClipRegion" in body, "the curtain must be clipped, or it dims the overlap"
        assert "subtracted" in body, "the clip is the complement of the incoming artwork"
        assert "plan.artwork_dst" in body, "the complement is taken from artwork_dst"

    def test_opacity_is_the_incoming_alpha(self) -> None:
        """The residue must ramp with the INCOMING alpha, not the outgoing one."""
        source = _source(_CANVAS)
        assert "self._paint_transition_curtain(painter, plan, self._image_alpha)" in source, (
            "the curtain shares the incoming image's alpha"
        )

    def test_uses_the_incoming_plans_ambient_colour(self) -> None:
        body = _method_body(_CANVAS, "_paint_transition_curtain")
        assert "plan.ambient_colour" in body, (
            "the residue must be wiped to the ambient colour, not hardcoded black"
        )


class TestCurtainIsANoOpForFullBleed:
    """``cover`` must not pay for a ``contain`` fix."""

    def test_returns_early_when_the_artwork_fills_the_screen(self) -> None:
        body = _method_body(_CANVAS, "_paint_transition_curtain")
        assert "plan.artwork_dst == plan.screen" in body, (
            "a full-bleed frame has no residue to wipe"
        )

    def test_returns_early_when_the_clip_is_empty(self) -> None:
        body = _method_body(_CANVAS, "_paint_transition_curtain")
        assert "region.isEmpty()" in body

    def test_only_runs_during_a_transition(self) -> None:
        """It is gated on there being an outgoing layer, like the crossfade."""
        paint = _method_body(_CANVAS, "paintEvent")
        assert "self._paint_transition_curtain" in paint
        index = paint.index("self._paint_transition_curtain")
        preceding = paint[:index]
        assert "self._prev_plan is not None and self._prev_alpha > 0.01" in preceding, (
            "the curtain must be guarded by the same condition as the outgoing layer"
        )

    def test_drawn_after_the_outgoing_and_before_the_incoming(self) -> None:
        """Order is load-bearing: wipe the residue, then lay the incoming over it."""
        paint = _method_body(_CANVAS, "paintEvent")
        curtain = paint.index("_paint_transition_curtain")
        assert paint.index("self._draw_artwork(painter, self._prev_plan") < curtain
        assert paint.index("self._draw_artwork(painter, plan)") > curtain


class TestAmbientColourPlumbing:
    """The curtain is only as correct as the colour it is handed."""

    def test_presenter_normalises_both_config_forms(self) -> None:
        """A hex string and an ``[r, g, b]`` list must both work.

        The card's colour picker writes hex while the neighbouring
        ``matte_color`` key is a list, so a hand-edited config must not silently
        fall back to the default — a wrong curtain colour shows as a flicker at
        the edge of a letterboxed photo, which does not point at the colour.
        """
        body = _method_body(_PRESENTER, "_ambient_colour")
        assert 'startswith("#")' in body, "hex form"
        assert "isinstance(value, (list, tuple))" in body, "array form"
        assert "_DEFAULT_AMBIENT_COLOUR" in body

    def test_layout_engine_forwards_the_colour_to_every_plan(self) -> None:
        engine_body = _method_body(
            Path(__file__).resolve().parents[3] / "src" / "metixel" / "framing" / "layout.py",
            "compute",
        )
        assert "ambient_colour=self._ambient_colour" in engine_body, (
            "every plan must carry the configured colour, or the curtain uses the default"
        )


def test_the_rect_rounding_is_shared_and_rounds_outwards() -> None:
    """A curtain clipped a pixel INTO the artwork shows as a line down its edge.

    ``QRegion`` is integer-only, so the rect is rounded outwards: over-wiping by
    a pixel is invisible, under-wiping leaves a visible seam.  The rule lives in
    ``display.geometry`` because the mpv widget is positioned from the SAME rect
    — see ``test_geometry.py`` for its behaviour.
    """
    assert "int(x + w + 0.9999)" in _source(_GEOMETRY), "round the far edge outward, not to nearest"
    # ...and the canvas consumes it rather than restating the rule.
    assert "def _qr" in _source(_CANVAS)
    assert "int_rect(rect)" in _source(_CANVAS)


@pytest.mark.parametrize("name", ["_paint_transition_curtain", "_qr"])
def test_helpers_exist(name: str) -> None:
    assert name in _source(_CANVAS)
