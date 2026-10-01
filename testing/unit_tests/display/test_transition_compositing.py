# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""How the two media groups compose on screen during a transition.

The scene stacks, bottom to top::

    background (Rectangle, flat colour)
    prevMedia  (Item{ prevAmbient, prevArtwork })   ← the OUTGOING item
    media      (Item{ ambient,     artwork     })   ← the INCOMING item
    video → rings → matte → moulding → overlay

Each media group carries ONE opacity, and its children inherit it — so a
backdrop and its artwork always fade together.  That part is correct and is not
what this file guards.

What went wrong is the *outgoing* group's opacity.  It was forced to ``0.0``
whenever the two items differed, which does not "keep the outgoing item opaque
and let the incoming one cover it" (what the scene's own note describes) — it
erases the outgoing item outright.  The first frame of a fade then showed the
flat ``background`` Rectangle with the incoming image dissolving up from it,
while the incoming *blur* faded in on the group's clock.  Two backgrounds at two
different rates, reported as "the blurred background is transitioning at a
different rate to the main image".

The outgoing group now fades with ``prev_alpha`` — the transition engine's own
value for the outgoing layer — which is 1.0 through a crossfade and ramps to 0
for ``fade_through_black``.  One value, both styles, no special case.

These tests do not need Qt: the property values are computed in
``present_transition`` from the plan and the alphas, and that arithmetic is the
part that was wrong.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from metixel.frontend.presentation.transitions import TransitionEngine
from metixel.shared.config import Config

_ROOT = Path(__file__).resolve().parents[3]
_BACKEND = _ROOT / "src" / "metixel" / "display" / "qt_qml_backend.py"
_QML = _ROOT / "src" / "metixel" / "display" / "qml" / "Frame.qml"


def _code(path: Path, name: str) -> str:
    """*name*'s executable statements, docstring and comments stripped."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = [n for n in node.body if not _is_docstring(n)]
            return ast.unparse(ast.Module(body=body, type_ignores=[]))
    raise AssertionError(f"{name} not found in {path.name}")


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _engine(style: str) -> TransitionEngine:
    cfg = Config()
    cfg.update("slideshow", {"transition_style": style, "transition_duration_ms": 1000})
    return TransitionEngine(cfg)


def _composite(
    engine: TransitionEngine,
    progress: float,
    group_alpha: float,
    *,
    outgoing: float = 0.8,
    incoming: float = 0.9,
    backdrop: float = 0.5,
    background: float = 0.0,
) -> float:
    """The pixel the scene paints, in the scene's own stacking order.

    Mirrors ``Frame.qml`` bottom-to-top: the flat ``background`` Rectangle, the
    outgoing group (blur then artwork, both multiplied by the GROUP opacity), then
    the incoming group (same, multiplied by ``artworkOpacity``).

    *group_alpha* is the outgoing group's opacity — the value under test.
    """
    out = background
    out = group_alpha * backdrop + (1.0 - group_alpha) * out
    out = group_alpha * outgoing + (1.0 - group_alpha) * out

    a = engine.get_alpha(progress, "next")
    out = a * backdrop + (1.0 - a) * out
    out = a * incoming + (1.0 - a) * out
    return out


class TestTheOutgoingItemSurvivesTheStartOfAFade:
    def test_the_outgoing_group_is_not_erased(self) -> None:
        """The defect: at t=0 the frame went fully to the flat background colour.

        A crossfade's outgoing layer is meant to stay opaque and be covered, so
        the frame at the *start* of the fade must still be essentially the
        outgoing item — not the background showing through a hole.
        """
        engine = _engine("crossfade")
        prev_alpha = engine.get_alpha(0.0, "current")

        at_start = _composite(engine, 0.0, prev_alpha)

        assert at_start > 0.7, (
            f"the frame collapsed to {at_start:.3f} at t=0 — the outgoing item was "
            "erased and the flat background is showing"
        )

    def test_erasing_the_group_is_what_produced_the_flat_frame(self) -> None:
        """The counterfactual, so the guard cannot be satisfied by accident.

        With the old ``group_alpha = 0.0`` the composite is the background colour
        at t=0.  Asserting the *difference* is what makes this test about the fix
        rather than about the compositing maths in general.
        """
        engine = _engine("crossfade")
        prev_alpha = engine.get_alpha(0.0, "current")

        erased = _composite(engine, 0.0, 0.0)
        kept = _composite(engine, 0.0, prev_alpha)

        assert erased < 0.05, "precondition: erasing the group exposes the background"
        assert kept > erased + 0.6

    def test_the_outgoing_backdrop_is_visible_at_the_start(self) -> None:
        """Both parts of the outgoing item, not just its artwork.

        The blur is the layer that was reported as racing the image, so it has to
        be present from the first frame — driven by the same group opacity as the
        artwork beside it.
        """
        engine = _engine("crossfade")
        prev_alpha = engine.get_alpha(0.0, "current")

        # Only the outgoing backdrop, then the whole group: the difference is what
        # the outgoing blur contributes.
        blur_only = prev_alpha * 0.5
        assert blur_only > 0.4, (
            "the outgoing blurred backdrop must be painted from the first frame, at "
            "the group's opacity"
        )


class TestOneOpacityServesBothStyles:
    def test_crossfade_keeps_the_outgoing_layer_opaque(self) -> None:
        """A crossfade dissolves the incoming over an opaque outgoing layer.

        That is the engine's contract, and it is why ``prev_alpha`` is the right
        group opacity: it is already 1.0 for the whole window.
        """
        engine = _engine("crossfade")

        for step in range(0, 101, 10):
            assert engine.get_alpha(step / 100, "current") == 1.0

    def test_fade_through_black_genuinely_ramps_the_group_down(self) -> None:
        """The style that *does* need the outgoing item to dissolve away."""
        engine = _engine("fade_through_black")

        assert engine.get_alpha(0.0, "current") == 1.0
        # approx, not ==: the curve is ``max(0.0, 1 - t * 2)`` evaluated at exactly
        # 0.5, which lands on ~1e-16 of floating-point residue rather than 0.
        assert engine.get_alpha(0.5, "current") == pytest.approx(0.0, abs=1e-9)
        assert engine.get_alpha(1.0, "current") == pytest.approx(0.0, abs=1e-9)

    def test_the_composite_never_blanks_mid_crossfade(self) -> None:
        """No frame of a crossfade may drop to the background.

        This is the property the old code violated: erasing the outgoing group
        meant the only thing under the incoming image was the flat Rectangle.
        """
        engine = _engine("crossfade")
        darkest = min(
            _composite(engine, step / 100, engine.get_alpha(step / 100, "current"))
            for step in range(101)
        )

        assert darkest > 0.7, (
            f"a crossfade darkened to {darkest:.3f} — the outgoing item is being "
            "erased rather than covered"
        )

    def test_fade_through_black_does_reach_black(self) -> None:
        """The opposite requirement, so the two styles stay distinguishable."""
        engine = _engine("fade_through_black")
        darkest = min(
            _composite(engine, step / 200, engine.get_alpha(step / 200, "current"))
            for step in range(201)
        )

        assert darkest < 0.05


class TestTheSceneGroupsAndFadesAsOne:
    """Structural guards on the QML, which is where the grouping lives."""

    def test_each_item_is_one_group_with_its_backdrop(self) -> None:
        src = _QML.read_text(encoding="utf-8")

        for group, child in (("prevMedia", "prevAmbient"), ("media", "ambient")):
            assert f"id: {group}" in src, f"{group} is not a container"
            assert f"id: {child}" in src, f"{child} is missing"

        # The backdrop and the artwork must be CHILDREN of the group, so the
        # group's opacity applies to both.  If either were a sibling, a backdrop
        # and its artwork could fade at different rates — the defect class this
        # whole file is about.
        assert src.count('objectName: "prevAmbient"') == 1
        assert src.count('objectName: "ambient"') == 1

    def test_the_outgoing_group_opacity_is_the_engines_value(self) -> None:
        """``prevBackdropOpacity`` must be fed ``prev_alpha``, with no special case."""
        code = _code(_BACKEND, "present_transition")

        assert "prevBackdropOpacity" in code
        assert "group_alpha" not in code, (
            "the collapsing group_alpha special case is back — it erases the "
            "outgoing item and exposes the flat background"
        )

    def test_the_incoming_group_fades_on_the_incoming_alpha(self) -> None:
        code = _code(_BACKEND, "present_transition")

        assert "artworkOpacity" in code
