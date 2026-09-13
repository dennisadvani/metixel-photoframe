# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""pytest suite for the Metixel Adaptive Framing Engine.

Specification: ``docs/geometry-model.md``.

The suite covers both branches (physical / virtual), the style presets, the
derived ambient fill, the units policy, the output views, and the invariant
checker — plus a cross-product sweep over styles, branch, artwork aspect,
whitespace and overflow.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from metixel.framing import framing_templates as templates
from metixel.framing.framing_engine import (
    MAX_FOCAL_SHIFT_Y,
    PANORAMA_THRESHOLD,
    PORTRAIT_THRESHOLD,
    SQUARE_MAX,
    Face,
    FocalPoint,
    FramingRequest,
    Insets,
    MediaDescriptor,
    MediaType,
    Rect,
    Screen,
    WhitespaceSpec,
    calculate_framing,
    check_invariants,
    classify_aspect,
    ring_target_mm,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SCREEN = templates.METIXEL_16_10_1920x1200

# 518 - 2*2 = 514, 324 - 2*2 = 320 -> reference dimension (shorter side) = 320
FRAME_OPENING_VIRTUAL = Rect(2.0, 2.0, 514.0, 320.0)
DEFAULT_WINDOW_PHYSICAL = Rect(2.0, 2.0, 514.0, 320.0)

STYLE_NAMES = [n for n in templates.STYLES if n not in ("custom", "borderless")]
#: ``borderless`` is a virtual-only style: with no mat the Frame Opening cannot
#: reach past the panel, so a physical frame would expose the non-screen area.
VIRTUAL_ONLY_STYLES = ["borderless"]
ASPECTS = {
    "panorama_21_9": 21 / 9,
    "landscape_16_9": 16 / 9,
    "landscape_3_2": 3 / 2,
    "landscape_4_3": 4 / 3,
    "square_1_1": 1.0,
    "portrait_4_5": 4 / 5,
    "portrait_2_3": 2 / 3,
    "portrait_9_16": 9 / 16,
}


def media(aspect: float, type_: MediaType = "image") -> MediaDescriptor:
    """Build a MediaDescriptor with an exact aspect ratio."""
    if aspect >= 1.0:
        return MediaDescriptor(width=float(aspect), height=1.0, type=type_)
    return MediaDescriptor(width=1.0, height=1.0 / aspect, type=type_)


def request(
    style: str = "gallery",
    aspect: float = 3 / 2,
    *,
    physical: bool = False,
    **kwargs: Any,
) -> FramingRequest:
    return templates.build_request(SCREEN, media(aspect), style, physical=physical, **kwargs)


def close(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, abs_tol=tol)


# ---------------------------------------------------------------------------
# Reference dimension and unit conversion
# ---------------------------------------------------------------------------


class TestReferenceDimension:
    @pytest.mark.parametrize(
        ("w", "h", "expected"),
        [
            (400.0, 600.0, 400.0),
            (600.0, 400.0, 400.0),
            (1000.0, 1500.0, 1000.0),
            (1500.0, 1000.0, 1000.0),
        ],
    )
    def test_shorter_side_is_the_reference(self, w, h, expected):
        """The reference is the shorter side, in either orientation."""
        screen = Screen(width_mm=w, height_mm=h)
        assert close(screen.reference_dimension(0.0), expected)

    def test_metixel_reference_is_320(self):
        assert close(SCREEN.reference_dimension(2.0), 320.0)

    def test_ring_fraction_resolves_to_millimetres(self):
        # gallery is 12.5% -> 0.125 * 320 = 40 mm
        ring = ring_target_mm(SCREEN, 0.125, 2.0)
        assert close(ring.left, 40.0)
        assert close(ring.bottom, 40.0)

    def test_bottom_ring_override(self):
        ring = ring_target_mm(SCREEN, 0.10, 2.0, bottom_fraction=0.20)
        assert close(ring.left, 32.0)
        assert close(ring.bottom, 64.0)


class TestScreen:
    def test_metixel_geometry(self):
        # Nominally 16:10; 518/324 = 1.5988 because the panel is 518 mm, not
        # the 518.4 mm that would give exactly 1.6.
        assert close(SCREEN.aspect, 1.6, 2e-3)
        assert SCREEN.width_px == 1920
        assert SCREEN.height_px == 1200

    def test_pixels_are_effectively_square(self):
        """518 mm is the panel's real width, so its pixels are near-square.

        1920/518 = 3.7066 px/mm vs 1200/324 = 3.7037 px/mm — a 0.08% spread.
        A 518.4 mm width would make them exactly square; the geometry is
        per-axis so either is handled correctly.
        """
        sx, sy = SCREEN.px_per_mm
        assert sx is not None and sy is not None
        assert abs(sx - sy) / sy < 1e-3
        assert close(SCREEN.mm_per_px, 0.27, 1e-3)

    def test_px_per_mm_is_per_axis(self):
        sx, sy = SCREEN.px_per_mm
        assert sx is not None and sy is not None
        assert close(sx, 1920.0 / 518.0)
        assert close(sy, 1200.0 / 324.0)

    def test_inset_applies_margin_per_side(self):
        rect = SCREEN.inset(2.0)
        assert close(rect.width, 514.0)
        assert close(rect.height, 320.0)

    @pytest.mark.parametrize("bad", [0, -1, 0.0])
    def test_non_positive_screen_rejected(self, bad):
        with pytest.raises(ValueError):
            Screen(width_mm=bad, height_mm=324.0)

    def test_oversized_margin_rejected(self):
        with pytest.raises(ValueError):
            SCREEN.inset(200.0)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        ("ar", "expected"),
        [
            (0.4, "portrait"),
            (PORTRAIT_THRESHOLD - 0.01, "portrait"),
            (PORTRAIT_THRESHOLD, "square"),
            (1.0, "square"),
            (SQUARE_MAX, "square"),
            (SQUARE_MAX + 0.01, "landscape"),
            (PANORAMA_THRESHOLD - 0.01, "landscape"),
            (PANORAMA_THRESHOLD, "panorama"),
            (4.0, "panorama"),
        ],
    )
    def test_thresholds(self, ar, expected):
        assert classify_aspect(ar) == expected

    def test_non_positive_rejected(self):
        with pytest.raises(ValueError):
            classify_aspect(0)


# ---------------------------------------------------------------------------
# Virtual branch
# ---------------------------------------------------------------------------


class TestVirtualBranch:
    def test_frame_opening_is_screen_inset(self):
        """Virtual anchors the Frame Opening to the screen, hiding the bezel."""
        for style in STYLE_NAMES:
            result = calculate_framing(request(style))
            assert close(result.frame.opening.width, 514.0)
            assert close(result.frame.opening.height, 320.0)

    def test_frame_outer_is_opening_plus_moulding(self):
        result = calculate_framing(request("gallery", moulding_width=40.0))
        assert close(result.frame.outer.width, 514.0 + 80.0)
        assert close(result.frame.outer.height, 320.0 + 80.0)

    def test_frame_opening_never_exceeds_screen(self):
        for style in STYLE_NAMES:
            result = calculate_framing(request(style))
            assert SCREEN.rect.contains(result.frame.opening)

    @pytest.mark.parametrize(
        ("style", "expected_ring"),
        [
            ("modern", 19.2),
            ("classic", 35.2),
            ("gallery", 56.0),
            ("museum", 80.0),
            ("floating", 48.0),
        ],
    )
    def test_shortest_ring_equals_the_style_target(self, style, expected_ring):
        """Design C: the shortest ring is held at the style proportion."""
        result = calculate_framing(request(style))
        shortest = result.mat.ring.minimum
        assert close(shortest, expected_ring, 1e-6)

    def test_ring_absorbs_the_mismatch(self):
        """A wider artwork pushes the excess onto the top/bottom ring."""
        landscape = calculate_framing(request("gallery", 21 / 9))
        portrait = calculate_framing(request("gallery", 9 / 16))
        # 21:9 is width-limited -> vertical ring absorbs; sides stay at target.
        assert close(landscape.mat.ring.left, 56.0)
        assert landscape.mat.ring.top > 56.0
        # 9:16 is height-limited -> the sides absorb.
        assert close(portrait.mat.ring.top, 56.0)
        assert portrait.mat.ring.left > 56.0

    def test_no_ambient_fill_when_a_ring_is_applied(self):
        for style in [s for s in STYLE_NAMES if s != "borderless"]:
            for aspect in ASPECTS.values():
                result = calculate_framing(request(style, aspect))
                assert result.ambient_fill.present is False, (style, aspect)

    def test_ring_grows_with_the_style_but_opening_does_not(self):
        small = calculate_framing(request("modern"))
        large = calculate_framing(request("museum"))
        assert large.mat.ring.top > small.mat.ring.top
        assert close(large.frame.opening.width, small.frame.opening.width)
        # Visible area shrinks as the ring grows.
        assert large.metrics.screen_utilisation < small.metrics.screen_utilisation

    def test_artwork_inside_window(self):
        for aspect in ASPECTS.values():
            result = calculate_framing(request("gallery", aspect))
            assert result.mat.window.contains(result.artwork.presentation, 1e-6)

    def test_mat_outer_is_the_opening(self):
        result = calculate_framing(request("gallery"))
        assert result.mat.outer == result.frame.opening

    @pytest.mark.parametrize("style", VIRTUAL_ONLY_STYLES)
    def test_borderless_is_valid_virtually(self, style):
        for aspect in ASPECTS.values():
            result = calculate_framing(request(style, aspect))
            assert close(result.mat.ring.minimum, 0.0)
            assert check_invariants(result) == []


# ---------------------------------------------------------------------------
# Physical branch
# ---------------------------------------------------------------------------


class TestPhysicalBranch:
    def test_mat_window_defaults_to_screen_inset(self):
        result = calculate_framing(request(physical=True))
        assert close(result.mat.window.width, 514.0)
        assert close(result.mat.window.height, 320.0)

    def test_frame_opening_grows_around_a_fixed_window(self):
        """A deeper mat needs a bigger frame; the window never changes."""
        modern = calculate_framing(request("modern", physical=True))
        museum = calculate_framing(request("museum", physical=True))
        assert close(modern.mat.window.width, museum.mat.window.width)
        assert close(modern.mat.window.width, 514.0)
        assert museum.frame.opening.width > modern.frame.opening.width
        assert museum.frame.outer.width > modern.frame.outer.width

    def test_ring_is_uniform_for_a_centred_window(self):
        result = calculate_framing(
            templates.build_request(
                SCREEN, media(3 / 2), "gallery", physical=True, whitespace=False
            )
        )
        assert close(result.mat.ring.left, result.mat.ring.right)
        assert close(result.mat.ring.top, result.mat.ring.bottom)
        # Scaled so a uniform physical ring matches the virtual mat's depth.
        k = templates.physical_ring_factor(templates.STYLES["gallery"].ring)
        assert close(result.mat.ring.left, 56.0 * k, 1e-6)

    def test_opening_equals_window_plus_ring(self):
        result = calculate_framing(request("gallery", physical=True))
        assert close(
            result.frame.opening.width,
            result.mat.window.width + result.mat.ring.horizontal,
        )
        assert close(
            result.frame.opening.height,
            result.mat.window.height + result.mat.ring.vertical,
        )

    def test_frame_opening_may_exceed_the_screen(self):
        """Physically required: the mat board spans the panel."""
        result = calculate_framing(request("museum", physical=True))
        assert result.frame.opening.width > SCREEN.width_mm

    def test_ambient_fill_absorbs_the_mismatch(self):
        # Default window is 514x320 (1.606); 3:2 artwork (1.5) leaves side bars.
        result = calculate_framing(request("gallery", 3 / 2, physical=True))
        assert result.ambient_fill.present is True
        assert close(result.artwork.bounds.height, 320.0)
        assert close(result.artwork.bounds.width, 480.0)
        # 17 mm of residue on each side is real; 14 mm is claimed by whitespace.
        assert close(
            result.ambient_fill.bars[0].width
            + result.ambient_fill.bars[1].width
            + result.artwork.bounds.width,
            514.0,
        )

    def test_crop_leaves_no_ambient_fill(self):
        result = calculate_framing(request("gallery", 3 / 2, physical=True, overflow="crop"))
        assert result.artwork.fit == "cover"
        assert result.ambient_fill.present is False
        assert result.artwork.bounds == result.mat.window

    def test_fill_contains_and_crops_does_not(self):
        filled = calculate_framing(request("gallery", 9 / 16, physical=True))
        cropped = calculate_framing(request("gallery", 9 / 16, physical=True, overflow="crop"))
        assert filled.artwork.fit == "contain"
        assert filled.ambient_fill.present is True
        assert cropped.artwork.fit == "cover"
        assert cropped.ambient_fill.present is False

    def test_square_window_on_a_16_10_screen(self):
        """The motivating case: a square window wastes much of a 16:10 panel.

        The frame must be wide enough to reach the panel edge, so a small
        window needs a proportionally wider moulding.
        """
        window = Rect(x=99.0, y=2.0, width=320.0, height=320.0)
        result = calculate_framing(
            templates.build_request(
                SCREEN,
                media(3 / 2),
                "classic",
                physical=True,
                mat_window=window,
                moulding_width=90.0,
            )
        )
        assert close(result.mat.window.width, 320.0)
        assert close(result.mat.window.height, 320.0)
        assert close(result.metrics.screen_utilisation, 320 * 320 / (518 * 324), 1e-9)
        assert not check_invariants(result)

    def test_off_centre_window_is_expressed_by_the_rect(self):
        """A physical offset is cut and mounted, so the rect carries it."""
        window = Rect(x=60.0, y=40.0, width=400.0, height=240.0)
        # Per-side rings describe the resulting gaps exactly.
        result = calculate_framing(
            templates.build_request(
                SCREEN,
                media(3 / 2),
                "custom",
                physical=True,
                mat_window=window,
                ring=(10.0, 20.0, 30.0, 40.0),
                moulding_width=60.0,
            )
        )
        assert close(result.mat.window.x, 60.0)
        assert close(result.mat.window.y, 40.0)
        assert close(result.mat.ring.left, 10.0)
        assert close(result.mat.ring.right, 20.0)
        assert close(result.mat.ring.top, 30.0)
        assert close(result.mat.ring.bottom, 40.0)

    def test_mat_window_offset_rejected_for_physical(self):
        """The offset is achieved physically, not by the engine."""
        with pytest.raises(ValueError, match="does not apply to a physical mat"):
            FramingRequest(
                screen=SCREEN,
                media=media(3 / 2),
                mat_window=DEFAULT_WINDOW_PHYSICAL,
                mat_window_offset=(0.5, 0.75),
            )

    def test_mat_window_offset_applies_virtually(self):
        """The offset redistributes the slack that exists on an axis.

        21:9 is width-limited inside the style's inner rect, so it leaves
        vertical slack for the offset to move.
        """
        centred = calculate_framing(request("gallery", 21 / 9))
        low = calculate_framing(
            templates.build_request(SCREEN, media(21 / 9), "gallery", mat_window_offset=(0.5, 1.0))
        )
        assert low.mat.window.y > centred.mat.window.y
        assert close(low.mat.window.height, centred.mat.window.height)
        # The artwork moves with the window, so whitespace stays uniform.
        assert close(
            low.artwork.bounds.y - low.mat.window.y,
            centred.artwork.bounds.y - centred.mat.window.y,
        )

    def test_offset_has_no_effect_on_the_fitting_axis(self):
        """3:2 exactly fills the inner rect's height, so there is no slack."""
        centred = calculate_framing(request("gallery", 3 / 2))
        biased = calculate_framing(
            templates.build_request(SCREEN, media(3 / 2), "gallery", mat_window_offset=(0.5, 1.0))
        )
        assert close(biased.mat.window.y, centred.mat.window.y)


# ---------------------------------------------------------------------------
# Whitespace
# ---------------------------------------------------------------------------


class TestWhitespace:
    def test_disabled_leaves_artwork_in_the_window(self):
        result = calculate_framing(
            templates.build_request(SCREEN, media(3 / 2), "gallery", whitespace=False)
        )
        # 3:2 artwork in an 80 mm-ring window fills the height exactly.
        assert close(result.whitespace.authored_gap, 0.0)
        assert close(result.artwork.bounds.height, result.mat.window.height)

    def test_whitespace_forces_the_crop_presentation(self):
        """A white border and ambient fill must never appear together."""
        result = calculate_framing(
            templates.build_request(
                SCREEN,
                media(9 / 16),
                "gallery",
                whitespace=True,
                whitespace_gap=8.0,
                overflow="fill",
            )
        )
        assert result.overflow == "crop"
        assert result.ambient_fill.present is False
        assert result.whitespace.enabled is True

    def test_whitespace_with_fill_rejected_at_the_request_level(self):
        with pytest.raises(ValueError, match="whitespace requires overflow='crop'"):
            FramingRequest(
                screen=SCREEN,
                media=media(9 / 16),
                whitespace=WhitespaceSpec(enabled=True, gap=8.0),
                overflow="fill",
            )

    def test_runs_outside_the_artwork_not_over_it(self):
        """The band is measured from ws_outer to the artwork."""
        result = calculate_framing(
            templates.build_request(
                SCREEN,
                media(3 / 2),
                "classic",
                whitespace=True,
                whitespace_gap=8.0,
            )
        )
        gap = result.whitespace.applied_gap
        assert close(gap.left, 8.0)
        assert close(gap.top, 8.0)
        art = result.artwork.bounds
        outer = result.whitespace.outer
        assert close(outer.width, art.width + 16.0)
        assert close(outer.height, art.height + 16.0)

    def test_physical_mat_can_have_whitespace(self):
        """Whitespace is drawn by the screen, so a physical mat supports it."""
        result = calculate_framing(
            templates.build_request(
                SCREEN,
                media(3 / 2),
                "classic",
                physical=True,
                whitespace=True,
                whitespace_gap=8.0,
            )
        )
        assert result.whitespace.enabled is True
        assert close(result.whitespace.applied_gap.left, 8.0)
        assert close(result.whitespace.applied_gap.top, 8.0)

    def test_band_and_residual_partition_the_window(self):
        """window → artwork = whitespace band + ambient residue, per side."""
        for physical in (False, True):
            result = calculate_framing(
                templates.build_request(
                    SCREEN,
                    media(3 / 2),
                    "museum",
                    physical=physical,
                    whitespace=True,
                    whitespace_gap=8.0,
                )
            )
            window = result.mat.window
            art = result.artwork.bounds
            gap = result.whitespace.applied_gap
            residual = result.whitespace.residual
            for distance, band, residue in (
                (art.x - window.x, gap.left, residual.left),
                (window.right - art.right, gap.right, residual.right),
                (art.y - window.y, gap.top, residual.top),
                (window.bottom - art.bottom, gap.bottom, residual.bottom),
            ):
                assert close(distance, band + residue, 1e-6)
                assert band >= -1e-6 and residue >= -1e-6

    def test_band_is_uniform_around_the_artwork(self):
        result = calculate_framing(
            templates.build_request(
                SCREEN,
                media(3 / 2),
                "classic",
                whitespace=True,
                whitespace_gap=10.0,
            )
        )
        gap = result.whitespace.applied_gap
        assert close(gap.left, 10.0)
        assert close(gap.right, 10.0)
        assert close(gap.top, 10.0)
        assert close(gap.bottom, 10.0)
        assert close(result.whitespace.outer.width, result.artwork.bounds.width + 20.0)

    def test_whitespace_lies_within_the_window(self):
        for aspect in ASPECTS.values():
            result = calculate_framing(
                templates.build_request(
                    SCREEN,
                    media(aspect),
                    "classic",
                    whitespace=True,
                    whitespace_gap=8.0,
                )
            )
            assert result.mat.window.contains(result.whitespace.outer, 1e-6)

    def test_museum_suggests_whitespace_virtually(self):
        """Museum's toned band substitutes for the deferred double mat."""
        choice = templates.resolve_whitespace("museum")
        assert choice.enabled is True
        assert choice.colour != "#ffffff"
        assert choice.source == "style"

    def test_physical_mat_defaults_whitespace_off(self):
        choice = templates.resolve_whitespace("museum", has_physical_mat=True)
        assert choice.enabled is False
        assert choice.source == "casing"

    def test_print_presentation_defaults_whitespace_on_for_every_style(self):
        for style in STYLE_NAMES + VIRTUAL_ONLY_STYLES:
            choice = templates.resolve_whitespace(style, presentation="print")
            assert choice.enabled is True, style
            assert choice.colour == "#ffffff"

    def test_user_override_beats_the_casing_default(self):
        off = templates.resolve_whitespace("museum", override=False)
        on = templates.resolve_whitespace("gallery", override=True)
        assert off.enabled is False and off.source == "override"
        assert on.enabled is True and on.source == "override"

    def test_other_styles_keep_whitespace_off_by_default(self):
        for style in [s for s in STYLE_NAMES if s != "museum"]:
            choice = templates.resolve_whitespace(style)
            assert choice.enabled is False, style


# ---------------------------------------------------------------------------
# Style presets
# ---------------------------------------------------------------------------


class TestStyles:
    def test_all_documented_styles_exist(self):
        for name in [
            "borderless",
            "modern",
            "classic",
            "gallery",
            "museum",
            "floating",
            "polaroid",
            "custom",
        ]:
            assert name in templates.STYLES

    def test_guidelines_are_respected(self):
        """Each style sits inside its documented percentage range."""
        ranges = {
            "modern": (0.04, 0.08),
            "classic": (0.08, 0.14),
            "gallery": (0.14, 0.21),
            "museum": (0.20, 0.31),
            "floating": (0.11, 0.20),
        }
        for name, (lo, hi) in ranges.items():
            fraction = templates.STYLES[name].ring
            assert lo <= fraction <= hi, name

    def test_polaroid_is_bottom_heavy(self):
        result = calculate_framing(
            templates.build_request(SCREEN, media(3 / 2), "polaroid", whitespace=False)
        )
        assert result.mat.ring.bottom > result.mat.ring.top
        assert close(result.mat.ring.top, 44.8)
        assert close(result.mat.ring.bottom, 89.6)

    def test_style_ordering_is_monotonic(self):
        """Deeper styles use more mat and less screen."""
        order = ["modern", "classic", "gallery", "museum"]
        rings = [calculate_framing(request(s)).mat.ring.minimum for s in order]
        utils = [calculate_framing(request(s)).metrics.screen_utilisation for s in order]
        assert rings == sorted(rings)
        assert utils == sorted(utils, reverse=True)

    def test_borderless_overflows_without_a_ring(self):
        filled = calculate_framing(
            templates.build_request_for_preset(SCREEN, media(9 / 16), "immersive_fill")
        )
        cropped = calculate_framing(
            templates.build_request_for_preset(SCREEN, media(9 / 16), "immersive")
        )
        assert close(filled.mat.ring.minimum, 0.0)
        assert filled.artwork.fit == "contain"
        assert filled.ambient_fill.present is True
        assert cropped.artwork.fit == "cover"
        assert cropped.ambient_fill.present is False

    def test_unknown_style_rejected(self):
        with pytest.raises(ValueError, match="Unknown style"):
            templates.build_request(SCREEN, media(3 / 2), "not-a-style")

    def test_unknown_preset_rejected(self):
        with pytest.raises(ValueError, match="Unknown overflow preset"):
            templates.build_request_for_preset(SCREEN, media(3 / 2), "nope")

    def test_style_table_reports_mm(self):
        table = templates.style_table()
        assert close(table["gallery"]["ring_mm"], 56.0)
        assert close(table["polaroid"]["bottom_ring_mm"], 89.6)


# ---------------------------------------------------------------------------
# Focal points
# ---------------------------------------------------------------------------


class TestFocalPoints:
    """Focal placement needs slack between the artwork and the presentation.

    Under design C the Mat Window is cut to the artwork's aspect, so no slack
    exists when a ring is applied. Slack appears at ``ring = 0`` (borderless),
    where the fixed window is the Frame Opening.
    """

    def test_center_forces_the_centre(self):
        result = calculate_framing(
            templates.build_request(SCREEN, media(21 / 9), "borderless", focal_position="center")
        )
        assert result.artwork.focal_point is not None
        assert close(result.artwork.focal_point.x, 0.5)

    def test_manual_takes_priority(self):
        focused = calculate_framing(
            templates.build_request(
                SCREEN,
                media(21 / 9),
                "borderless",
                focal_position="manual",
                manual_focal_point=FocalPoint(0.5, 0.0),
            )
        )
        centred = calculate_framing(
            templates.build_request(SCREEN, media(21 / 9), "borderless", focal_position="center")
        )
        assert focused.artwork.bounds.y < centred.artwork.bounds.y

    def test_face_centroid_used_when_available(self):
        subject = media(21 / 9)
        subject.faces = [Face(x=0.4, y=0.3, width=0.2, height=0.4)]
        result = calculate_framing(templates.build_request(SCREEN, subject, "borderless"))
        assert result.artwork.focal_point is not None
        assert result.artwork.focal_point.x > 0.4

    def test_focal_shift_is_bounded(self):
        """A maximal focal point shifts the artwork by at most the bound.

        The bound is a fraction of the **slot** (the Mat Window, less any
        whitespace), matching the v1 aperture-relative behaviour.
        """
        result = calculate_framing(
            templates.build_request(
                SCREEN,
                media(21 / 9),
                "borderless",
                focal_position="manual",
                manual_focal_point=FocalPoint(1.0, 1.0),
            )
        )
        centred = calculate_framing(
            templates.build_request(SCREEN, media(21 / 9), "borderless", focal_position="center")
        )
        slot = result.mat.window  # whitespace disabled, so slot == window
        dy = abs(result.artwork.bounds.y - centred.artwork.bounds.y)
        dx = abs(result.artwork.bounds.x - centred.artwork.bounds.x)
        assert dy > 0.0
        assert dy <= MAX_FOCAL_SHIFT_Y * slot.height + 1e-6
        # 21:9 fills the slot width exactly, so there is no horizontal slack.
        assert close(dx, 0.0, 1e-6)

    def test_no_slack_means_focal_has_no_effect(self):
        """With a ring the window fits the artwork exactly, so nothing moves."""
        focused = calculate_framing(
            templates.build_request(
                SCREEN,
                media(3 / 2),
                "gallery",
                focal_position="manual",
                manual_focal_point=FocalPoint(1.0, 1.0),
            )
        )
        centred = calculate_framing(
            templates.build_request(SCREEN, media(3 / 2), "gallery", focal_position="center")
        )
        assert focused.artwork.bounds == centred.artwork.bounds


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


class TestConstraints:
    def test_window_outside_the_screen_rejected(self):
        with pytest.raises(ValueError, match="within the screen"):
            calculate_framing(
                templates.build_request(
                    SCREEN,
                    media(3 / 2),
                    "borderless",
                    physical=True,
                    mat_window=Rect(-5.0, 0.0, 400.0, 300.0),
                )
            )

    def test_oversized_ring_rejected(self):
        with pytest.raises(ValueError, match="consumes the frame opening"):
            calculate_framing(templates.build_request(SCREEN, media(3 / 2), "custom", ring=300.0))

    def test_negative_ring_rejected(self):
        with pytest.raises(ValueError, match="Mat Ring widths"):
            calculate_framing(FramingRequest(screen=SCREEN, media=media(3 / 2), ring=-1.0))

    def test_degenerate_mat_window_rejected(self):
        with pytest.raises(ValueError, match="positive size"):
            calculate_framing(
                templates.build_request(
                    SCREEN,
                    media(3 / 2),
                    "borderless",
                    physical=True,
                    mat_window=Rect(2.0, 2.0, 0.0, 300.0),
                )
            )

    @pytest.mark.parametrize("bad", [(-0.1, 0.5), (0.5, 1.1), (1.5, 0.5)])
    def test_invalid_offset_rejected(self, bad):
        with pytest.raises(ValueError, match="within"):
            FramingRequest(screen=SCREEN, media=media(3 / 2), mat_window_offset=bad)

    def test_invalid_media_rejected(self):
        with pytest.raises(ValueError):
            MediaDescriptor(width=0, height=10)

    def test_negative_whitespace_gap_rejected(self):
        with pytest.raises(ValueError):
            WhitespaceSpec(enabled=True, gap=-1.0)


# ---------------------------------------------------------------------------
# Panel coverage — the non-screen area must never be visible
# ---------------------------------------------------------------------------


class TestPanelCoverage:
    """The panel is 528 x 337 mm around a 518 x 324 mm active area.

    The frame must overlap the panel so the non-screen area is never visible.
    The overlap is the **rebate**, which is part of the moulding — so it can
    never be wider than the moulding itself.
    """

    def test_panel_is_larger_than_the_active_area(self):
        assert SCREEN.housing_width_mm == 528.0
        assert SCREEN.housing_height_mm == 337.0
        assert SCREEN.has_bezel is True
        assert close(SCREEN.bezel.left, 5.0)
        assert close(SCREEN.bezel.top, 6.5)
        assert SCREEN.panel_size == (528.0, 337.0)

    def test_minimum_rebate_is_the_non_screen_border(self):
        assert close(SCREEN.minimum_rebate.left, 5.0)
        assert close(SCREEN.minimum_rebate.top, 6.5)

    def test_required_rebate_when_the_opening_matches_the_active_area(self):
        """7 mm sides and 8.5 mm top/bottom for the default virtual opening."""
        needed = SCREEN.required_rebate(SCREEN.inset(2.0))
        assert close(needed.left, 7.0)
        assert close(needed.top, 8.5)

    def test_required_rebate_is_zero_once_the_opening_covers_the_panel(self):
        big = Rect(0.0, 0.0, 600.0, 420.0)
        needed = SCREEN.required_rebate(big)
        assert close(needed.left, 0.0)
        assert close(needed.top, 0.0)

    def test_screen_without_a_bezel_requires_no_rebate(self):
        plain = Screen(width_mm=518.0, height_mm=324.0)
        assert plain.has_bezel is False
        assert close(plain.bezel.left, 0.0)
        assert close(plain.required_rebate(plain.inset(2.0)).left, 2.0)

    def test_housing_smaller_than_the_active_area_rejected(self):
        with pytest.raises(ValueError, match="at least the active area"):
            Screen(width_mm=518.0, height_mm=324.0, housing_width_mm=500.0)

    def test_every_style_reports_a_rebate_that_fits_the_frame(self):
        """Physical and virtual alike: the rebate never exceeds the moulding."""
        for style in STYLE_NAMES + VIRTUAL_ONLY_STYLES:
            for physical in (False, True):
                result = calculate_framing(request(style, physical=physical))
                rebate = result.frame.required_rebate
                assert rebate.left <= result.frame.moulding.left + 1e-6, style
                assert rebate.top <= result.frame.moulding.top + 1e-6, style

    def test_borderless_is_valid_in_both_branches(self):
        """With no mat the rebate is larger, but a 40 mm moulding still holds it."""
        for physical in (False, True):
            result = calculate_framing(request("borderless", physical=physical))
            assert close(result.mat.ring.minimum, 0.0)
            assert close(result.frame.required_rebate.left, 7.0)
            assert check_invariants(result) == []

    def test_virtual_branch_reports_the_bezel_rebate(self):
        """The Frame Opening is inside the active area, so a rebate is needed."""
        result = calculate_framing(request("gallery"))
        assert close(result.frame.required_rebate.left, 7.0)
        assert close(result.frame.required_rebate.top, 8.5)

    def test_physical_branch_needs_no_rebate_once_the_ring_grows_the_opening(self):
        result = calculate_framing(request("gallery", physical=True))
        assert close(result.frame.required_rebate.left, 0.0)
        assert close(result.frame.required_rebate.top, 0.0)

    def test_moulding_too_narrow_for_the_rebate_is_rejected(self):
        with pytest.raises(ValueError, match=r"rebate needed.*wider than the frame"):
            calculate_framing(
                templates.build_request(SCREEN, media(3 / 2), "gallery", moulding_width=5.0)
            )

    def test_small_window_needing_an_impossible_rebate_is_rejected(self):
        """A 120 mm window cannot be grown by a 25 mm ring to cover the panel."""
        with pytest.raises(ValueError, match=r"rebate needed.*wider than the frame"):
            calculate_framing(
                templates.build_request(
                    SCREEN,
                    media(3 / 2),
                    "custom",
                    physical=True,
                    mat_window=Rect(200.0, 100.0, 120.0, 120.0),
                    ring=25.0,
                )
            )

    def test_a_bigger_ring_rescues_the_same_window(self):
        tiny = Rect(200.0, 100.0, 120.0, 120.0)
        result = calculate_framing(
            templates.build_request(
                SCREEN,
                media(3 / 2),
                "custom",
                physical=True,
                mat_window=tiny,
                ring=220.0,
            )
        )
        assert result.frame.opening.width >= 528.0
        assert result.frame.opening.height >= 337.0
        assert close(result.frame.required_rebate.left, 0.0)

    def test_a_wider_moulding_rescues_a_narrow_frame(self):
        """The rebate is feasible as long as the moulding is wide enough."""
        result = calculate_framing(
            templates.build_request(SCREEN, media(3 / 2), "gallery", moulding_width=12.0)
        )
        assert close(result.frame.required_rebate.left, 7.0)
        assert result.frame.moulding.left >= result.frame.required_rebate.left

    def test_screen_table_reports_the_panel_and_rebate(self):
        entry = templates.screen_table()["metixel_16_10_1920x1200"]
        assert entry["active_area"] == [518.0, 324.0]
        assert entry["panel"] == [528.0, 337.0]
        assert entry["non_screen"] == [10.0, 13.0]


class TestPortraitMounting:
    """The panel can be mounted portrait: the assembly rotated 90 degrees."""

    def test_preset_swaps_the_axes(self):
        portrait = templates.METIXEL_16_10_PORTRAIT
        assert close(portrait.width_mm, 324.0)
        assert close(portrait.height_mm, 518.0)
        assert close(portrait.housing_width_mm, 337.0)
        assert close(portrait.housing_height_mm, 528.0)
        assert portrait.width_px == 1200
        assert portrait.height_px == 1920

    def test_reference_dimension_is_unchanged(self):
        """The shorter side is 320 mm either way up, so rings match."""
        landscape = templates.METIXEL_16_10_1920x1200
        portrait = templates.METIXEL_16_10_PORTRAIT
        assert close(landscape.reference_dimension(2.0), 320.0)
        assert close(portrait.reference_dimension(2.0), 320.0)

    def test_ring_width_matches_the_landscape_mounting(self):
        landscape = calculate_framing(
            templates.build_request(templates.METIXEL_16_10_1920x1200, media(3 / 2), "gallery")
        )
        portrait = calculate_framing(
            templates.build_request(templates.METIXEL_16_10_PORTRAIT, media(3 / 2), "gallery")
        )
        assert close(portrait.mat.ring.minimum, landscape.mat.ring.minimum)

    def test_both_mountings_hold_the_panel(self):
        for screen in templates.METIXEL_SCREENS.values():
            panel = screen.panel_size
            assert panel == (528.0, 337.0) or panel == (337.0, 528.0)

    @pytest.mark.parametrize("orientation", ["landscape", "portrait"])
    @pytest.mark.parametrize("style", STYLE_NAMES + VIRTUAL_ONLY_STYLES)
    def test_invariants_hold_in_every_mounting(self, orientation, style):
        screen = templates.METIXEL_SCREENS[orientation]
        for aspect in ASPECTS.values():
            result = calculate_framing(templates.build_request(screen, media(aspect), style))
            assert check_invariants(result) == [], (orientation, style, aspect)

    @pytest.mark.parametrize("orientation", ["landscape", "portrait"])
    def test_physical_branch_holds_the_panel_in_every_mounting(self, orientation):
        screen = templates.METIXEL_SCREENS[orientation]
        window = screen.inset(2.0)
        for style in STYLE_NAMES:
            result = calculate_framing(
                templates.build_request(
                    screen,
                    media(3 / 2),
                    style,
                    physical=True,
                    mat_window=window,
                    whitespace=False,
                )
            )
            rebate = result.frame.required_rebate
            assert rebate.left <= result.frame.moulding.left + 1e-6
            assert rebate.top <= result.frame.moulding.top + 1e-6


class TestRingProportions:
    """A style must read the same whether the mat is virtual or physical.

    The branches differ in kind: the virtual mat insets the artwork, so its ring
    is a fraction of the *artwork*; the physical mat is the whole active area,
    so a ring of the same width is a much smaller fraction.  The template
    rescales the physical ring so the **ring-to-artwork ratio** matches, which is
    the proportion the eye reads.
    """

    def test_multiplier_is_one_over_one_minus_two_f(self):
        for f in (0.0, 0.06, 0.11, 0.175, 0.25):
            assert close(templates.physical_ring_factor(f), 1.0 / (1.0 - 2 * f), 1e-9)

    def test_borderless_needs_no_multiplier(self):
        assert close(templates.physical_ring_factor(0.0), 1.0)

    def test_fraction_at_or_above_a_half_is_rejected(self):
        for bad in (0.5, 0.6, 1.0):
            with pytest.raises(ValueError, match=r"must be in \[0, 0.5\)"):
                templates.physical_ring_factor(bad)

    @pytest.mark.parametrize("style", [s for s in STYLE_NAMES if s != "polaroid"])
    def test_ring_to_artwork_ratio_matches(self, style):
        """The key invariant: ring / artwork is the same in both branches.

        Compared on the artwork's short axis, against the reference-aspect
        artwork — the shape the virtual branch derives its ring from.
        """
        aspect = SCREEN.inset(2.0).aspect
        virtual = calculate_framing(
            templates.build_request(SCREEN, media(aspect), style, whitespace=False)
        )
        physical = calculate_framing(
            templates.build_request(SCREEN, media(aspect), style, physical=True, whitespace=False)
        )
        v_ring = virtual.mat.ring.minimum
        v_art = min(virtual.artwork.bounds.width, virtual.artwork.bounds.height)
        p_ring = physical.mat.ring.minimum
        p_art = min(physical.artwork.bounds.width, physical.artwork.bounds.height)
        assert close(p_ring / p_art, v_ring / v_art, 1e-6), style

    @pytest.mark.parametrize("style", [s for s in STYLE_NAMES if s != "polaroid"])
    def test_ring_fraction_of_the_opening_matches(self, style):
        """The other view: the ring's share of the whole opening also matches."""
        aspect = SCREEN.inset(2.0).aspect
        virtual = calculate_framing(
            templates.build_request(SCREEN, media(aspect), style, whitespace=False)
        )
        physical = calculate_framing(
            templates.build_request(SCREEN, media(aspect), style, physical=True, whitespace=False)
        )
        assert close(
            physical.mat.ring.minimum
            / min(physical.frame.opening.width, physical.frame.opening.height),
            virtual.mat.ring.minimum
            / min(virtual.frame.opening.width, virtual.frame.opening.height),
            1e-6,
        ), style

    def test_explicit_ring_is_taken_at_face_value(self):
        """A caller-supplied millimetre ring is not rescaled."""
        explicit = calculate_framing(
            templates.build_request(
                SCREEN,
                media(3 / 2),
                "gallery",
                physical=True,
                ring=60.0,
                whitespace=False,
            )
        )
        assert close(explicit.mat.ring.left, 60.0)

    def test_virtual_ring_is_unscaled(self):
        """The virtual branch keeps the style proportion on the shortest ring."""
        for style in STYLE_NAMES:
            virtual = calculate_framing(templates.build_request(SCREEN, media(3 / 2), style))
            expected = templates.STYLES[style].ring * 320.0
            assert close(virtual.mat.ring.minimum, expected, 1e-6), style

    def test_polaroid_keeps_its_asymmetry_when_scaled(self):
        """One multiplier on all four sides preserves the bottom-heavy ratio."""
        physical = calculate_framing(
            templates.build_request(
                SCREEN, media(3 / 2), "polaroid", physical=True, whitespace=False
            )
        )
        spec = templates.STYLES["polaroid"]
        assert spec.bottom_ring is not None
        ratio = spec.bottom_ring / spec.ring
        assert close(physical.mat.ring.bottom / physical.mat.ring.top, ratio, 1e-6)
        k = templates.physical_ring_factor(spec.ring)
        assert close(physical.mat.ring.left, spec.ring * 320.0 * k, 1e-6)

    @pytest.mark.parametrize("style", STYLE_NAMES)
    def test_physical_ring_is_deeper_than_the_unscaled_style(self, style):
        """Without scaling the uniform ring reads thinner than the virtual mat."""
        unscaled = templates.STYLES[style].ring * 320.0
        physical = calculate_framing(
            templates.build_request(SCREEN, media(3 / 2), style, physical=True)
        )
        assert physical.mat.ring.minimum > unscaled


# ---------------------------------------------------------------------------
# Outputs and views
# ---------------------------------------------------------------------------


class TestOutputs:
    def test_every_group_is_reported_in_both_branches(self):
        for physical in (False, True):
            result = calculate_framing(request(physical=physical))
            assert result.frame.outer.is_positive()
            assert result.mat.window.is_positive()
            assert result.whitespace.outer.is_positive()
            assert result.ambient_fill.region.is_positive()
            assert result.artwork.bounds.is_positive()
            assert 0.0 < result.metrics.screen_utilisation <= 1.0

    def test_to_spec_millimetres(self):
        result = calculate_framing(
            templates.build_request(
                SCREEN, media(3 / 2), "gallery", physical=True, whitespace=False
            )
        )
        spec = result.to_spec("mm")
        assert spec["unit"] == "mm"
        assert close(spec["mat_window"][0], 514.0)
        k = templates.physical_ring_factor(templates.STYLES["gallery"].ring)
        assert close(spec["mat_ring"]["left"], 56.0 * k, 1e-2)

    def test_to_spec_units_convert(self):
        result = calculate_framing(
            templates.build_request(
                SCREEN, media(3 / 2), "gallery", physical=True, whitespace=False
            )
        )
        mm = result.to_spec("mm")
        cm = result.to_spec("cm")
        inch = result.to_spec("in")
        k = templates.physical_ring_factor(templates.STYLES["gallery"].ring)
        assert close(cm["mat_ring"]["left"], 5.6 * k, 1e-2)
        assert close(inch["mat_ring"]["left"], 56.0 * k / 25.4, 1e-2)
        assert close(mm["mat_ring"]["left"], 56.0 * k, 1e-2)

    def test_to_px_matches_px_per_mm(self):
        result = calculate_framing(
            templates.build_request(SCREEN, media(3 / 2), "gallery", whitespace=False)
        )
        px = result.to_px()
        sx, _ = SCREEN.px_per_mm
        assert sx is not None
        assert close(px["mat_window"]["width"], result.mat.window.width * sx, 1e-6)

    def test_to_px_requires_pixel_dimensions(self):
        screen = Screen(width_mm=518.0, height_mm=324.0)
        result = calculate_framing(templates.build_request(screen, media(3 / 2), "gallery"))
        with pytest.raises(ValueError, match="pixel"):
            result.to_px()

    def test_frame_relative_normalises_outer_to_unit_square(self):
        result = calculate_framing(request("gallery", physical=True))
        outer = result.frame_relative()["frame_outer"]
        assert close(outer["x"], 0.0)
        assert close(outer["width"], 1.0)

    def test_display_relative_keeps_geometry_proportional(self):
        result = calculate_framing(request("gallery", whitespace=False))
        view = result.display_relative()
        # Frame Opening is the screen inset by 2 mm on each axis.
        assert close(view["frame_opening"]["x"], 2.0 / 518.0)
        assert close(view["mat_window"]["width"], result.mat.window.width / 518.0)

    def test_uniform_mm_band_is_not_uniform_normalised(self):
        """The reason all arithmetic happens in mm."""
        result = calculate_framing(request("gallery", physical=True))
        view = result.display_relative()
        ring_x = view["frame_opening"]["x"]
        ring_y = view["frame_opening"]["y"]
        assert not close(ring_x, ring_y, 1e-3)


# ---------------------------------------------------------------------------
# Cross-product invariants
# ---------------------------------------------------------------------------


class TestInvariantMatrix:
    @pytest.mark.parametrize("style", STYLE_NAMES)
    @pytest.mark.parametrize("physical", [False, True])
    @pytest.mark.parametrize("aspect_name", list(ASPECTS))
    @pytest.mark.parametrize("whitespace", [False, True])
    @pytest.mark.parametrize("overflow", ["crop", "fill"])
    def test_no_invariant_violations(self, style, physical, aspect_name, whitespace, overflow):
        result = calculate_framing(
            request(
                style,
                ASPECTS[aspect_name],
                physical=physical,
                whitespace=whitespace,
                whitespace_gap=6.0,
                overflow=overflow,
            )
        )
        assert check_invariants(result) == []

    @pytest.mark.parametrize("style", STYLE_NAMES)
    @pytest.mark.parametrize("physical", [False, True])
    def test_nesting_always_holds(self, style, physical):
        for aspect in ASPECTS.values():
            result = calculate_framing(request(style, aspect, physical=physical))
            assert result.frame.outer.contains(result.frame.opening, 1e-6)
            assert result.frame.opening.contains(result.mat.window, 1e-6)
            assert result.mat.window.contains(result.artwork.presentation, 1e-6)
            assert result.artwork.presentation.contains(result.artwork.bounds, 1e-6)

    @pytest.mark.parametrize("style", STYLE_NAMES)
    @pytest.mark.parametrize("physical", [False, True])
    def test_determinism(self, style, physical):
        req = request(style, 3 / 2, physical=physical)
        assert calculate_framing(req) == calculate_framing(req)

    @pytest.mark.parametrize("aspect_name", list(ASPECTS))
    def test_video_geometry_is_stable(self, aspect_name):
        aspect = ASPECTS[aspect_name]
        video = MediaDescriptor(width=aspect, height=1.0, type="video")
        result = calculate_framing(templates.build_request(SCREEN, video, "gallery"))
        assert check_invariants(result) == []
        assert result.artwork.orientation == classify_aspect(aspect)


# ---------------------------------------------------------------------------
# Aspect preservation (the v2 simplification)
# ---------------------------------------------------------------------------


class TestAspectPreservation:
    @pytest.mark.parametrize("aspect_name", list(ASPECTS))
    def test_contained_artwork_keeps_its_aspect(self, aspect_name):
        """In millimetres the aspect compares directly — no compensation."""
        aspect = ASPECTS[aspect_name]
        result = calculate_framing(request("gallery", aspect))
        assert close(result.artwork.bounds.aspect, aspect, 1e-9)

    @pytest.mark.parametrize("aspect_name", list(ASPECTS))
    def test_artwork_fills_one_axis_of_its_slot(self, aspect_name):
        """With the fill presentation the artwork fits its slot exactly."""
        result = calculate_framing(
            templates.build_request(
                SCREEN,
                media(ASPECTS[aspect_name]),
                "classic",
                whitespace=True,
                whitespace_gap=8.0,
                overflow="crop",
            )
        )
        slot = result.mat.window.inset(8.0, 8.0, 8.0, 8.0)
        rect = result.artwork.bounds
        assert close(rect.width, slot.width, 1e-6) or close(rect.height, slot.height, 1e-6)
        # Whitespace forces crop, so no ambient fill is present.
        assert result.overflow == "crop"
        assert result.ambient_fill.present is False

    def test_square_artwork_stays_square(self):
        result = calculate_framing(request("gallery", 1.0))
        assert close(result.artwork.bounds.aspect, 1.0, 1e-9)


# ---------------------------------------------------------------------------
# Rect helper
# ---------------------------------------------------------------------------


class TestRect:
    def test_inset_and_expand_are_inverse(self):
        rect = Rect(10.0, 20.0, 100.0, 50.0)
        assert rect.inset(5, 5, 5, 5).expand(5, 5, 5, 5) == rect

    def test_contains_respects_tolerance(self):
        outer = Rect(0.0, 0.0, 10.0, 10.0)
        assert outer.contains(Rect(1.0, 1.0, 8.0, 8.0))
        assert not outer.contains(Rect(-1.0, 0.0, 5.0, 5.0))

    def test_centre_and_area(self):
        rect = Rect(0.0, 0.0, 10.0, 4.0)
        assert rect.centre == (5.0, 2.0)
        assert close(rect.area, 40.0)
        assert close(rect.aspect, 2.5)

    def test_normalised_against_a_reference(self):
        view = Rect(5.0, 5.0, 50.0, 20.0).normalised(Rect(0.0, 0.0, 100.0, 40.0))
        assert close(view["x"], 0.05)
        assert close(view["width"], 0.5)
        assert close(view["height"], 0.5)

    def test_negative_size_is_not_positive(self):
        assert not Rect(0.0, 0.0, -1.0, 5.0).is_positive()


class TestInsetsHelper:
    def test_uniform(self):
        i = Insets.uniform(4.0)
        assert i.minimum == 4.0
        assert close(i.horizontal, 8.0)

    def test_scalar_ring_is_uniform(self):
        """A scalar ring is uniform, but design C holds the shortest side at it.

        On the fitting axis the ring is exactly 10 mm; the opposite axis takes
        the excess needed to match the artwork's aspect.
        """
        result = calculate_framing(FramingRequest(screen=SCREEN, media=media(3 / 2), ring=10.0))
        assert close(result.mat.ring.minimum, 10.0)
        assert close(result.mat.ring.top, 10.0)
        assert result.mat.ring.left >= 10.0

    def test_per_side_ring_tuple(self):
        result = calculate_framing(
            FramingRequest(
                screen=SCREEN,
                media=media(3 / 2),
                mat_window=Rect(2.0, 2.0, 514.0, 320.0),
                ring=(10.0, 20.0, 30.0, 40.0),
            )
        )
        assert close(result.mat.ring.left, 10.0)
        assert close(result.mat.ring.right, 20.0)
        assert close(result.mat.ring.top, 30.0)
        assert close(result.mat.ring.bottom, 40.0)
