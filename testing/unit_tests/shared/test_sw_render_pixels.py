# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the per-board software-render buffer cap.

The cap exists because using libmpv's software render API is what removes the
``sync_file`` descriptor leak from the GL render path, and that correctness costs
CPU spent in mpv's scale-and-convert step.  Unlike the decoder choice in
``test_hwdec.py``, guessing *high* here does not break video — it makes it
stutter, which reads as a hardware fault rather than as a configuration mistake.
So the conservative direction is asserted explicitly.
"""

from __future__ import annotations

import pytest

from metixel.shared.platform import (
    DEFAULT_SW_RENDER_PIXELS,
    SWRenderPixelsByModel,
    sw_render_max_pixels_for_model,
)


class TestSwRenderPixelCap:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("pi5", 1_000_000),
            ("pi4", 1_000_000),
            ("pi3", 350_000),
            ("pi2", 250_000),
        ],
    )
    def test_known_models_map_to_their_cap(self, model: str, expected: int) -> None:
        assert sw_render_max_pixels_for_model(model) == expected

    @pytest.mark.parametrize("model", [None, "", "unknown", "radxa-zero-3w"])
    def test_unknown_model_gets_the_conservative_cap(self, model: str | None) -> None:
        assert sw_render_max_pixels_for_model(model) == DEFAULT_SW_RENDER_PIXELS

    def test_an_unidentified_board_is_not_given_the_pi5_budget(self):
        """The asymmetry is deliberate.

        Too small a buffer is a softer picture; too large a one is a stutter that
        looks like failing hardware and is far harder to diagnose remotely.
        """
        assert sw_render_max_pixels_for_model(None) < SWRenderPixelsByModel["pi5"]

    def test_weaker_boards_get_a_smaller_cap(self):
        """The cost is CPU-bound, and the boards differ by ~3-4x per core."""
        assert (
            SWRenderPixelsByModel["pi5"]
            >= SWRenderPixelsByModel["pi4"]
            > SWRenderPixelsByModel["pi3"]
            > SWRenderPixelsByModel["pi2"]
        )

    @pytest.mark.parametrize("model", sorted(SWRenderPixelsByModel))
    def test_the_cap_forces_a_reduction_below_full_hd(self, model: str) -> None:
        """1080p is 2.07 Mpx, and the production artwork rect is 2.25 Mpx.

        A cap at or above that would mean the software path renders the full rect,
        which measures 110% of one Pi 5 core — a 27 fps ceiling, i.e. below the
        project's 30 fps target.
        """
        assert SWRenderPixelsByModel[model] < 1920 * 1080, model

    @pytest.mark.parametrize("model", sorted(SWRenderPixelsByModel))
    def test_the_cap_is_large_enough_to_be_worth_rendering(self, model: str) -> None:
        """The ~10 ms fixed cost per ``render()`` call makes tiny buffers futile.

        Below roughly 0.5 Mpx the size stops paying, so a cap that small would
        only lose quality.  The Pi 3 / Pi 2 values are deliberately above that
        floor; a board that cannot keep up should give up frame rate instead.
        """
        assert SWRenderPixelsByModel[model] > 200_000, model

    def test_limits_are_positive_integers(self):
        for model, cap in SWRenderPixelsByModel.items():
            assert isinstance(cap, int), model
            assert cap > 0, model
