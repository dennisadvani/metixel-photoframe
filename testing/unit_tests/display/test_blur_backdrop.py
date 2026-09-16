# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for the ``blur`` ambient fill mode (the TV-letterbox backdrop).

The effect: the artwork is stretched to fill the whole screen with the aspect
ratio IGNORED, blurred, and painted as the base layer, so a contained photo sits
on top of a soft, screen-filling copy of itself.  The user also controls how
heavy the blur is and how far the backdrop is dimmed.

Two properties carry the cost, and both are easy to lose:

1. **Once per slide, not once per frame.**  A crossfade repaints ~75 times and
   calls the backdrop builder on every one of those frames.  Rebuilding the blur
   each time would be far more expensive than the resample the pre-scaled
   artwork already exists to avoid — it is a full-screen scale, twice, plus a
   downscale.  The cache key is what makes it once-per-slide.
2. **Brightness must not invalidate the blur.**  Dimming is a translucent black
   rect at paint time, so the brightness slider is free to drag.  Baking the
   darkening into the pixmap would make every pixel of the slider rebuild a
   full-screen blur.

As with the other display guards, these are structural assertions over the
source because CI has no Qt.  Qt-specific behaviour uses ``pytest.importorskip``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_CANVAS = _ROOT / "src" / "metixel" / "display" / "qt_canvas.py"
_PRESENTER = _ROOT / "src" / "metixel" / "frontend" / "presentation" / "presenter.py"
_TEMPLATES = _ROOT / "src" / "metixel" / "framing" / "framing_templates.py"
_LAYOUT = _ROOT / "src" / "metixel" / "framing" / "layout.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_source(path))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name}")


def _body(path: Path, name: str) -> str:
    return ast.get_source_segment(_source(path), _function(path, name)) or ""


def _code(path: Path, name: str) -> str:
    """The function's executable statements, docstring and comments stripped.

    Needed for "must not contain" checks: these docstrings quote the expressions
    they are warning about, so a naive substring search matches the explanation.
    """
    node = _function(path, name)
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def _blur_guards(path: Path, name: str) -> dict[str, list[str]]:
    """Which statements in *name* are gated on the blur strategy.

    Needed because several distinct guards share the same textual condition, so a
    substring check on the whole function passes when any ONE is present —
    removing another goes unnoticed.  The mutation harness caught exactly that:
    deleting the ambient-fill exclusion left the test green because the curtain
    exclusion contains the same text.

    Keys:
      ``backdrop_calls``   every ``_draw_backdrop_layer(...)`` call's arguments
      ``ambient_fill``     the test of the ``if`` wrapping the ambient band fill
      ``curtain``          the test of the ``if`` wrapping the curtain call
    """
    node = _function(path, name)
    found: dict[str, list[str]] = {}

    # Walk the statement tree, tracking whether an ENCLOSING if already tests for
    # blur.  Only the guard that actually decides the call is recorded, so the
    # enclosing ``if plan is not None:`` does not count as a blur gate.
    def visit(body: list[ast.stmt], blur_guarded: bool) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                func = ast.unparse(stmt.value.func)
                if func.endswith("_draw_backdrop_layer"):
                    if blur_guarded:
                        found.setdefault("backdrop_gated_calls", []).append(
                            ", ".join(ast.unparse(a) for a in stmt.value.args)
                        )
                    else:
                        found.setdefault("ungated_backdrop_calls", []).append(
                            ", ".join(ast.unparse(a) for a in stmt.value.args)
                        )
            if isinstance(stmt, ast.If):
                test = ast.unparse(stmt.test)
                body_src = "\n".join(ast.unparse(s) for s in stmt.body)
                if "_fill(painter, plan.ambient" in body_src and _excludes_blur(test):
                    found.setdefault("ambient_fill_excludes_blur", []).append(test)
                if "_paint_transition_curtain" in body_src and _excludes_blur(test):
                    found.setdefault("curtain_excludes_blur", []).append(test)
                nested = blur_guarded or _requires_blur(test)
                visit(stmt.body, nested)
                visit(stmt.orelse, blur_guarded)
            else:
                for child in ast.iter_child_nodes(stmt):
                    if isinstance(child, ast.If):
                        visit([child], blur_guarded)

    visit(node.body, False)
    return found


def _excludes_blur(test: str) -> bool:
    """True when *test* is a blur-strategy exclusion (``... != "blur"``)."""
    normalised = test.replace('"', "'")
    return "'blur'" in normalised and "!=" in normalised


def _requires_blur(test: str) -> bool:
    """True when *test* is a blur-strategy inclusion (``... == "blur"``)."""
    normalised = test.replace('"', "'")
    return "'blur'" in normalised and "==" in normalised and "!=" not in normalised


def _cache_key_elements(path: Path, name: str) -> list[str]:
    """Source fragments of each element of the cache-key tuple in *name*.

    Handles both shapes the key has taken: an assignment (``key = (...)``) and a
    direct ``return (...)``.  Parsed with AST rather than string-split, because
    the tuple contains a call (``round(radius, 2)``) and splitting on the first
    ``)`` truncates inside the nested brackets.
    """
    node = _function(path, name)
    for inner in ast.walk(node):
        tuple_node: ast.Tuple | None = None
        is_key_assign = (
            isinstance(inner, ast.Assign)
            and isinstance(inner.value, ast.Tuple)
            and any(isinstance(t, ast.Name) and t.id == "key" for t in inner.targets)
        )
        is_return = isinstance(inner, ast.Return) and isinstance(inner.value, ast.Tuple)
        if is_key_assign or is_return:
            tuple_node = inner.value  # type: ignore[assignment]
        if tuple_node is not None:
            return [ast.unparse(element) for element in tuple_node.elts]
    raise AssertionError(f"no cache-key tuple found in {name}")


def _dataclass_field_defaults(path: Path, class_name: str) -> dict[str, object]:
    """Field name -> literal default for an annotated dataclass attribute.

    Scoped to one class: ``__post_init__`` and similar names exist on several
    dataclasses in the framing engine, so a bare name lookup finds the wrong one.
    """
    for node in ast.walk(ast.parse(_source(path))):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        defaults: dict[str, object] = {}
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or stmt.value is None:
                continue
            if isinstance(stmt.target, ast.Name) and isinstance(stmt.value, ast.Constant):
                defaults[stmt.target.id] = stmt.value.value
        return defaults
    raise AssertionError(f"class {class_name} not found in {path.name}")


class TestTheBlurIsBuiltOncePerSlide:
    """The explicit efficiency requirement."""

    def test_the_builder_consults_its_cache_before_doing_work(self) -> None:
        """The cache hit must be a GUARDED return, not dead code.

        Checking only that a ``return self._blur_pixmap`` line exists somewhere
        before the first ``scaled()`` is not enough: deleting the ``if`` leaves
        the return in place as unreachable code and that check still passes, so
        the blur would be rebuilt on every frame of every crossfade with the
        suite green.  (That mutation was found by the mutation harness — it
        survived the first version of this test.)
        """
        node = _function(_CANVAS, "_blurred_backdrop")
        guarded = False
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            test_src = ast.unparse(inner.test)
            body_src = "\n".join(ast.unparse(s) for s in inner.body)
            if "_blur_pixmap" in test_src and "return self._blur_pixmap" in body_src:
                guarded = True
                break
        assert guarded, (
            "the cached pixmap must be returned from inside an `if` that tests "
            "the cache — an unguarded return is dead code and the blur rebuilds "
            "every frame"
        )

    def test_the_cache_test_checks_both_the_pixmap_and_the_key(self) -> None:
        """A stale pixmap must not be served for a different image or radius."""
        node = _function(_CANVAS, "_blurred_backdrop")
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            test_src = ast.unparse(inner.test)
            if "_blur_pixmap" in test_src:
                assert "_blur_key" in test_src, (
                    "the cache hit must compare the key, or a changed image or "
                    "blur radius would reuse the previous backdrop"
                )
                return
        raise AssertionError("no guarded cache-hit return found")

    def test_a_repeat_call_does_no_scaling_at_all(self) -> None:
        """The efficiency requirement, proved by counting real scale calls.

        Runs only where Qt exists (the Pi), because CI has no PySide6.  The
        structural guards above are what CI enforces.
        """
        pytest.importorskip("PySide6", reason="PySide6 not installed (CI/desktop dev)")
        from PySide6.QtGui import QColor, QImage

        from metixel.display.qt_canvas import FrameCanvas

        calls = {"n": 0}
        original_scaled = QImage.scaled

        def counting_scaled(self, *args, **kwargs):
            calls["n"] += 1
            return original_scaled(self, *args, **kwargs)

        QImage.scaled = counting_scaled  # type: ignore[method-assign]
        try:
            canvas = FrameCanvas()
            image = QImage(1600, 1200, QImage.Format.Format_RGB888)
            image.fill(QColor(200, 40, 40))
            canvas._image = image
            plan = self._plan()

            canvas._blurred_backdrop(plan)  # cold: three scales
            first_cost = calls["n"]
            assert first_cost >= 3, "the first build must stretch, downscale, upscale"

            for _ in range(10):
                canvas._blurred_backdrop(plan)  # warm: must be free
            assert calls["n"] == first_cost, (
                f"10 cached calls did {calls['n'] - first_cost} extra scales; "
                "the blur is being rebuilt per frame"
            )
        finally:
            QImage.scaled = original_scaled  # type: ignore[method-assign]

    def test_the_key_includes_the_image(self) -> None:
        """A new slide must not reuse the previous slide's backdrop."""
        code = _code(_CANVAS, "_backdrop_key")
        assert "id(image)" in code

    def test_the_key_includes_the_screen_size(self) -> None:
        """A resize or rotation must rebuild, since the backdrop is screen-sized."""
        code = _code(_CANVAS, "_backdrop_key")
        assert "rect.width()" in code and "rect.height()" in code

    def test_the_key_includes_the_blur_radius(self) -> None:
        """Changing the blur must rebuild; it is baked into the pixels.

        Parsed with AST: the tuple contains a call (``round(radius, 2)``), so
        splitting the source on the first ``)`` truncates at the wrong bracket
        and reports a false failure.
        """
        rendered = " | ".join(_cache_key_elements(_CANVAS, "_backdrop_key"))
        assert "radius" in rendered, f"the blur radius must be in the cache key: {rendered}"

    def test_the_key_excludes_the_darken_amount(self) -> None:
        """Brightness is applied at paint time, so it must NOT invalidate the blur.

        Including it would make dragging the brightness slider rebuild a
        full-screen blur per step — the expensive half of the effect — for a
        change that is a translucent rect.
        """
        rendered = " | ".join(_cache_key_elements(_CANVAS, "_backdrop_key"))
        assert "darken" not in rendered, f"darken must not be in the blur key: {rendered}"

    def test_darkening_is_a_fill_rect_at_paint_time(self) -> None:
        code = _code(_CANVAS, "_draw_backdrop_layer")
        assert "fillRect" in code, "dimming must be a paint-time overlay"
        assert "darken" in code


class TestTheBackdropGeometry:
    """Stretch-ignore-aspect, full screen — the user's explicit instruction."""

    def test_the_stretch_ignores_the_aspect_ratio(self) -> None:
        """``KeepAspectRatio`` would reintroduce the very letterbox gaps this fixes."""
        code = _code(_CANVAS, "_blur_payload")
        assert "IgnoreAspectRatio" in code
        assert "KeepAspectRatio" not in code, "keeping the aspect ratio leaves gaps at the edges"

    def test_the_blur_target_is_the_whole_screen(self) -> None:
        code = _code(_CANVAS, "_backdrop_key") + _code(_CANVAS, "_blur_payload")
        assert "plan.screen" in code, "the backdrop is screen-sized, not band-sized"

    def test_the_blur_is_a_real_filter_not_a_downscale(self) -> None:
        """A downscale round trip produces rectangular blocking.

        Regression: the backdrop was built by shrinking to ``1/radius`` and
        scaling back up.  That is a bilinear round trip through an ~80x50 image
        at display size, and a 4x-magnified side-by-side showed unmistakable
        blocky patches — the "JPEG-like artefacts" this replaced.
        """
        payload = _code(_CANVAS, "_blur_payload")
        assert "_blur_qimage(stretched, radius)" in payload, (
            "the blur must be a real filter, not a downscale/upscale pair"
        )
        assert "int(target_w / radius)" not in payload, (
            "the downscale divisor is what produced the blocking"
        )

    def test_the_filter_is_box_blur(self) -> None:
        """Measured: GaussianBlur is 2.4x the cost for no visible gain.

        Both are smooth; this runs on the render thread once per slide, so the
        cheaper of two good options wins.  Latency-bounded by that measurement —
        if the blur ever moves off the render thread, re-measure before changing.
        """
        code = _code(_CANVAS, "_blur_qimage")
        assert "BoxBlur" in code
        assert "GaussianBlur" not in code, "2.4x the cost for no visible gain"

    def test_the_radius_is_a_pixel_radius(self) -> None:
        """Larger must mean blurrier — the intuitive direction.

        The old code used the radius as a *divisor*, so larger meant LESS blur.
        That inverted control is part of why the quality problem was hard to
        reason about.
        """
        code = _code(_CANVAS, "_blur_qimage")
        assert "BoxBlur(radius)" in code, "the radius is passed straight to the filter"

    def test_the_radius_is_clamped(self) -> None:
        """A hand-edited config must not reach the filter unclamped."""
        code = _code(_CANVAS, "_backdrop_key")
        assert "min(100.0" in code or "min(100," in code
        assert "max(1.0" in code or "max(1," in code

    def test_a_filter_failure_reports_none(self) -> None:
        """Degrade to the flat fill rather than blanking the frame."""
        code = _code(_CANVAS, "_blur_qimage")
        assert "return None" in code
        assert "except Exception" in code, "a Pillow failure must not escape"

    def test_the_qimage_round_trip_is_encoded(self) -> None:
        """There is no direct QImage/PIL bridge; the buffer must be real bytes."""
        code = _code(_CANVAS, "_blur_qimage")
        assert "QBuffer" in code
        assert "Image.open" in code
        assert "QImage.fromData" in code


class TestTheBackdropZOrder:
    """Each backdrop sits immediately BEHIND its own artwork.

    The specified stacking, top to bottom:

        5. incoming artwork
        4. incoming backdrop
        3. outgoing artwork
        2. outgoing backdrop
        1. background

    i.e. the two items are stacked as ``(backdrop, artwork)`` PAIRS, outgoing
    first.  Getting this wrong is visible: a backdrop painted above the *other*
    item's artwork occludes it, and the outgoing photo then appears to vanish
    abruptly when ``_advance()`` drops the layer at the end of the transition.
    """

    def test_the_five_layers_are_in_the_specified_order(self) -> None:
        paint = _code(_CANVAS, "paintEvent")

        background = paint.index("fillRect(self.rect(), self._background)")
        outgoing_backdrop = paint.index("self._draw_backdrop_layer")
        outgoing_art = paint.index("self._draw_artwork(painter, self._prev_plan, self._prev_image)")
        incoming_backdrop = paint.index(
            "self._draw_backdrop_layer(painter, plan, self._image, self._image_alpha)"
        )
        incoming_art = paint.index("self._draw_artwork(painter, plan)")

        assert background < outgoing_backdrop, "the background is the base layer"
        assert outgoing_backdrop < outgoing_art, "the outgoing backdrop is behind ITS artwork"
        assert outgoing_art < incoming_backdrop, (
            "the incoming pair is stacked over the outgoing one"
        )
        assert incoming_backdrop < incoming_art, "the incoming backdrop is behind ITS artwork"

    def test_each_backdrop_uses_its_own_item_s_image(self) -> None:
        """A letterboxed photo must be surrounded by ITS OWN blur, not the next one's."""
        paint = _code(_CANVAS, "paintEvent")
        assert "self._draw_backdrop_layer(painter, self._prev_plan, self._prev_image" in paint
        assert "self._draw_backdrop_layer(painter, plan, self._image" in paint

    def test_backdrops_fade_with_their_own_artwork(self) -> None:
        """Pair and photo resolve together, so nothing pops."""
        paint = _code(_CANVAS, "paintEvent")
        assert "self._prev_alpha" in paint
        assert "self._image_alpha" in paint

    def test_the_backdrop_is_clipped_to_its_own_band(self) -> None:
        """Otherwise it would cover its own photo, which sits above it."""
        code = _code(_CANVAS, "_draw_backdrop_layer")
        assert "setClipRegion" in code
        assert "subtracted" in code, "the clip is the artwork's complement"
        assert "plan.artwork_dst" in code

    def test_the_darken_overlay_is_inside_the_opacity_scope(self) -> None:
        """Otherwise the dim sits at full strength over a half-faded backdrop."""
        code = _code(_CANVAS, "_draw_backdrop_layer")
        set_opacity = code.index("setOpacity")
        fill = code.index("fillRect")
        reset = code.rindex("setOpacity(1.0)")
        assert set_opacity < fill < reset, "the dim must be drawn at the layer's alpha"

    def test_each_backdrop_is_gated_on_the_blur_strategy(self) -> None:
        """Both backdrop calls must sit inside a ``== 'blur'`` check.

        Asserted per call site rather than by substring: several guards share the
        text ``ambient_strategy``, so a whole-function substring check passes when
        only one of them remains, and the mutation harness caught that.
        """
        guards = _blur_guards(_CANVAS, "paintEvent")
        assert not guards.get("ungated_backdrop_calls"), (
            f"every backdrop call must be gated on blur, ungated: "
            f"{guards.get('ungated_backdrop_calls')}"
        )
        calls = guards.get("backdrop_gated_calls", [])
        assert len(calls) == 2, f"expected one gated backdrop call per layer, found {calls}"
        assert any("self._prev_image" in call for call in calls), "the outgoing layer"
        assert any("self._image" in call for call in calls), "the incoming layer"

    def test_the_flat_ambient_band_is_skipped_in_blur_mode(self) -> None:
        """A flat fill over the backdrop would hide the effect entirely."""
        guards = _blur_guards(_CANVAS, "paintEvent")
        assert guards.get("ambient_fill_excludes_blur"), (
            "the ambient band fill must be excluded in blur mode"
        )

    def test_the_curtain_is_skipped_in_blur_mode(self) -> None:
        """In blur mode the backdrop owns the band, so a flat wipe would defeat it."""
        guards = _blur_guards(_CANVAS, "paintEvent")
        assert guards.get("curtain_excludes_blur"), "the curtain must be excluded in blur mode"

    def test_the_curtain_still_runs_in_solid_and_bars_modes(self) -> None:
        """Regression: deleting the call instead of gating it dropped the fix.

        The curtain wipes the outgoing item's letterbox residue so it fades
        rather than snapping.  In blur mode the incoming backdrop replaces that
        residue, so the curtain is skipped there — but it must still run for
        ``solid``/``bars``, where there is no backdrop to do the job.
        """
        paint = _code(_CANVAS, "paintEvent")
        assert "_paint_transition_curtain" in paint, (
            "the curtain call must exist — it was removed for all modes at one point"
        )
        index = paint.index("_paint_transition_curtain")
        preceding = paint[:index]
        assert "ambient_strategy != 'blur'" in preceding, (
            "the curtain must be gated to non-blur modes, not deleted"
        )

    def test_the_curtain_is_not_painted_under_a_backdrop(self) -> None:
        """In blur mode the backdrop owns the band; a flat wipe would defeat it."""
        paint = _code(_CANVAS, "paintEvent")
        curtain = paint.index("_paint_transition_curtain")
        incoming_backdrop = paint.index(
            "self._draw_backdrop_layer(painter, plan, self._image, self._image_alpha)"
        )
        assert curtain < incoming_backdrop


class TestFailureDegradesRatherThanBlanks:
    def test_a_failed_build_reports_none(self) -> None:
        code = _code(_CANVAS, "_blurred_backdrop")
        assert "return None" in code, "a failed blur must be reported, not raised"

    def test_the_painter_skips_a_missing_backdrop(self) -> None:
        code = _code(_CANVAS, "_draw_backdrop_layer")
        assert "if pixmap is None" in code, (
            "a missing backdrop must fall through to the flat ambient look"
        )


class TestTheBlurCacheCannotOutliveItsImage:
    """A full-screen pixmap is ~9 MB; pinning it would be a slow leak."""

    def test_the_backdrop_is_released_when_the_image_changes(self) -> None:
        code = _code(_CANVAS, "_store_layers")
        assert "self._blur_pixmap = None" in code
        assert "self._blur_key = None" in code

    def test_the_release_test_uses_identity(self) -> None:
        code = _code(_CANVAS, "_store_layers")
        assert "is not self._blur_image" in code

    def test_exactly_two_backdrops_are_held(self) -> None:
        """One per crossfade layer — no more.

        The fix for the z-order bug needs the outgoing item's backdrop as well as
        the incoming one, so two slots are correct.  A *collection* would not be:
        it could grow, and a full-screen pixmap is ~9 MB.
        """
        source = _source(_CANVAS)
        assert "self._prev_blur_pixmap" in source, "the outgoing layer needs its own"
        assert "self._blur_pixmaps" not in source, (
            "a collection here could grow unboundedly; two named slots cannot"
        )
        assert "self._blur_cache" not in source

    def test_both_slots_are_released(self) -> None:
        code = _code(_CANVAS, "_store_layers")
        assert "self._blur_pixmap = None" in code
        assert "self._prev_blur_pixmap = None" in code, (
            "the outgoing backdrop must be released when its layer ends"
        )


class TestTheParametersReachTheEngine:
    """A control that never reaches the geometry is a dead control."""

    def test_build_request_accepts_both(self) -> None:
        signature = _function(_TEMPLATES, "build_request").args
        names = {a.arg for a in signature.args}
        names |= {a.arg for a in signature.kwonlyargs}
        assert "ambient_blur_radius" in names
        assert "ambient_darken" in names

    def test_build_request_forwards_them_into_the_spec(self) -> None:
        """They were previously hardcoded, so a user value was silently dropped."""
        code = _code(_TEMPLATES, "build_request")
        assert "blur_radius=float(ambient_blur_radius)" in code
        assert "darken=float(ambient_darken)" in code

    def test_the_layout_engine_carries_them(self) -> None:
        code = _code(_LAYOUT, "compute")
        assert "ambient_blur_radius=self._ambient_blur_radius" in code
        assert "ambient_darken=self._ambient_darken" in code

    def test_the_plan_exposes_them_to_the_canvas(self) -> None:
        """The canvas reads them off the plan; without that the mode does nothing."""
        code = _code(_LAYOUT, "compute")
        assert "ambient_strategy=result.ambient_fill.strategy" in code
        assert "ambient_blur_radius=result.ambient_fill.blur_radius" in code
        assert "ambient_darken=result.ambient_fill.darken" in code

    def test_the_presenter_forwards_the_config_values(self) -> None:
        code = _code(_PRESENTER, "reload_config")
        assert "_ambient_blur_radius(config)" in code
        assert "_ambient_darken(config)" in code

    def test_a_blur_change_is_detected_by_the_reload(self) -> None:
        """The exact bug that made the ambient colour look un-configurable.

        The parameters are engine constructor arguments, so a reload that does
        not compare them leaves the engine on its startup values and the slider
        appears dead.
        """
        code = _code(_PRESENTER, "reload_config")
        assert "self._layout.ambient_blur_radius" in code
        assert "self._layout.ambient_darken" in code


class TestConfigValuesAreSanitised:
    """Hand-edited config must not break the render loop."""

    def test_blur_radius_is_clamped(self) -> None:
        code = _code(_PRESENTER, "_ambient_blur_radius")
        assert "MIN_AMBIENT_BLUR_RADIUS" in code
        assert "MAX_AMBIENT_BLUR_RADIUS" in code

    def test_blur_radius_survives_a_non_number(self) -> None:
        code = _code(_PRESENTER, "_ambient_blur_radius")
        assert "except (TypeError, ValueError)" in code

    def test_darken_is_clamped_to_zero_one(self) -> None:
        code = _code(_PRESENTER, "_ambient_darken")
        assert "0.0 <= darken <= 1.0" in code

    def test_the_defaults_match_the_engine(self) -> None:
        """A drift here would show as a different look before and after a reload."""
        presenter_src = _source(_PRESENTER)
        assert "_DEFAULT_AMBIENT_BLUR_RADIUS = 24.0" in presenter_src
        assert "_DEFAULT_AMBIENT_DARKEN = 0.35" in presenter_src

        # The engine's own AmbientFillSpec defaults must agree.  Read them off
        # the specific class — ``__post_init__`` exists on several dataclasses in
        # that module, so name lookup alone finds the wrong one.
        defaults = _dataclass_field_defaults(
            _ROOT / "src" / "metixel" / "framing" / "framing_engine.py",
            "AmbientFillSpec",
        )
        assert defaults.get("blur_radius") == 24.0
        assert defaults.get("darken") == 0.35


class TestQtBehaviourWhenAvailable:
    """The real thing, where PySide6 exists (the Pi, not CI)."""

    def _plan(self, **overrides):
        from metixel.framing.layout import RenderPlan

        base = {
            "screen": (0.0, 0.0, 1920.0, 1200.0),
            "ambient": (0.0, 0.0, 1920.0, 1200.0),
            "artwork_dst": (169.0, 7.0, 1581.0, 1185.0),
            "artwork_src": (0.0, 0.0, 1600.0, 1200.0),
            "whitespace": (),
            "matte": (),
            "moulding": (),
            "matte_colour": "#ffffff",
            "whitespace_colour": "#ffffff",
            "ambient_colour": "#101014",
            "style": "borderless",
            "branch": "virtual",
            "overflow": "fill",
            "ambient_strategy": "blur",
            "ambient_blur_radius": 24.0,
            "ambient_darken": 0.35,
        }
        base.update(overrides)
        return RenderPlan(**base)

    def test_the_backdrop_is_screen_sized(self) -> None:
        pytest.importorskip("PySide6", reason="PySide6 not installed (CI/desktop dev)")
        from PySide6.QtGui import QColor, QImage

        from metixel.display.qt_canvas import FrameCanvas

        canvas = FrameCanvas()
        image = QImage(1600, 1200, QImage.Format.Format_RGB888)
        image.fill(QColor(200, 40, 40))
        canvas._image = image

        pixmap = canvas._blurred_backdrop(self._plan())
        assert pixmap is not None
        assert (pixmap.width(), pixmap.height()) == (1920, 1200), (
            "the backdrop must fill the screen, not the artwork rect"
        )

    def test_a_repeat_call_reuses_the_pixmap(self) -> None:
        pytest.importorskip("PySide6", reason="PySide6 not installed (CI/desktop dev)")
        from PySide6.QtGui import QColor, QImage

        from metixel.display.qt_canvas import FrameCanvas

        canvas = FrameCanvas()
        image = QImage(1600, 1200, QImage.Format.Format_RGB888)
        image.fill(QColor(200, 40, 40))
        canvas._image = image
        plan = self._plan()

        first = canvas._blurred_backdrop(plan)
        second = canvas._blurred_backdrop(plan)
        assert first is second, "the per-frame call must reuse the cached blur"

    def test_darken_does_not_invalidate_the_blur(self) -> None:
        """Twiddling brightness must not rebuild the pixmap."""
        pytest.importorskip("PySide6", reason="PySide6 not installed (CI/desktop dev)")
        from PySide6.QtGui import QColor, QImage

        from metixel.display.qt_canvas import FrameCanvas

        canvas = FrameCanvas()
        image = QImage(1600, 1200, QImage.Format.Format_RGB888)
        image.fill(QColor(200, 40, 40))
        canvas._image = image

        first = canvas._blurred_backdrop(self._plan(ambient_darken=0.0))
        second = canvas._blurred_backdrop(self._plan(ambient_darken=0.9))
        assert first is second

    def test_a_new_radius_rebuilds(self) -> None:
        pytest.importorskip("PySide6", reason="PySide6 not installed (CI/desktop dev)")
        from PySide6.QtGui import QColor, QImage

        from metixel.display.qt_canvas import FrameCanvas

        canvas = FrameCanvas()
        image = QImage(1600, 1200, QImage.Format.Format_RGB888)
        image.fill(QColor(200, 40, 40))
        canvas._image = image

        first = canvas._blurred_backdrop(self._plan(ambient_blur_radius=8.0))
        second = canvas._blurred_backdrop(self._plan(ambient_blur_radius=60.0))
        assert first is not second, "the blur is baked in, so it must rebuild"
