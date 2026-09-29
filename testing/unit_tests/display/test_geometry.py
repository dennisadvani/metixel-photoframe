# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for the shared pixel-rect conversion.

Small, but load-bearing: the mpv widget is positioned from :func:`int_rect` and
the canvas clips its artwork hole to the same value.  A disagreement of one pixel
between two SIBLING widgets is a black seam down the edge of the video — and the
symptom points at the paint code rather than at the arithmetic, which is what
makes it worth pinning here.
"""

from __future__ import annotations

import pytest

from metixel.display.geometry import int_rect


class TestIntRect:
    """The conversion is exact, and its asymmetry is the point."""

    def test_whole_pixels_are_unchanged(self) -> None:
        assert int_rect((10.0, 20.0, 30.0, 40.0)) == (10, 20, 30, 40)

    def test_the_origin_floors(self) -> None:
        # Flooring keeps the rect from creeping up and to the left, where it
        # would cover a sliver of the outgoing frame.
        assert int_rect((10.7, 20.9, 30.0, 40.0))[:2] == (10, 20)

    def test_the_far_edge_rounds_outward(self) -> None:
        """A fraction past an integer still consumes the whole pixel.

        Truncating here is what left a one-pixel column painted by neither the
        artwork nor the curtain that wipes its complement.
        """
        assert int_rect((7.4, 7.4, 1905.17, 1185.19)) == (7, 7, 1906, 1186)

    def test_an_exact_integer_far_edge_is_not_pushed_out(self) -> None:
        # 0.9999 is deliberately just under 1, so an exact edge stays put.
        assert int_rect((0.0, 0.0, 100.0, 50.0)) == (0, 0, 100, 50)

    def test_a_zero_extent_stays_empty(self) -> None:
        # A negative extent is NOT the empty rect, it is a different rectangle.
        assert int_rect((10.0, 20.0, 0.0, 0.0)) == (10, 20, 0, 0)

    def test_it_is_idempotent(self) -> None:
        once = int_rect((7.4, 7.4, 1905.17, 1185.19))
        as_floats = tuple(float(v) for v in once)
        assert int_rect(as_floats) == once  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "rect",
        [
            (0.0, 0.0, 1920.0, 1200.0),
            (0.5, 0.5, 0.0, 0.0),
            (1.999, 2.999, 3.0, 4.0),
            (1919.999, 1199.999, 0.001, 0.001),
        ],
    )
    def test_every_component_is_an_int(self, rect: tuple[float, float, float, float]) -> None:
        assert all(isinstance(value, int) for value in int_rect(rect))
