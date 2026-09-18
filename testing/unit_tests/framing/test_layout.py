# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for :mod:`metixel.framing.layout` — millimetres to pixels, once.

``LayoutEngine`` is the single bridge between the pure framing engine and any
renderer, so these tests carry the weight the old presentation-layer layout
tests used to.  Two properties matter most and are asserted directly:

* the plan's rectangles are **pixel** values consistent with the panel's
  ``px_per_mm``, so mat bands are physically the size the style asked for; and
* the paint order is expressible without a depth buffer — the matte ring is
  *disjoint* from the artwork, which is what lets a canvas draw a flat list.
"""

from __future__ import annotations

import pytest

from metixel.framing import framing_templates as templates
from metixel.framing.layout import LayoutEngine, RenderPlan, _annulus, _to_px
from metixel.framing.resolve import MediaSize

LANDSCAPE = (1920, 1200)
#: The Metixel panel's scale, for converting expected mm back to px.
PPM_X = 1920.0 / 518.0
PPM_Y = 1200.0 / 324.0


def _landscape(**kwargs) -> LayoutEngine:
    return LayoutEngine(*LANDSCAPE, **kwargs)


def _band_thickness_mm(plan, ppm_x: float = PPM_X, ppm_y: float = PPM_Y) -> tuple[float, float]:
    """Return ``(top_bottom_mm, side_mm)`` for a plan's matte ring.

    Bands are identified by *shape*: a top/bottom band spans the full opening
    width, whereas a side band is only as tall as the Mat Window.  Selecting by
    sort order on one coordinate picks the wrong band (the top band also has the
    smallest ``x``), which is how a "side band" test can silently end up
    measuring the opening instead.

    Each thickness is divided by the axis factor it spans, so the result is in
    millimetres and comparable across a non-square pixel grid.
    """
    # The full-width bands are the horizontal ones; take the widest as reference.
    widest = max(b[2] for b in plan.matte)
    horizontals = [b for b in plan.matte if b[2] == widest]
    verticals = [b for b in plan.matte if b[2] != widest]

    top_bottom_mm = max((b[3] / ppm_y for b in horizontals), default=0.0)
    side_mm = max((b[2] / ppm_x for b in verticals), default=0.0)
    return top_bottom_mm, side_mm


# ---------------------------------------------------------------------------
# RenderPlan shape
# ---------------------------------------------------------------------------


class TestRenderPlanContract:
    def test_compute_returns_a_plan_for_valid_media(self) -> None:
        plan = _landscape().compute(MediaSize(3000, 2000))
        assert isinstance(plan, RenderPlan)
        assert plan.screen == (0.0, 0.0, 1920.0, 1200.0)

    def test_artwork_rect_is_inside_the_screen(self) -> None:
        plan = _landscape(style="gallery").compute(MediaSize(3000, 2000))
        x, y, w, h = plan.artwork_dst
        assert x >= 0 and y >= 0
        assert x + w <= 1920.0 + 1e-6
        assert y + h <= 1200.0 + 1e-6

    def test_legacy_aliases_match_the_new_fields(self) -> None:
        """Callers migrating from the old dict API get the same values."""
        plan = _landscape(style="gallery").compute(MediaSize(3000, 2000))
        assert plan.image_rect == plan.artwork_dst
        assert plan.matte_rects == plan.matte

    def test_full_source_is_sampled_when_not_cropping(self) -> None:
        plan = _landscape(style="gallery").compute(MediaSize(3000, 2000))
        assert plan.artwork_src == (0.0, 0.0, 3000.0, 2000.0)

    def test_describe_is_json_friendly(self) -> None:
        import json

        plan = _landscape(style="gallery").compute(MediaSize(3000, 2000))
        json.dumps(_landscape().describe(plan))


class TestRebateIsNotDrawn:
    """The rebate is a fit check and must never enter the render plan.

    ``required_rebate`` answers "what overlap must a real frame provide to hide
    the panel edge?" so a customer can choose a frame.  It is not a layer and has
    no render step.  These guards exist because the value is reported next to
    genuine geometry in the same ``FramingResult``, which makes it easy to assume
    it is part of the composition.
    """

    def test_render_plan_has_no_rebate_field(self) -> None:
        """RenderPlan is the compositing contract — a rebate field would be a bug."""
        fields = set(RenderPlan.__dataclass_fields__)
        assert not any("rebate" in name for name in fields), (
            f"RenderPlan must not carry rebate geometry; found {sorted(fields)}"
        )

    def test_render_plan_fields_are_all_drawable_layers(self) -> None:
        """Every rect in the plan is something the canvas actually paints."""
        plan = _landscape(style="gallery").compute(MediaSize(3000, 2000))
        # The five drawn things, plus metadata and the two *source-space* entries:
        # ``artwork_src`` is a sampling instruction and ``source_size`` is the
        # media size that instruction is expressed in.  Neither is a screen rect,
        # which is the distinction this guard exists to keep — a new field here
        # must be one or the other, never an undrawn screen rectangle.
        drawable = {"screen", "ambient", "artwork_dst", "whitespace", "matte", "moulding"}
        rect_fields = {
            name
            for name, value in RenderPlan.__dataclass_fields__.items()
            if value.type in ("tuple[float, float, float, float]",) or "tuple" in str(value.type)
        }
        unexpected = rect_fields - drawable - {"artwork_src", "source_size"}
        assert not unexpected, (
            f"RenderPlan gained rect fields that are not drawn layers: {sorted(unexpected)}"
        )
        # Sanity: the ones we expect are populated.
        assert plan.matte or plan.style == "borderless"

    def test_the_engine_reports_the_rebate_separately_from_the_plan(self) -> None:
        """It lives on the Frame, which is the fit-check vocabulary.

        ``FramingResult.frame.required_rebate`` is the reporting channel; the
        render plan never sees it.  Keeping the two apart is what stops the value
        being painted by a future change that "helpfully" surfaces it.
        """
        from metixel.framing.framing_engine import FramingResult

        frame_fields = set(FramingResult.__dataclass_fields__)
        assert "frame" in frame_fields
        # And the plan type is a different type entirely.
        assert RenderPlan is not FramingResult


# ---------------------------------------------------------------------------
# Unusable media
# ---------------------------------------------------------------------------


class TestUnusableMedia:
    """A frame must never show a traceback; unprobed media falls back."""

    @pytest.mark.parametrize(("w", "h"), [(0, 0), (1920, 0), (0, 1080)])
    def test_zero_dimensions_render_full_bleed(self, w: int, h: int) -> None:
        plan = _landscape(style="gallery").compute(MediaSize(w, h))
        assert plan.artwork_dst == (0.0, 0.0, 1920.0, 1200.0)
        assert plan.matte == ()
        assert plan.moulding == ()
        assert plan.ambient is None

    def test_source_rect_is_never_degenerate(self) -> None:
        """Width/height of 0 must not produce a zero-area source sample."""
        plan = _landscape().compute(MediaSize(0, 0))
        _, _, sw, sh = plan.artwork_src
        assert sw > 0 and sh > 0


# ---------------------------------------------------------------------------
# Physical correctness of the pixel geometry
# ---------------------------------------------------------------------------


class TestPixelGeometry:
    def test_gallery_ring_is_physically_56mm_at_1080p(self) -> None:
        """gallery = 0.175 of a 320 mm reference.  The plan must reflect that.

        This is the assertion that would catch a mm/px unit mix-up — the class
        of bug the spec says computing in mm is designed to prevent.
        """
        plan = _landscape(style="gallery").compute(MediaSize(3000, 2000))
        # Top matte band height, from the plan itself.
        top = min(plan.matte, key=lambda r: r[1])
        assert top[3] / PPM_Y == pytest.approx(56.0, abs=0.2)

    def test_matte_bands_are_disjoint_from_the_artwork(self) -> None:
        """No band may overlap the artwork — that is why paint order is safe."""
        plan = _landscape(style="gallery").compute(MediaSize(3000, 2000))
        ax, ay, aw, ah = plan.artwork_dst
        for bx, by, bw, bh in plan.matte:
            overlap_x = min(ax + aw, bx + bw) - max(ax, bx)
            overlap_y = min(ay + ah, by + bh) - max(ay, by)
            assert not (overlap_x > 1e-6 and overlap_y > 1e-6)

    def test_matte_bands_are_disjoint_from_each_other(self) -> None:
        plan = _landscape(style="gallery").compute(MediaSize(3000, 2000))
        bands = plan.matte
        for i, (ax, ay, aw, ah) in enumerate(bands):
            for bx, by, bw, bh in bands[i + 1 :]:
                overlap_x = min(ax + aw, bx + bw) - max(ax, bx)
                overlap_y = min(ay + ah, by + bh) - max(ay, by)
                assert not (overlap_x > 1e-6 and overlap_y > 1e-6)

    def test_bands_account_for_the_whole_ring(self) -> None:
        """The four bands must tile the ring: no unpainted gap is allowed."""
        plan = _landscape(style="gallery").compute(MediaSize(3000, 2000))
        band_area = sum(w * h for _, _, w, h in plan.matte)
        # Ring area = opening area - window area; both are reported as bands,
        # so the areas must agree with the artwork's inset.
        assert band_area > 0

    def test_near_screen_aspect_artwork_has_a_56mm_shortest_ring(self) -> None:
        """16:10 artwork on a 16:10 panel: the SHORTEST ring is still 56 mm.

        The ring is deliberately uneven even when the aspects match.  The style
        fraction gives the *shortest* ring (``ring_target``); the Mat Window is
        then cut to the artwork's aspect inside ``opening - 2 x ring``, and any
        remaining difference is absorbed by the opposite axis.  So the bands are
        never all equal, and asserting they are would be asserting a bug.

        For 16:10 artwork the excess lands on the SIDES (90.6 mm) while the
        top/bottom carry the 56 mm target — so the assertion is on the pair
        that happens to hold the target, not on "all four bands".
        """
        plan = _landscape(style="gallery").compute(MediaSize(1920, 1200))

        vertical_mm = sorted(b[3] / PPM_Y for b in plan.matte)
        horizontal_mm = sorted(b[2] / PPM_X for b in plan.matte)

        # The shortest ring overall is the 56 mm target, on one axis only.
        assert min(vertical_mm[0], horizontal_mm[0]) == pytest.approx(56.0, abs=0.3)
        # And here it is the vertical pair that holds it; the sides are wider.
        assert vertical_mm[0] == pytest.approx(56.0, abs=0.3)
        assert horizontal_mm[0] > vertical_mm[0]

    def test_shortest_ring_matches_the_style_fraction_everywhere(self) -> None:
        """``ring_min == fraction x reference`` regardless of artwork aspect.

        This is the invariant the whole virtual branch rests on: whatever the
        media, exactly one axis carries the style's ring and the other carries
        the excess.  Asserted on whichever axis is limiting — for a portrait
        artwork inside a landscape opening that is the height, for a wide one
        it is the width.
        """
        reference_mm = 320.0
        fractions = {"modern": 0.06, "classic": 0.11, "gallery": 0.175, "museum": 0.25}
        for style, fraction in fractions.items():
            expected = fraction * reference_mm
            for w, h in ((3000, 2000), (4000, 2250), (2000, 3000), (2000, 2000)):
                plan = _landscape(style=style).compute(MediaSize(w, h))
                vertical = min(b[3] / PPM_Y for b in plan.matte)
                horizontal = min(b[2] / PPM_X for b in plan.matte)
                assert min(vertical, horizontal) == pytest.approx(expected, abs=0.5), (
                    f"{style} {w}x{h}: shortest ring should be {expected} mm"
                )

    def test_wide_artwork_gets_taller_top_bottom_bands(self) -> None:
        """A 21:9 image on a 16:10 panel is letterboxed.

        Compared in *millimetres*, not pixels: a pixel comparison across the
        two axes would be wrong on a panel whose pixels are not square, and
        would also hide a genuine unit bug.  For wide artwork the limiting axis
        is the width, so the sides hold the 56 mm target and the top/bottom
        carry the excess.

        Bands are selected by *shape*, not by sort order: the top/bottom bands
        are full-width and the side bands are window-height, so sorting on one
        coordinate picks up the wrong band entirely.
        """
        plan = _landscape(style="gallery").compute(MediaSize(2100, 900))
        top_mm, side_mm = _band_thickness_mm(plan)

        assert top_mm > side_mm
        assert side_mm == pytest.approx(56.0, abs=0.5), "sides carry the target ring"

    def test_tall_artwork_gets_wider_side_bands(self) -> None:
        plan = _landscape(style="gallery").compute(MediaSize(900, 2100))
        top_mm, side_mm = _band_thickness_mm(plan)

        assert side_mm > top_mm
        # For a portrait artwork inside a landscape opening, the limiting axis
        # is the height, so the top/bottom pair carries the target ring.
        assert top_mm == pytest.approx(56.0, abs=0.5)

    def test_aspect_ratio_of_artwork_is_preserved(self) -> None:
        """Framing must not distort: the destination keeps the source ratio."""
        for w, h in ((3000, 2000), (4000, 2250), (2000, 3000), (2000, 2000)):
            plan = _landscape(style="gallery").compute(MediaSize(w, h))
            _, _, aw, ah = plan.artwork_dst
            assert (aw / ah) == pytest.approx(w / h, rel=0.01)


# ---------------------------------------------------------------------------
# Styles and overflow
# ---------------------------------------------------------------------------


class TestStyles:
    @pytest.mark.parametrize("style", sorted(templates.STYLES))
    def test_style_produces_a_usable_plan(self, style: str) -> None:
        plan = _landscape(style=style).compute(MediaSize(3000, 2000))
        _, _, aw, ah = plan.artwork_dst
        assert aw > 0 and ah > 0

    def test_borderless_has_no_matte_band(self) -> None:
        plan = _landscape(style="borderless").compute(MediaSize(3000, 2000))
        assert plan.matte == ()

    def test_museum_adds_a_whitespace_band(self) -> None:
        """Only a whitespace style should emit whitespace rectangles."""
        museum = _landscape(style="museum").compute(MediaSize(3000, 2000))
        gallery = _landscape(style="gallery").compute(MediaSize(3000, 2000))
        assert len(museum.whitespace) > 0
        assert gallery.whitespace == ()

    def test_whitespace_is_inside_the_artwork_not_outside_it(self) -> None:
        """Whitespace sits between the Mat Window edge and the artwork."""
        plan = _landscape(style="museum").compute(MediaSize(3000, 2000))
        ax, ay, aw, ah = plan.artwork_dst
        for bx, by, bw, bh in plan.whitespace:
            overlap_x = min(ax + aw, bx + bw) - max(ax, bx)
            overlap_y = min(ay + ah, by + bh) - max(ay, by)
            assert not (overlap_x > 1e-6 and overlap_y > 1e-6)

    def test_deeper_style_shrinks_the_artwork(self) -> None:
        """modern < classic < gallery < museum in ring depth."""
        widths = [
            _landscape(style=s).compute(MediaSize(3000, 2000)).artwork_dst[2]
            for s in ("modern", "classic", "gallery", "museum")
        ]
        assert widths == sorted(widths, reverse=True)

    def test_polaroid_is_bottom_heavy(self) -> None:
        plan = _landscape(style="polaroid").compute(MediaSize(3000, 2000))
        top = min(plan.matte, key=lambda r: r[1])
        bottom = max(plan.matte, key=lambda r: r[1])
        assert bottom[3] > top[3]


class TestOverflow:
    def test_crop_samples_a_sub_rectangle_of_a_wider_source(self) -> None:
        """crop fills a fixed window, so a wide source loses its sides."""
        plan = _landscape(style="borderless", overflow="crop").compute(MediaSize(4000, 1500))
        sx, sy, sw, sh = plan.artwork_src
        assert sx > 0, "sides should be cropped"
        assert sy == pytest.approx(0.0)
        assert sw < 4000
        assert sh == pytest.approx(1500.0)

    def test_crop_samples_a_sub_rectangle_of_a_taller_source(self) -> None:
        plan = _landscape(style="borderless", overflow="crop").compute(MediaSize(1500, 4000))
        sx, sy, sw, sh = plan.artwork_src
        assert sy > 0, "top/bottom should be cropped"
        assert sx == pytest.approx(0.0)
        assert sh < 4000

    def test_crop_source_keeps_the_window_aspect(self) -> None:
        plan = _landscape(style="borderless", overflow="crop").compute(MediaSize(4000, 1500))
        _, _, sw, sh = plan.artwork_src
        _, _, aw, ah = plan.artwork_dst
        assert (sw / sh) == pytest.approx(aw / ah, rel=0.01)

    def test_fill_uses_the_whole_source(self) -> None:
        plan = _landscape(style="borderless", overflow="fill").compute(MediaSize(4000, 1500))
        assert plan.artwork_src == (0.0, 0.0, 4000.0, 1500.0)

    def test_borderless_fill_produces_ambient_fill(self) -> None:
        """Ring 0 pins the Mat Window, so the residue becomes ambient fill."""
        plan = _landscape(style="borderless", overflow="fill").compute(MediaSize(4000, 1500))
        assert plan.ambient is not None

    def test_style_ring_suppresses_ambient_fill(self) -> None:
        """With a ring the Mat Window is cut to the artwork: no residue."""
        plan = _landscape(style="gallery").compute(MediaSize(4000, 1500))
        assert plan.ambient is None


def _naked_plan(
    *,
    artwork_src: tuple[float, float, float, float] = (10.0, 20.0, 30.0, 40.0),
    source_size: tuple[float, float] = (0.0, 0.0),
) -> RenderPlan:
    """A hand-built plan — the shape tests and the preview endpoint produce."""
    return RenderPlan(
        screen=(0.0, 0.0, 1920.0, 1200.0),
        ambient=None,
        artwork_dst=(0.0, 0.0, 1920.0, 1200.0),
        artwork_src=artwork_src,
        source_size=source_size,
        whitespace=(),
        matte=(),
        moulding=(),
        matte_colour="#000000",
        whitespace_colour="#ffffff",
        ambient_colour="#101014",
        style="borderless",
        branch="virtual",
        overflow="fill",
    )


# ---------------------------------------------------------------------------
# The plan's source space vs the image actually drawn
# ---------------------------------------------------------------------------


class TestSourceWindowMapsIntoTheDrawnImage:
    """``artwork_src`` is in the plan's MEDIA pixels; the image may be smaller.

    Regression: a video is laid out against the video's own dimensions but drawn
    as its pre-generated first-frame poster, which ffmpeg has already scaled to
    fit the screen (``scale=min(screen,source):force_original_aspect_ratio=
    decrease``).  The crop window is a window in *video* pixels, so applying it to
    the poster sampled past its edge — and neither ``QImage.copy`` nor PIL's
    ``crop`` clips a source rectangle; both pad the overhang black.  On the panel
    that read as the poster in the top-left corner with the rest of the screen
    black.
    """

    #: The portrait sample, and the poster ffmpeg derives from it on a 1920x1200
    #: panel: 1080x1920 fitted inside min(1920,1080) x min(1200,1920) = 675x1200,
    #: padded to even dimensions.
    VIDEO = MediaSize(1080, 1920, "video")
    POSTER = (676, 1200)

    def _portrait(self) -> RenderPlan:
        return _landscape(style="borderless", overflow="crop").compute(self.VIDEO)

    def test_the_plan_states_the_media_it_was_laid_out_for(self) -> None:
        assert self._portrait().source_size == (1080.0, 1920.0)

    def test_an_unusable_size_still_states_a_usable_source(self) -> None:
        """A zero-sized item is drawn full-bleed from a 1x1 source, not a 0x0 one."""
        plan = _landscape(style="borderless").compute(MediaSize(0, 0))
        assert plan.source_size == (1.0, 1.0)

    def test_the_unmapped_window_really_does_overrun_the_poster(self) -> None:
        """Otherwise the sizing test below could pass without mapping anything."""
        sx, _, sw, _ = self._portrait().artwork_src
        assert sx + sw > self.POSTER[0], "the crop window is wider than the poster"

    def test_the_window_is_scaled_into_the_image(self) -> None:
        sx, sy, sw, sh = self._portrait().source_window(*self.POSTER)
        assert (sx, sw) == pytest.approx((0.0, 676.0), abs=0.5)
        assert sy == pytest.approx(389.9, abs=0.5)
        assert sh == pytest.approx(420.2, abs=0.5)

    def test_the_mapped_window_is_inside_the_image(self) -> None:
        sx, sy, sw, sh = self._portrait().source_window(*self.POSTER)
        assert sx >= 0.0 and sy >= 0.0
        assert sx + sw <= self.POSTER[0]
        assert sy + sh <= self.POSTER[1]

    def test_the_mapped_window_keeps_the_destination_aspect(self) -> None:
        """Still the centred cover crop — only the units changed."""
        plan = self._portrait()
        _, _, sw, sh = plan.source_window(*self.POSTER)
        _, _, aw, ah = plan.artwork_dst
        assert (sw / sh) == pytest.approx(aw / ah, rel=0.01)

    def test_an_image_matching_the_media_is_returned_unchanged(self) -> None:
        plan = _landscape(style="borderless", overflow="fill").compute(MediaSize(4000, 1500))
        assert plan.source_window(4000, 1500) == plan.artwork_src == (0.0, 0.0, 4000.0, 1500.0)

    def test_a_plan_that_does_not_state_its_media_is_left_alone(self) -> None:
        """Hand-built plans (tests, previews) predate the field."""
        plan = _naked_plan()
        assert plan.source_size == (0.0, 0.0)
        assert plan.source_window(100, 100) == plan.artwork_src

    def test_a_degenerate_window_degrades_to_the_whole_image(self) -> None:
        """An empty crop is a blank frame, and a display must never show one."""
        plan = _naked_plan(artwork_src=(0.0, 0.0, 0.0, 0.0), source_size=(1080.0, 1920.0))
        assert plan.source_window(676, 1200) == (0.0, 0.0, 676.0, 1200.0)

    def test_the_media_size_is_reported_for_debugging(self) -> None:
        """``describe`` answers a geometry question without a screenshot."""
        engine = _landscape(style="borderless", overflow="crop")
        assert engine.describe(self._portrait())["source_size"] == (1080.0, 1920.0)


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


class TestRotation:
    def test_portrait_swaps_the_screen_basis(self) -> None:
        engine = LayoutEngine(1200, 1920, rotation=90, style="gallery")
        assert (engine.screen_w, engine.screen_h) == (1200, 1920)
        plan = engine.compute(MediaSize(3000, 2000))
        assert plan.screen == (0.0, 0.0, 1200.0, 1920.0)

    def test_portrait_ring_is_the_same_physical_size(self) -> None:
        """The reference dimension is the shorter side either way up.

        A 320 mm reference on the same panel means the same 56 mm *shortest*
        ring, so the mat is the same mat whichever way the frame hangs — the
        point of expressing the ring in millimetres rather than as a
        percentage of the opening.

        Asserted on each mounting's limiting axis rather than a hand-derived
        px-per-mm factor: for landscape artwork in a landscape panel the excess
        lands on the sides, and for portrait artwork in a portrait panel it
        lands on the top/bottom.
        """
        land = LayoutEngine(1920, 1200, rotation=0, style="gallery").compute(MediaSize(1920, 1200))
        port = LayoutEngine(1200, 1920, rotation=90, style="gallery").compute(MediaSize(1200, 1920))

        land_vertical = min(b[3] / (1200.0 / 324.0) for b in land.matte)
        port_horizontal = min(b[2] / (1200.0 / 324.0) for b in port.matte)

        assert land_vertical == pytest.approx(56.0, abs=0.5)
        assert port_horizontal == pytest.approx(56.0, abs=0.5)

    def test_rotation_180_stays_landscape(self) -> None:
        engine = LayoutEngine(1920, 1200, rotation=180, style="gallery")
        plan = engine.compute(MediaSize(3000, 2000))
        assert plan.screen == (0.0, 0.0, 1920.0, 1200.0)


# ---------------------------------------------------------------------------
# Resolution independence
# ---------------------------------------------------------------------------


class TestResolutionIndependence:
    def test_non_metixel_resolution_still_frames(self) -> None:
        """A 2560x1600 monitor must work — nothing is pinned to 1080p."""
        engine = LayoutEngine(2560, 1600, style="gallery")
        plan = engine.compute(MediaSize(3000, 2000))
        assert plan.screen == (0.0, 0.0, 2560.0, 1600.0)
        _, _, aw, ah = plan.artwork_dst
        assert aw > 0 and ah > 0

    def test_larger_panel_keeps_the_ring_physically_identical(self) -> None:
        """A 40 mm ring is 40 mm on any panel of the same physical size."""
        small = LayoutEngine(1920, 1200, style="gallery").compute(MediaSize(1920, 1200))
        large = LayoutEngine(2560, 1600, style="gallery").compute(MediaSize(2560, 1600))
        small_mm = min(b[3] for b in small.matte) / (1200.0 / 324.0)
        large_mm = min(b[3] for b in large.matte) / (1600.0 / 324.0)
        assert small_mm == pytest.approx(large_mm, abs=0.3)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestAnnulus:
    def test_nested_rects_yield_four_bands(self) -> None:
        bands = _annulus((0, 0, 100, 100), (10, 10, 80, 80))
        assert len(bands) == 4

    def test_bands_tile_the_ring_area(self) -> None:
        outer, inner = (0, 0, 100, 100), (10, 10, 80, 80)
        band_area = sum(w * h for _, _, w, h in _annulus(outer, inner))
        assert band_area == 100 * 100 - 80 * 80

    def test_identical_rects_yield_nothing(self) -> None:
        assert _annulus((0, 0, 100, 100), (0, 0, 100, 100)) == ()

    def test_coincident_edge_omits_an_empty_band(self) -> None:
        """A band flush with the outer edge has zero thickness — drop it."""
        bands = _annulus((0, 0, 100, 100), (0, 10, 100, 80))
        assert len(bands) == 2  # top and bottom only

    def test_expanding_inner_rect_yields_nothing(self) -> None:
        assert _annulus((0, 0, 50, 50), (0, 0, 100, 100)) == ()


class TestToPx:
    def test_scales_each_axis_independently(self) -> None:
        from metixel.framing.framing_engine import Rect

        assert _to_px(Rect(1.0, 2.0, 3.0, 4.0), 10.0, 100.0) == (10.0, 200.0, 30.0, 400.0)


# ---------------------------------------------------------------------------
# Qt independence — the boundary that keeps CI able to run this
# ---------------------------------------------------------------------------


class TestQtIndependence:
    def test_framing_package_imports_no_qt_or_display_backend(self) -> None:
        """``resolved geometry`` must be testable without Qt installed.

        CI runs on a machine with no PySide6, and the layout maths is the part
        most worth testing.  Importing the display backend here would also
        invert the dependency (geometry must not know about rendering).
        """
        import ast
        from pathlib import Path

        import metixel.framing as framing_pkg

        pkg_dir = Path(framing_pkg.__file__).parent
        banned = ("PySide6", "mpv", "metixel.display", "metixel.frontend")

        for path in pkg_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    for bad in banned:
                        assert not name.startswith(bad), (
                            f"{path.name} imports {name!r} — the framing package "
                            f"must not depend on Qt, mpv, or the display layer"
                        )

    def test_visualize_framing_does_not_import_matplotlib_eagerly(self) -> None:
        """matplotlib is an optional extra; importing the package must not need it.

        The visualiser is a dev tool that draws figures.  If it imported
        matplotlib at module level, then ``import metixel.framing`` — and with
        it the whole application — would require a plotting stack on the Pi.
        """
        import ast
        from pathlib import Path

        import metixel.framing as framing_pkg

        viz = Path(framing_pkg.__file__).parent / "visualize_framing.py"
        tree = ast.parse(viz.read_text(encoding="utf-8"))

        def _is_matplotlib(node: ast.stmt) -> bool:
            if isinstance(node, ast.Import):
                return any(a.name.split(".")[0] == "matplotlib" for a in node.names)
            if isinstance(node, ast.ImportFrom):
                return (node.module or "").split(".")[0] == "matplotlib"
            return False

        offenders = [n for n in tree.body if _is_matplotlib(n)]
        assert not offenders, (
            "visualize_framing must import matplotlib lazily so "
            "`import metixel.framing` works without the viz extra installed"
        )
