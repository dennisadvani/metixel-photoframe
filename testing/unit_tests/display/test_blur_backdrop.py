# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for the ``blur`` ambient fill mode (the TV-letterbox backdrop).

The effect: the artwork is stretched to fill the whole screen with the aspect
ratio IGNORED, blurred, and painted as the base layer, so a contained photo sits
on top of a soft, screen-filling copy of itself.  The user also controls how
heavy the blur is and how far the backdrop is dimmed.

Three properties carry the cost, and all three are easy to lose:

1. **The blur runs in a throttled subprocess.**  It is ``nice``'d and, where
   ``cpulimit`` exists, hard-capped — see
   :mod:`metixel.display.ambient_blur`.  Building it in the render loop is what
   made crossfades judder.
2. **It is built once per showing, not once per frame.**  The result is held in
   one of two slots in the canvas for the whole time the slide is on screen.
3. **Brightness must not invalidate the blur.**  Dimming is a translucent black
   rect at paint time, so the brightness slider is free to drag.  Baking the
   darkening into the image would make every step of the slider rebuild a
   full-screen blur.

The lifecycle guards (identity, the subprocess wrapper, the presenter's
sequencing) live in ``test_backdrop_warm.py``.

As with the other display guards, these are structural assertions over the
source because CI has no Qt.  Qt-specific behaviour uses ``pytest.importorskip``.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_CANVAS = _ROOT / "src" / "metixel" / "display" / "qt_canvas.py"
_AMBIENT = _ROOT / "src" / "metixel" / "display" / "ambient_blur.py"
_PRESENTER = _ROOT / "src" / "metixel" / "frontend" / "presentation" / "presenter.py"
_TEMPLATES = _ROOT / "src" / "metixel" / "framing" / "framing_templates.py"
_LAYOUT = _ROOT / "src" / "metixel" / "framing" / "layout.py"

#: Stands in for an adopted pixmap in the slot tests.  A real ``QPixmap`` would
#: prove nothing extra about the slot logic and needs a GUI thread.
_SENTINEL = object()


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


class TestTheBlurRunsOutsideTheRenderLoop:
    """The cost fix: no blur, and no Pillow, anywhere on the paint path."""

    def test_the_canvas_has_no_blur_helper_left(self) -> None:
        """The blur moved to :mod:`metixel.display.ambient_blur`.

        A leftover copy is not harmless.  Deleting the call but keeping the
        helper is exactly how this regresses: the next edit reaches for the
        function that is still sitting there, and the blur is back in the paint.
        """
        source = _source(_CANVAS)
        for gone in ("_blur_payload", "_blur_qimage", "_blurred_backdrop"):
            assert gone not in source, f"{gone} must be gone from the canvas"

    def test_the_canvas_does_not_import_pillow(self) -> None:
        """Pillow belongs to the blur subprocess, not to the renderer."""
        assert "from PIL" not in _source(_CANVAS)

    def test_the_paint_path_never_blurs(self) -> None:
        code = _code(_CANVAS, "_draw_backdrop_layer")
        assert "BoxBlur" not in code
        assert "filter(" not in code

    def test_warming_uses_a_subprocess_runner_not_a_thread(self) -> None:
        """``nice``/``cpulimit`` are process-level.

        A thread cannot be throttled by either, so warming on a thread would be
        the original stall with nothing capping it.
        """
        code = _code(_CANVAS, "warm_backdrop")
        assert "BackdropRunner" in code, "the blur must run in the child process"
        assert "threading.Thread" not in code

    def test_darkening_is_a_fill_rect_at_paint_time(self) -> None:
        code = _code(_CANVAS, "_draw_backdrop_layer")
        assert "fillRect" in code, "dimming must be a paint-time overlay"
        assert "darken" in code


class TestTheBlurGeometry:
    """Stretch-ignore-aspect, full screen — the user's explicit instruction.

    Behavioural rather than structural, and it can be: ``ambient_blur`` imports
    no Qt, which is a property of the design worth relying on.
    """

    @staticmethod
    def _edge_image(path: Path) -> Path:
        """A hard black/white vertical edge, to prove a filter really ran."""
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (200, 200), (0, 0, 0))
        ImageDraw.Draw(image).rectangle([100, 0, 199, 199], fill=(255, 255, 255))
        image.save(path)
        return path

    @staticmethod
    def _solid_image(path: Path, size: tuple[int, int], colour: tuple[int, int, int]) -> Path:
        from PIL import Image

        Image.new("RGB", size, colour).save(path)
        return path

    def test_the_stretch_ignores_the_aspect_ratio(self, tmp_path: Path) -> None:
        """``KeepAspectRatio`` would reintroduce the very letterbox gaps this fixes."""
        from PIL import Image

        from metixel.display.ambient_blur import blur_to_file

        source = self._solid_image(tmp_path / "wide.jpg", (800, 200), (200, 40, 40))
        dest = tmp_path / "out.jpg"
        assert blur_to_file(source, dest, 400, 400, 8.0) is True

        with Image.open(dest) as result:
            assert result.size == (400, 400), "the backdrop must fill the target exactly"

    def test_the_blur_is_a_real_filter_not_a_downscale(self, tmp_path: Path) -> None:
        """A downscale round trip produces rectangular blocking.

        Regression: the backdrop was built by shrinking to ``1/radius`` and
        scaling back up.  That is a bilinear round trip through an ~80x50 image
        at display size, and a 4x-magnified side-by-side showed unmistakable
        blocky patches — the "JPEG-like artefacts" this replaced.  A hard edge is
        the cheapest way to tell the two apart: a filter spreads it, a resample
        leaves it nearly binary.
        """
        from PIL import Image

        from metixel.display.ambient_blur import blur_to_file

        source = self._edge_image(tmp_path / "edge.jpg")
        dest = tmp_path / "out.jpg"
        assert blur_to_file(source, dest, 200, 200, 20.0) is True

        # Read the band straddling the edge as a histogram: it gives the spread
        # of tones directly, with no per-pixel typing to argue with.
        with Image.open(dest) as result:
            histogram = result.convert("L").crop((70, 0, 130, 200)).histogram()

        tones = [tone for tone, count in enumerate(histogram) if count]
        assert tones[0] < 90, "the dark side must survive"
        assert tones[-1] > 170, "the light side must survive"
        assert len(tones) > 5, (
            "a resample leaves an edge nearly binary; a real filter produces a gradient"
        )

    def test_both_kernels_are_offered_and_box_is_the_default(self) -> None:
        """Measured: GaussianBlur is ~2.4x the cost for no visible gain.

        Both are smooth, so the cheap one is the default — but which one is used
        is the user's choice, because a photo with soft gradients can make the box
        kernel's square shoulders visible.
        """
        from metixel.display.ambient_blur import DEFAULT_FILTER, FILTERS

        assert FILTERS == ("box", "gaussian")
        assert DEFAULT_FILTER == "box", "the measured-cheaper kernel is the default"
        code = _code(_AMBIENT, "blur_to_file")
        assert "ImageFilter.BoxBlur" in code
        assert "ImageFilter.GaussianBlur" in code

    def test_an_unknown_kernel_falls_back_instead_of_failing(self) -> None:
        """A hand-edited config — or one from a release before this key — has none."""
        from metixel.display.ambient_blur import resolve_filter

        assert resolve_filter(None) == "box"
        assert resolve_filter("") == "box"
        assert resolve_filter("swirl") == "box"
        assert resolve_filter(7) == "box"
        assert resolve_filter("Gaussian") == "gaussian", "case must not matter"

    def test_the_kernel_choice_reaches_the_filter(self, tmp_path: Path) -> None:
        """Every other parameter is identical, so identical output means the
        kernel was ignored — which is exactly what a broken setting looks like."""
        from metixel.display.ambient_blur import blur_to_file

        source = self._edge_image(tmp_path / "edge.jpg")
        box = tmp_path / "box.jpg"
        gaussian = tmp_path / "gaussian.jpg"

        assert blur_to_file(source, box, 200, 200, 20.0, "box") is True
        assert blur_to_file(source, gaussian, 200, 200, 20.0, "gaussian") is True

        assert box.read_bytes() != gaussian.read_bytes(), (
            "the selected kernel must actually build the backdrop"
        )

    def test_the_radius_is_a_pixel_radius(self) -> None:
        """Larger must mean blurrier — the intuitive direction.

        The old code used the radius as a *divisor*, so larger meant LESS blur.
        """
        code = _code(_AMBIENT, "blur_to_file")
        assert "pixels = clamp_radius(radius)" in code, (
            "the radius is clamped before it reaches either filter"
        )
        assert "BoxBlur(pixels)" in code

    def test_the_radius_is_clamped(self) -> None:
        """A hand-edited config must not reach the filter unclamped."""
        from metixel.display.ambient_blur import clamp_radius

        assert clamp_radius(0.0) == 1.0
        assert clamp_radius(-5.0) == 1.0
        assert clamp_radius(1e9) == 100.0
        assert clamp_radius(24.0) == 24.0

    def test_a_filter_failure_reports_false(self, tmp_path: Path) -> None:
        """Degrade to the flat fill rather than blanking the frame."""
        from metixel.display.ambient_blur import blur_to_file

        assert blur_to_file(tmp_path / "missing.jpg", tmp_path / "out.jpg", 10, 10, 4.0) is False

    def test_a_failure_never_raises(self, tmp_path: Path) -> None:
        """The caller is a subprocess boundary; an exception would be a crash."""
        code = _code(_AMBIENT, "blur_to_file")
        assert "return False" in code
        assert "except Exception" in code

    def test_the_stored_backdrop_is_a_jpeg(self, tmp_path: Path) -> None:
        """A lossless full-screen image per item would be megabytes of SD write."""
        from metixel.display.ambient_blur import blur_to_file

        source = self._solid_image(tmp_path / "in.jpg", (600, 400), (10, 120, 200))
        dest = tmp_path / "out.jpg"
        assert blur_to_file(source, dest, 640, 480, 6.0) is True

        assert dest.read_bytes()[:2] == b"\xff\xd8", "the backdrop is stored as JPEG"

    def test_the_cli_reports_the_result_through_the_exit_code(self, tmp_path: Path) -> None:
        """The exit code is the ONLY result channel — ``cpulimit`` owns stdout."""
        from metixel.display.ambient_blur import main

        source = self._solid_image(tmp_path / "in.jpg", (300, 300), (30, 30, 30))
        argv = [
            "--source",
            str(source),
            "--dest",
            str(tmp_path / "out.jpg"),
            "--width",
            "120",
            "--height",
            "120",
            "--radius",
            "5",
        ]
        assert main(argv) == 0

        bad = list(argv)
        bad[bad.index("--source") + 1] = str(tmp_path / "nope.jpg")
        assert main(bad) == 1

    def test_the_worker_never_prints(self) -> None:
        """``cpulimit`` writes its own lines to stdout.

        A worker that reported its result there had that result corrupted once
        already, so the blur worker prints nothing at all.
        """
        code = _code(_AMBIENT, "main")
        assert "print(" not in code


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
            "self._draw_backdrop_layer(painter, plan, "
            "self._backdrop_for(self._image), self._image_alpha)"
        )
        incoming_art = paint.index("self._draw_artwork(painter, plan)")

        assert background < outgoing_backdrop, "the background is the base layer"
        assert outgoing_backdrop < outgoing_art, "the outgoing backdrop is behind ITS artwork"
        assert outgoing_art < incoming_backdrop, (
            "the incoming pair is stacked over the outgoing one"
        )
        assert incoming_backdrop < incoming_art, "the incoming backdrop is behind ITS artwork"

    def test_each_backdrop_uses_its_own_item_s_artwork(self) -> None:
        """A letterboxed photo must be surrounded by ITS OWN blur, not the next one's.

        The lookup is by the layer's own handle, so the two backdrops can never
        be swapped for each other.
        """
        paint = _code(_CANVAS, "paintEvent")
        assert (
            "self._draw_backdrop_layer(painter, self._prev_plan, "
            "self._backdrop_for(self._prev_image), self._prev_alpha)" in paint
        )
        assert (
            "self._draw_backdrop_layer(painter, plan, "
            "self._backdrop_for(self._image), self._image_alpha)" in paint
        )

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
        incoming_backdrop = paint.index("self._draw_backdrop_layer(painter, plan,")
        assert curtain < incoming_backdrop


class TestFailureDegradesRatherThanBlanks:
    def test_a_missing_backdrop_falls_back_to_the_flat_fill(self) -> None:
        """``_draw_backdrop_layer`` must never blur, and never blank the frame."""
        code = _code(_CANVAS, "_draw_backdrop_layer")
        assert "if pixmap is None" in code
        assert "_paint_flat_backdrop" in code

    def test_the_fallback_is_black_not_the_ambient_colour(self) -> None:
        """Under a blur, the configured colour belongs to the OTHER looks.

        A band of ``ambient_color`` appearing for a frame or two is a flash of a
        look the user did not choose, which reads as a glitch.  Black is the only
        honest neutral stand-in.
        """
        code = _code(_CANVAS, "_paint_flat_backdrop")
        assert "QColor(0, 0, 0)" in code
        assert "ambient_colour" not in code, (
            "the blur fallback must not borrow the solid/bars colour"
        )

    def test_a_failed_backdrop_is_not_waited_on_forever(self) -> None:
        """A backdrop that cannot be built must not hold every slide that shows it."""
        code = _code(_CANVAS, "backdrop_ready")
        assert "_backdrop_failed" in code
        assert "return True" in code

    def test_a_failure_is_recorded_by_the_runner(self) -> None:
        code = _code(_CANVAS, "collect_warm_backdrop")
        assert "_backdrop_failed.add" in code


class TestOnlyTwoBackdropsAreHeld:
    """A full-screen pixmap is ~9 MB; the bound must be structural."""

    def test_the_canvas_keeps_exactly_two_slots(self) -> None:
        source = _source(_CANVAS)
        assert "self._backdrop: _BackdropSlot | None = None" in source
        assert "self._prev_backdrop: _BackdropSlot | None = None" in source

    def test_the_slots_are_not_a_growing_collection(self) -> None:
        """A cache here could grow without bound, one full-screen pixmap per key."""
        source = _source(_CANVAS)
        assert "self._backdrops:" not in source
        assert "self._backdrop_cache" not in source

    def test_storing_reuses_the_slot_that_is_not_on_screen(self) -> None:
        """That reuse is what makes the two slots sufficient.

        Choosing by liveness rather than by searching for a key is also what
        removes the release pass an earlier version needed.
        """
        code = _code(_CANVAS, "_store_backdrop")
        assert "_is_live" in code
        assert "self._prev_backdrop = slot" in code

    def test_liveness_is_identity_not_equality(self) -> None:
        """``QImage`` equality compares every pixel."""
        code = _code(_CANVAS, "_is_live")
        assert "is self._image" in code
        assert "is self._prev_image" in code

    def test_the_outgoing_backdrop_has_its_own_slot(self) -> None:
        source = _source(_CANVAS)
        assert "self._prev_backdrop" in source, "the outgoing layer needs its own"


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


@pytest.fixture
def canvas():
    """A real ``FrameCanvas`` on an offscreen platform, so no display is needed."""
    pytest.importorskip("PySide6", reason="PySide6 not installed (CI/desktop dev)")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from metixel.display.qt_canvas import FrameCanvas

    app = QApplication.instance() or QApplication([])
    widget = FrameCanvas()
    try:
        yield widget
    finally:
        widget.deleteLater()
        del app


class TestTheBackdropSlotsWhenQtIsAvailable:
    """The real slot container, where PySide6 exists (the Pi, not CI).

    ``QPixmap`` is deliberately avoided: a sentinel proves the slot logic just as
    well, and creating a GUI-thread resource here would only add a way for the
    test to fail for reasons that are not the behaviour under test.
    """

    @staticmethod
    def _touch(tmp_path: Path, name: str) -> Path:
        """A backdrop's identity includes the source's size and mtime."""
        path = tmp_path / name
        path.write_bytes(b"not a real image")
        return path

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

    def test_the_request_is_screen_sized(self, canvas, tmp_path: Path) -> None:
        source = self._touch(tmp_path, "a.jpg")
        request = canvas.backdrop_request(self._plan(), source)
        assert request is not None
        assert (request.width, request.height) == (1920, 1200), (
            "the backdrop fills the screen, not the artwork rect"
        )

    def test_a_stored_backdrop_is_found_by_its_own_artwork(self, canvas, tmp_path: Path) -> None:
        source = self._touch(tmp_path, "a.jpg")
        request = canvas.backdrop_request(self._plan(), source)
        assert request is not None
        handle = object()
        canvas._store_backdrop(request, handle, _SENTINEL)

        assert canvas._backdrop_for(handle) is _SENTINEL
        assert canvas._backdrop_for(object()) is None, "another layer must not borrow it"

    def test_readiness_follows_the_stored_request(self, canvas, tmp_path: Path) -> None:
        source = self._touch(tmp_path, "a.jpg")
        plan = self._plan()
        assert canvas.backdrop_ready(plan, source) is False

        request = canvas.backdrop_request(plan, source)
        assert request is not None
        canvas._store_backdrop(request, object(), _SENTINEL)
        assert canvas.backdrop_ready(plan, source) is True

    def test_a_different_radius_is_not_ready(self, canvas, tmp_path: Path) -> None:
        """The blur is baked into the pixels, so the radius is part of identity."""
        source = self._touch(tmp_path, "a.jpg")
        request = canvas.backdrop_request(self._plan(ambient_blur_radius=8.0), source)
        assert request is not None
        canvas._store_backdrop(request, object(), _SENTINEL)

        assert canvas.backdrop_ready(self._plan(ambient_blur_radius=60.0), source) is False
        assert canvas.backdrop_ready(self._plan(ambient_blur_radius=8.0), source) is True

    def test_a_new_screen_size_is_not_ready(self, canvas, tmp_path: Path) -> None:
        """A resize or rotation changes the target, so the old one is stale."""
        source = self._touch(tmp_path, "a.jpg")
        request = canvas.backdrop_request(self._plan(), source)
        assert request is not None
        canvas._store_backdrop(request, object(), _SENTINEL)

        smaller = self._plan(screen=(0.0, 0.0, 1280.0, 720.0))
        assert canvas.backdrop_ready(smaller, source) is False

    def test_a_failed_backdrop_is_treated_as_ready(self, canvas, tmp_path: Path) -> None:
        """Holding a slide for a backdrop that will never arrive is a stall."""
        source = self._touch(tmp_path, "a.jpg")
        plan = self._plan()
        request = canvas.backdrop_request(plan, source)
        assert request is not None

        canvas._backdrop_failed.add(request.job_id)
        assert canvas.backdrop_ready(plan, source) is True

    def test_a_third_backdrop_reuses_a_slot_rather_than_growing(
        self, canvas, tmp_path: Path
    ) -> None:
        """The bound is two full-screen pixmaps, by construction."""
        handles = [object() for _ in range(3)]
        for index, handle in enumerate(handles):
            path = self._touch(tmp_path, f"{index}.jpg")
            request = canvas.backdrop_request(self._plan(), path)
            assert request is not None
            canvas._store_backdrop(request, handle, _SENTINEL)

        held = [handle for handle in handles if canvas._backdrop_for(handle) is not None]
        assert len(held) <= 2, "at most two full-screen pixmaps may be held"
        assert canvas._backdrop_for(handles[-1]) is _SENTINEL, "the newest is kept"
