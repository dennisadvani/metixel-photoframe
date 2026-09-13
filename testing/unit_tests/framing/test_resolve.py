# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for :mod:`metixel.framing.resolve` — panel preset selection.

The point of ``resolve`` is that no caller hand-rolls panel geometry, so these
tests pin the two things that would break that guarantee:

* the millimetre constants come from ``framing_templates`` and nowhere else, and
* a panel that is *not* the Metixel panel still resolves (i.e. nothing is
  hardcoded to 1920x1200 / 518x324).
"""

from __future__ import annotations

import pytest

from metixel.framing import framing_templates as templates
from metixel.framing.framing_engine import Screen, calculate_framing
from metixel.framing.resolve import (
    ORIENTATIONS,
    MediaSize,
    _orientation_for_rotation,
    resolve,
    scale_screen,
    screen_for,
)

# ---------------------------------------------------------------------------
# Orientation mapping
# ---------------------------------------------------------------------------


class TestOrientation:
    @pytest.mark.parametrize(
        ("rotation", "expected"),
        [
            (0, "landscape"),
            (90, "portrait"),
            (180, "landscape"),
            (270, "portrait"),
            (360, "landscape"),
            (-90, "portrait"),
        ],
    )
    def test_rotation_maps_to_mounting(self, rotation: int, expected: str) -> None:
        assert _orientation_for_rotation(rotation) == expected

    @pytest.mark.parametrize("bad", [None, "", "ninety", 3.7])
    def test_unusable_rotation_falls_back_to_landscape(self, bad: object) -> None:
        """A missing/garbage rotation must never raise — it defaults to landscape."""
        assert _orientation_for_rotation(bad) == "landscape"  # type: ignore[arg-type]

    def test_every_orientation_has_a_preset(self) -> None:
        assert set(ORIENTATIONS) == set(templates.METIXEL_SCREENS)


# ---------------------------------------------------------------------------
# Screen selection
# ---------------------------------------------------------------------------


class TestScreenFor:
    def test_landscape_uses_the_landscape_preset(self) -> None:
        scr = screen_for(0, width_px=1920, height_px=1200)
        assert (scr.width_mm, scr.height_mm) == (518.0, 324.0)
        assert (scr.width_px, scr.height_px) == (1920.0, 1200.0)

    def test_portrait_swaps_active_area_and_pixels(self) -> None:
        scr = screen_for(90, width_px=1200, height_px=1920)
        assert (scr.width_mm, scr.height_mm) == (324.0, 518.0)
        assert (scr.width_px, scr.height_px) == (1200.0, 1920.0)

    def test_housing_is_carried_through(self) -> None:
        """The non-screen border drives ``required_rebate`` — it must survive."""
        scr = screen_for(0, width_px=1920, height_px=1200)
        assert (scr.housing_width_mm, scr.housing_height_mm) == (528.0, 337.0)
        assert scr.has_bezel

    def test_panel_size_is_identical_in_both_mountings(self) -> None:
        """It is the same physical panel rotated, so its area cannot change."""
        land = screen_for(0, width_px=1920, height_px=1200)
        port = screen_for(90, width_px=1200, height_px=1920)
        assert land.area == pytest.approx(port.area)

    def test_unknown_resolution_keeps_the_preset_pixels(self) -> None:
        """0x0 means "not detected yet" — do not invent a resolution."""
        scr = screen_for(0, width_px=0, height_px=0)
        assert (scr.width_px, scr.height_px) == (1920.0, 1200.0)


class TestScaleScreen:
    def test_millimetres_are_preserved_and_only_pixels_change(self) -> None:
        base = templates.METIXEL_16_10_1920x1200
        scaled = scale_screen(base, 2560, 1600)
        assert (scaled.width_mm, scaled.height_mm) == (base.width_mm, base.height_mm)
        assert (scaled.width_px, scaled.height_px) == (2560.0, 1600.0)
        assert (scaled.housing_width_mm, scaled.housing_height_mm) == (
            base.housing_width_mm,
            base.housing_height_mm,
        )

    def test_non_positive_pixels_are_ignored(self) -> None:
        base = templates.METIXEL_16_10_1920x1200
        assert scale_screen(base, 0, 1200) is base
        assert scale_screen(base, 1920, -1) is base


class TestSyntheticScreen:
    """A non-Metixel panel must resolve — proving nothing is hardcoded.

    This is the regression guard for "route the panel constants through
    templates": if someone reintroduces 518/324/1920/1200 into the render path,
    a synthetic panel stops working and this fails.
    """

    def test_synthetic_panel_resolves_and_frames(self) -> None:
        synthetic = Screen(
            width_mm=600.0,
            height_mm=340.0,
            width_px=2560.0,
            height_px=1440.0,
            housing_width_mm=610.0,
            housing_height_mm=350.0,
        )
        request = resolve(synthetic, MediaSize(3000, 2000))
        result = calculate_framing(request)
        assert result.branch == "virtual"
        assert result.screen.width_mm == 600.0

        # 16:10 artwork on a 600x340 (1.76) panel: still a valid mat.
        assert result.mat.window.width > 0
        assert result.mat.window.height > 0

    def test_reference_dimension_is_the_shorter_side(self) -> None:
        """A style keeps its ring whichever way the panel is mounted."""
        land = templates.METIXEL_16_10_1920x1200
        port = templates.METIXEL_16_10_PORTRAIT
        margin = 2.0
        assert land.reference_dimension(margin) == port.reference_dimension(margin) == 320.0


# ---------------------------------------------------------------------------
# MediaSize
# ---------------------------------------------------------------------------


class TestMediaSize:
    def test_zero_dimensions_are_invalid(self) -> None:
        assert not MediaSize(0, 0).is_valid
        assert not MediaSize(1920, 0).is_valid
        assert not MediaSize(0, 1080).is_valid

    def test_positive_dimensions_are_valid(self) -> None:
        assert MediaSize(1920, 1080).is_valid

    def test_media_type_defaults_to_image(self) -> None:
        assert MediaSize(100, 100).media_type == "image"
        assert MediaSize(100, 100, "video").media_type == "video"


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


SCREEN = templates.METIXEL_16_10_1920x1200


class TestResolve:
    def test_returns_a_virtual_branch_request(self) -> None:
        """The digital frame draws a software mat, so no Mat Window is given."""
        request = resolve(SCREEN, MediaSize(3000, 2000))
        assert request.mat_window is None
        assert not request.is_physical

    def test_style_fraction_becomes_a_millimetre_ring(self) -> None:
        """gallery is 0.175 of the 320 mm reference -> a 56 mm shortest ring."""
        request = resolve(SCREEN, MediaSize(3000, 2000), style="gallery")
        result = calculate_framing(request)
        assert result.mat.ring.minimum == pytest.approx(56.0, abs=0.05)

    def test_defaults_come_from_the_templates_not_this_module(self) -> None:
        """Omitted overrides must not be re-specified by resolve()."""
        request = resolve(SCREEN, MediaSize(3000, 2000))
        assert (
            request.edge_margin
            == templates.build_request(
                SCREEN, templates.MediaDescriptor(3000, 2000), "gallery"
            ).edge_margin
        )
        assert request.moulding_width == 40.0

    def test_overrides_are_forwarded(self) -> None:
        request = resolve(
            SCREEN,
            MediaSize(3000, 2000),
            overflow="crop",
            whitespace=True,
            ambient_strategy="bars",
            edge_margin=8.0,
            moulding_width=60.0,
        )
        assert request.overflow == "crop"
        assert request.whitespace.enabled is True
        assert request.ambient.strategy == "bars"
        assert request.edge_margin == 8.0
        assert request.moulding_width == 60.0

    def test_omitting_an_override_forwards_the_template_default(self) -> None:
        """Omitted values must resolve to the template's default, not a copy.

        ``resolve`` reads the defaults from ``build_request`` itself rather
        than restating the numbers, so this pins that the introspection still
        lands on the same value the templates would have used.
        """
        via_resolve = resolve(SCREEN, MediaSize(3000, 2000))
        direct = templates.build_request(SCREEN, templates.MediaDescriptor(3000, 2000), "gallery")
        assert via_resolve.edge_margin == direct.edge_margin
        assert via_resolve.moulding_width == direct.moulding_width

    def test_a_typo_in_overflow_is_rejected_early(self) -> None:
        """A bad config value must fail at the boundary, not per frame on a Pi."""
        with pytest.raises(ValueError, match="overflow"):
            resolve(SCREEN, MediaSize(3000, 2000), overflow="croped")

    def test_a_typo_in_ambient_strategy_is_rejected_early(self) -> None:
        with pytest.raises(ValueError, match="ambient_strategy"):
            resolve(SCREEN, MediaSize(3000, 2000), ambient_strategy="blured")

    def test_whitespace_none_defers_to_the_style(self) -> None:
        """museum suggests a whitespace band; gallery does not."""
        museum = calculate_framing(resolve(SCREEN, MediaSize(3000, 2000), style="museum"))
        gallery = calculate_framing(resolve(SCREEN, MediaSize(3000, 2000), style="gallery"))
        assert museum.whitespace.enabled
        assert not gallery.whitespace.enabled

    @pytest.mark.parametrize("style", sorted(templates.STYLES))
    def test_every_style_resolves_for_every_aspect(self, style: str) -> None:
        for w, h in ((3000, 2000), (4000, 2250), (2000, 3000), (2000, 2000), (5000, 2000)):
            result = calculate_framing(resolve(SCREEN, MediaSize(w, h), style=style))
            assert result.mat.window.width > 0
            assert result.mat.window.height > 0

    def test_portrait_preset_frames_landscape_artwork(self) -> None:
        portrait = screen_for(90, width_px=1200, height_px=1920)
        result = calculate_framing(resolve(portrait, MediaSize(4000, 2250)))
        assert result.screen.height_mm > result.screen.width_mm
        assert result.mat.window.height > 0
