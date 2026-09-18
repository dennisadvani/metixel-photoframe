# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for the crossfade's pre-scaled artwork.

Regression these exist for: a crossfade re-ran a bilinear resample of BOTH
layers on EVERY frame, for the whole transition.  A crossfade changes exactly
one quantity per frame — the alpha — while ``artwork_src``, ``artwork_dst`` and
the source pixels are constant, so ~75 resamples per layer were pure waste.  It
was the transition's dominant CPU cost and the reason a crossfade burned far
more CPU than the retired pi3d path, where the GPU's texture sampler scaled for
free.

Qt's raster engine has no "sample a texture at an opacity": ``setOpacity`` scales
the *result* of a draw, so a scaled draw still pays for the scaling.  The fix is
to scale once into a destination-sized pixmap, then blit it 1:1.

Two things must stay true, and they pull in opposite directions:

1. **Scale once per layer.**  Rebuilding on every tick would restore the old cost
   while looking like a fix.
2. **Never outlive the layer.**  Keeping a stale pixmap would either paint the
   wrong geometry or pin ~7.5 MB per layer for the rest of the run — on a 512 MB
   device that is the slow-OOM failure mode ``ImageCache`` is capped to avoid.

These are structural assertions over the source, because CI runs with no Qt
installed.  Anything needing Qt uses ``pytest.importorskip``.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

_DISPLAY_DIR = Path(__file__).resolve().parents[3] / "src" / "metixel" / "display"
_CANVAS = _DISPLAY_DIR / "qt_canvas.py"
_GEOMETRY = _DISPLAY_DIR / "geometry.py"
_TK_BACKEND = _DISPLAY_DIR / "tk_backend.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _method_body(path: Path, name: str) -> str:
    source = _source(path)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} not found in {path.name}")


def _find_function(source: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _code_without_docstring(path: Path, name: str) -> str:
    """The method's EXECUTABLE code, as normalised source.

    Needed before any "must not contain X" assertion: the docstrings and comments
    in ``qt_canvas`` quote the old buggy expressions (``int(dw)``,
    ``drawImage(target, ...)``) to explain them, so a substring check against the
    raw method body matches the *explanation* and reports a failure against
    correct code.

    Built by re-rendering every statement in the body with :func:`ast.unparse`,
    which drops the docstring, all comments, and the original formatting.  The
    result is valid Python, so bracketed fragments read naturally
    (``int(dw)``), which is what makes an exact negative assertion possible.
    """
    source = _source(path)
    func = _find_function(source, name)
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


class TestTheArtworkIsScaledOnce:
    """The whole point: per-frame cost must depend on the DESTINATION, not the source."""

    def test_the_tick_path_blits_without_scaling(self) -> None:
        body = _method_body(_CANVAS, "_draw_artwork")
        assert "drawPixmap" in body, "the cached path must blit a pixmap 1:1"
        assert "drawPixmap(rect.left(), rect.top(), pixmap)" in body, (
            "the blit must carry no target rect, or Qt scales it again"
        )

    def test_the_scaled_path_sets_no_render_hint(self) -> None:
        """A render hint inside the cached path would re-enable the resample."""
        body = _method_body(_CANVAS, "_draw_artwork")
        cached = body.split("Fallback")[0]
        assert "SmoothPixmapTransform" not in cached, (
            "the 1:1 blit must not enable smooth transforms — that is the cost"
        )

    def test_the_scale_happens_in_the_cache_helper(self) -> None:
        body = _method_body(_CANVAS, "_scaled_artwork")
        assert ".scaled(" in body, "the scaling must live here"
        assert "SmoothTransformation" in body, "quality is why we pre-scale, not to skip it"
        assert "IgnoreAspectRatio" in body, (
            "the destination rect is already the final geometry — do not re-fit it"
        )

    def test_the_cache_key_contains_all_three_parts(self) -> None:
        """The key must discriminate on image AND both rects.

        Asserted on the parsed key expression rather than on the method source.
        The earlier version grepped the whole method for ``plan.artwork_src`` and
        friends — but those names are also used *after* the key, for the crop and
        the scale, so a mutation replacing the key with ``(id(image), None,
        None)`` still matched every substring and the test passed while the cache
        had stopped discriminating.  That is a fit-mode change serving a stale
        scale, which is the exact bug this cache could introduce.
        """
        source = _source(_CANVAS)
        func = _find_function(source, "_scaled_artwork")
        key_tuple = None
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Tuple)
                and any(isinstance(t, ast.Name) and t.id == "key" for t in node.targets)
            ):
                key_tuple = node.value
                break
        assert key_tuple is not None, "the key must be a tuple literal to be checkable"

        rendered = [ast.unparse(element) for element in key_tuple.elts]
        joined = " | ".join(rendered)
        assert any("id(image)" in part for part in rendered), f"image missing from key: {joined}"
        assert any("artwork_src" in part for part in rendered), f"source rect missing: {joined}"
        assert any("artwork_dst" in part for part in rendered), (
            f"destination rect missing: {joined}"
        )
        assert len(rendered) == 3, f"expected exactly the three parts, got: {joined}"

    def test_it_crops_before_scaling(self) -> None:
        """``overflow='crop'`` must not sample pixels the plan discards."""
        body = _method_body(_CANVAS, "_scaled_artwork")
        assert "image.copy(" in body, "crop to artwork_src first"
        crop_at = body.index("image.copy(")
        scale_at = body.index(".scaled(")
        assert crop_at < scale_at, "crop must precede the scale"

    def test_a_repeat_call_returns_the_cached_pixmap(self) -> None:
        """The cache must be consulted BEFORE any scaling work."""
        body = _method_body(_CANVAS, "_scaled_artwork")
        hit = body.index("return self._scaled_pixmap")
        scale = body.index(".scaled(")
        assert hit < scale, "an early return must precede the scale, or nothing is cached"

    def test_one_cache_per_layer(self) -> None:
        """Both crossfade layers need their own pixmap — they have different geometry."""
        body = _method_body(_CANVAS, "_scaled_artwork")
        assert "self._scaled_pixmap" in body, "incoming layer"
        assert "self._prev_scaled_pixmap" in body, "outgoing layer"


class TestTheCacheCannotOutliveItsLayer:
    """A pinned pixmap is ~7.5 MB; two of them on a 1 GB Pi is a real risk."""

    def test_the_pixmaps_are_dropped_when_the_layer_changes(self) -> None:
        body = _method_body(_CANVAS, "_store_layers")
        assert "self._scaled_pixmap = None" in body, "incoming pixmap must be released"
        assert "self._prev_scaled_pixmap = None" in body, "outgoing pixmap must be released"

    def test_the_release_test_uses_identity(self) -> None:
        """``is not`` on the image handle: free and exact, like the repaint guard."""
        body = _method_body(_CANVAS, "_store_layers")
        assert "is not self._scaled_image" in body
        assert "is not self._prev_scaled_image" in body

    def test_the_release_happens_after_the_layers_are_stored(self) -> None:
        """Comparing against the OLD image would never release anything."""
        body = _method_body(_CANVAS, "_store_layers")
        store = body.index("self._image = image")
        release = body.index("self._scaled_pixmap = None")
        assert store < release, "the release must see the NEW image to compare against"

    def test_only_two_pixmaps_are_ever_held(self) -> None:
        """Bounded by construction — there is no dict or list to grow."""
        source = _source(_CANVAS)
        assert "self._scaled_pixmap" in source
        assert "self._prev_scaled_pixmap" in source
        assert "self._scaled_pixmaps" not in source, "a collection here could grow unboundedly"


class TestNoSeamBetweenTheArtworkAndTheCurtain:
    """The artwork and the curtain must tile the screen with no unpainted column.

    Regression: the pixmap was sized ``int(dw)`` (truncated DOWN) while the
    curtain's clip used the rounded rect, which rounds its far edge UP.  For a
    fractional ``artwork_dst`` such as ``w = 1581.467`` the pixmap was 1581 px
    wide and the curtain started past 1582, so one column was painted by neither
    and the dark background showed through as a thin line at the artwork edge —
    the visible seam.

    Not an aliasing artefact: a rounding disagreement between two call sites that
    are supposed to be complements.  Both now take the rect from ``_qr``, which
    delegates to the shared ``display.geometry.int_rect`` — the same value the
    mpv widget is positioned from, because a seam there is just as visible.
    """

    def test_the_pixmap_uses_the_same_rect_as_the_curtain(self) -> None:
        """One rect, derived once, shared by both sides."""
        scaled = _method_body(_CANVAS, "_scaled_artwork")
        assert "_qr(plan.artwork_dst)" in scaled, (
            "the pixmap size must come from the shared rounded rect, not int(dw)"
        )
        # Check the CODE, not the prose.  The docstring and comments here quote
        # ``int(dw)`` precisely to explain the old bug, so grepping the whole
        # method matched the explanation and reported a false failure.  The
        # helper returns tokenised text, so the multi-token fragment is matched
        # without its punctuation spacing mattering.
        code = _code_without_docstring(_CANVAS, "_scaled_artwork")
        assert "int(dw)" not in code, "int(dw) truncates, which is what left the seam"
        assert "int(dh)" not in code, "int(dh) truncates, which is what left the seam"

    def test_the_blit_uses_that_rects_origin(self) -> None:
        body = _method_body(_CANVAS, "_draw_artwork")
        assert "_qr(plan.artwork_dst)" in body
        assert "rect.left(), rect.top()" in body, (
            "the blit origin must come from the same rect as the size"
        )

    def test_the_curtain_still_clips_to_the_complement(self) -> None:
        """The curtain's side of the contract must not drift."""
        body = _method_body(_CANVAS, "_paint_transition_curtain")
        assert "_qr(plan.artwork_dst)" in body
        assert "subtracted" in body

    def test_the_fallback_uses_the_same_rect_too(self) -> None:
        """A fallback frame must not reintroduce the seam."""
        body = _method_body(_CANVAS, "_draw_artwork")
        tail = body[body.index("Fallback") :]
        assert "_qr(plan.artwork_dst)" in tail, "the fallback draw must use the same rounded rect"

    def test_rounding_is_outward_so_the_artwork_can_only_overlap(self) -> None:
        """Overlapping by a pixel is invisible; a gap is a visible line.

        This is the asymmetry the shared helper exists for, and it is why the
        pixmap must match it rather than round to nearest.  The rule itself lives
        in ``display.geometry`` — ``test_geometry.py`` pins its behaviour — and
        the canvas must consume it rather than re-derive it.
        """
        assert "int(x + w + 0.9999)" in _source(_GEOMETRY), "the far edge must round outward"
        assert "int_rect(rect)" in _source(_CANVAS), (
            "the canvas must consume the shared helper, not restate the rule"
        )


class TestFailureDegradesRatherThanBlanks:
    """A refused allocation must not leave an empty frame."""

    def test_a_failed_scale_falls_back_to_the_direct_draw(self) -> None:
        body = _method_body(_CANVAS, "_draw_artwork")
        assert "if pixmap is not None:" in body
        assert "painter.drawImage(" in body, (
            "the original scaled draw is the fallback — correct, just slower"
        )

    def test_the_fallback_is_reachable_only_when_the_scale_fails(self) -> None:
        # Strip the docstring before indexing: the earlier attempt used
        # ``body.index("return")``, which matched the word inside the prose and
        # so passed for the wrong reason.
        code = _code_without_docstring(_CANVAS, "_draw_artwork")
        assert code.index("return") < code.index("drawImage"), (
            "the cached path must return before the fallback is reached"
        )

    def test_the_helper_can_report_failure(self) -> None:
        body = _method_body(_CANVAS, "_scaled_artwork")
        assert "return None" in body, "a failed scale must be reported, not raised"


class TestQtBehaviourWhenAvailable:
    """The real thing, on a machine that has PySide6 (the Pi, not CI)."""

    def test_a_repeat_paint_reuses_one_pixmap(self) -> None:
        pytest.importorskip("PySide6", reason="PySide6 not installed (CI/desktop dev)")
        from PySide6.QtGui import QColor, QImage

        from metixel.display.qt_canvas import FrameCanvas
        from metixel.framing.layout import RenderPlan

        canvas = FrameCanvas()
        image = QImage(400, 300, QImage.Format.Format_RGB888)
        image.fill(QColor(10, 20, 30))
        plan = RenderPlan(
            screen=(0.0, 0.0, 1920.0, 1200.0),
            ambient=None,
            artwork_dst=(100.0, 100.0, 800.0, 600.0),
            artwork_src=(0.0, 0.0, 400.0, 300.0),
            whitespace=(),
            matte=(),
            moulding=(),
            matte_colour="#ffffff",
            whitespace_colour="#ffffff",
            ambient_colour="#101014",
            style="borderless",
            branch="virtual",
            overflow="fill",
        )

        first = canvas._scaled_artwork(plan, image)
        second = canvas._scaled_artwork(plan, image)

        assert first is not None
        assert first is second, "the second call must reuse the cached pixmap"
        assert first.width() == 800 and first.height() == 600, "scaled to the destination"

    def test_a_reused_image_object_with_new_geometry_rescales(self) -> None:
        """The key includes the rects, so an unchanged image is not enough to reuse."""
        pytest.importorskip("PySide6", reason="PySide6 not installed (CI/desktop dev)")
        from PySide6.QtGui import QColor, QImage

        from metixel.display.qt_canvas import FrameCanvas
        from metixel.framing.layout import RenderPlan

        canvas = FrameCanvas()
        image = QImage(400, 300, QImage.Format.Format_RGB888)
        image.fill(QColor(10, 20, 30))

        def plan_with(dst: tuple[float, float, float, float]) -> RenderPlan:
            return RenderPlan(
                screen=(0.0, 0.0, 1920.0, 1200.0),
                ambient=None,
                artwork_dst=dst,
                artwork_src=(0.0, 0.0, 400.0, 300.0),
                whitespace=(),
                matte=(),
                moulding=(),
                matte_colour="#ffffff",
                whitespace_colour="#ffffff",
                ambient_colour="#101014",
                style="borderless",
                branch="virtual",
                overflow="fill",
            )

        first = canvas._scaled_artwork(plan_with((0.0, 0.0, 800.0, 600.0)), image)
        second = canvas._scaled_artwork(plan_with((0.0, 0.0, 400.0, 300.0)), image)

        assert first is not None and second is not None
        assert first is not second, "different geometry must not reuse the pixmap"
        assert second.width() == 400, "the new destination is honoured"


class TestTheCropIsMappedIntoTheImageThatIsDrawn:
    """``artwork_src`` is in the plan's media pixels, which need not be the image's.

    Regression: a video is laid out against the **video's** dimensions
    (``video.py::_build_item`` records the probe's width/height) but drawn as its
    pre-generated first-frame poster, which ffmpeg has already scaled to fit the
    screen.  For the 1080x1920 sample on a 1920x1200 panel the plan's crop window
    is ``(0, 623.8, 1080, 672.4)`` in video pixels while the poster is 676x1200,
    so the window ran 404 px past the poster's right edge.  ``QImage.copy`` does
    NOT clip an out-of-range rectangle — it returns one of the requested size and
    pads the overhang black — and that crop was then scaled up to the whole
    screen: the poster in the top-left corner, black everywhere else.

    Invisible whenever the source video is no larger than the panel, because the
    fit-inside scale filter is then a no-op and the poster matches the media
    exactly.  The 1920x1080 landscape sample is exactly that case.
    """

    def test_the_cached_path_maps_before_it_crops(self) -> None:
        code = _code_without_docstring(_CANVAS, "_scaled_artwork")
        assert "plan.source_window(image.width(), image.height())" in code
        assert "image.copy(window)" in code
        assert "image.copy(QRect(" not in code, (
            "cropping with the raw plan rect is the bug: it overruns the image and "
            "QImage.copy pads the overhang black instead of clipping it"
        )

    def test_the_fallback_draw_maps_too(self) -> None:
        code = _code_without_docstring(_CANVAS, "_draw_artwork")
        assert "plan.source_window(source_image.width(), source_image.height())" in code
        assert "float(int(sx))" not in code, (
            "Qt CLIPS an over-long source rect and rescales what it found, "
            "so a wrong window is merely wrong differently"
        )

    def test_the_tk_backend_maps_too(self) -> None:
        """Desktop dev must not disagree with the panel it stands in for."""
        body = _method_body(_TK_BACKEND, "_artwork")
        assert "plan.source_window(pil_img.width, pil_img.height)" in body


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


class TestThePosterOverhangIsNeverPainted:
    """The real pixels, where PySide6 exists (the Pi, not CI)."""

    def test_a_portrait_video_poster_fills_the_frame(self, canvas) -> None:
        """A white poster must stay white to every corner of the frame."""
        from PySide6.QtGui import QColor, QImage

        from metixel.framing.layout import LayoutEngine
        from metixel.framing.resolve import MediaSize

        # Laid out against the VIDEO (1080x1920); drawn from the POSTER (676x1200).
        plan = LayoutEngine(1920, 1200, style="borderless", overflow="crop").compute(
            MediaSize(1080, 1920, "video")
        )
        poster = QImage(676, 1200, QImage.Format.Format_RGB888)
        poster.fill(QColor(255, 255, 255))

        pixmap = canvas._scaled_artwork(plan, poster)
        assert pixmap is not None, "the crop still found something to draw"

        painted = pixmap.toImage()
        corners = (
            (0, 0),
            (painted.width() - 1, 0),
            (0, painted.height() - 1),
            (painted.width() - 1, painted.height() - 1),
        )
        for x, y in corners:
            colour = painted.pixelColor(x, y)
            assert colour.red() > 200, (
                f"({x}, {y}) is {colour.name()} — the poster's overhang was padded black"
            )
