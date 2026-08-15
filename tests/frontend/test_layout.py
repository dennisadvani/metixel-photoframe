# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the presentation engine and layout."""

import pytest


def test_layout_engine_contain():
    """Verify contain layout for a wider-than-screen image."""
    from metixel.frontend.presentation.layout import LayoutEngine

    engine = LayoutEngine(screen_w=1920, screen_h=1080)
    # Image wider than 16:9 (e.g., 21:9 ultrawide)
    layout = engine._compute_contain(21.0 / 9.0)

    assert layout["image_rect"][0] == 0  # x
    assert layout["image_rect"][2] == 1920  # w
    assert len(layout["matte_rects"]) == 2  # Top and bottom bars


def test_layout_engine_contain_tall():
    """Verify contain layout for a taller-than-screen image."""
    from metixel.frontend.presentation.layout import LayoutEngine

    engine = LayoutEngine(screen_w=1920, screen_h=1080)
    # Image taller than 16:9 (e.g., 9:16 portrait phone photo)
    layout = engine._compute_contain(9.0 / 16.0)

    assert layout["image_rect"][1] == 0  # y
    assert layout["image_rect"][3] == 1080  # h
    assert len(layout["matte_rects"]) == 2  # Left and right bars


def test_layout_engine_near_match_no_matte():
    """Verify near-match aspect ratios don't get matte bars."""
    from metixel.frontend.presentation.layout import LayoutEngine

    engine = LayoutEngine(screen_w=1920, screen_h=1080)
    # 16:9 image (exact match)
    layout = engine._compute_contain(16.0 / 9.0)

    assert layout["image_rect"] == (0, 0, 1920, 1080)
    assert len(layout["matte_rects"]) == 0


def test_layout_centering_contain_wider():
    """Image in contain mode should be horizontally and vertically centred."""
    from metixel.frontend.presentation.layout import LayoutEngine

    engine = LayoutEngine(screen_w=1920, screen_h=1080)
    layout = engine._compute_contain(2.0)  # Wider image (e.g., 3000×1500)

    ix, iy, iw, ih = layout["image_rect"]
    # Image should span full width
    assert ix == 0
    assert iw == 1920
    # Image vertical centre should equal screen vertical centre
    assert iy + ih / 2 == pytest.approx(540.0)


def test_layout_centering_contain_taller():
    """Taller image in contain mode should be centred."""
    from metixel.frontend.presentation.layout import LayoutEngine

    engine = LayoutEngine(screen_w=1920, screen_h=1080)
    layout = engine._compute_contain(0.5)  # Taller image (e.g., 1500×3000)

    ix, iy, iw, ih = layout["image_rect"]
    # Image should span full height
    assert iy == 0
    assert ih == 1080
    # Image horizontal centre should equal screen horizontal centre
    assert ix + iw / 2 == pytest.approx(960.0)


def test_layout_centering_cover_wider():
    """Wider image in cover mode should be vertically centred."""
    from metixel.frontend.presentation.layout import LayoutEngine

    engine = LayoutEngine(screen_w=1920, screen_h=1080)
    layout = engine._compute_cover(2.0)  # Wider image

    ix, iy, iw, ih = layout["image_rect"]
    # Image should span full height
    assert iy == 0
    assert ih == 1080
    # Image horizontal centre should equal screen horizontal centre
    assert ix + iw / 2 == pytest.approx(960.0)


def test_layout_centering_cover_taller():
    """Taller image in cover mode should be horizontally centred."""
    from metixel.frontend.presentation.layout import LayoutEngine

    engine = LayoutEngine(screen_w=1920, screen_h=1080)
    layout = engine._compute_cover(0.5)  # Taller image

    ix, iy, iw, ih = layout["image_rect"]
    # Image should span full width
    assert ix == 0
    assert iw == 1920
    # Image vertical centre should equal screen vertical centre
    assert iy + ih / 2 == pytest.approx(540.0)


def test_layout_centering_fill():
    """Fill mode should always fill the screen exactly."""
    from pathlib import Path

    from metixel.frontend.presentation.layout import LayoutEngine
    from metixel.shared.models import MediaItem, MediaType

    engine = LayoutEngine(screen_w=1920, screen_h=1080)
    item = MediaItem(
        id="test",
        original_path=Path("test.jpg"),
        cached_path=Path("test.jpg"),
        media_type=MediaType.IMAGE,
        width=3000,
        height=1500,
    )
    layout = engine.compute(item, fit_mode="fill")

    assert layout["image_rect"] == (0, 0, 1920, 1080)
    assert layout["matte_rects"] == []


def test_layout_fit_mode_passthrough():
    """Verify that fit_mode parameter is respected via compute()."""
    from pathlib import Path

    from metixel.frontend.presentation.layout import LayoutEngine
    from metixel.shared.models import MediaItem, MediaType

    engine = LayoutEngine(screen_w=1920, screen_h=1080)
    item = MediaItem(
        id="test",
        original_path=Path("test.jpg"),
        cached_path=Path("test.jpg"),
        media_type=MediaType.IMAGE,
        width=3000,
        height=2000,  # 3:2 image
    )

    # contain: should have matte bars (image is taller than 16:9)
    layout_contain = engine.compute(item, fit_mode="contain")
    assert len(layout_contain["matte_rects"]) == 2  # pillarbox

    # cover: should have no matte bars, but image rect != screen rect
    layout_cover = engine.compute(item, fit_mode="cover")
    assert layout_cover["matte_rects"] == []
    assert layout_cover["image_rect"] != (0, 0, 1920, 1080)  # Not full screen

    # fill: should fill screen exactly
    layout_fill = engine.compute(item, fit_mode="fill")
    assert layout_fill["image_rect"] == (0, 0, 1920, 1080)
    assert layout_fill["matte_rects"] == []
