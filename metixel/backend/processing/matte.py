# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Virtual matte board generator.

Creates colored background plates for images that don't match the screen
aspect ratio. Produces a seamless, gallery-style presentation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class MatteGenerator:
    """Generates virtual matte board backgrounds.

    When an image's aspect ratio doesn't match the display, a colored matte
    (letterbox/pillarbox) is rendered behind the image to avoid black bars
    or ugly cropping.
    """

    def __init__(
        self,
        screen_width: int = 1920,
        screen_height: int = 1080,
        default_color: tuple[int, int, int] = (20, 20, 20),  # Dark gray
        cache_dir: Path | None = None,
    ) -> None:
        self._screen_w = screen_width
        self._screen_h = screen_height
        self._screen_ratio = screen_width / screen_height
        self._default_color = default_color
        self._cache_dir = cache_dir / "matte" if cache_dir else None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    def get_layout(
        self, image_w: int, image_h: int, matte_color: tuple[int, int, int] | None = None
    ) -> dict:
        """Calculate the layout for an image on screen.

        Returns a dict with:
        - ``fit``: "fill", "contain_letterbox", or "contain_pillarbox"
        - ``image_rect``: (x, y, w, h) for the image on screen
        - ``matte_rects``: list of (x, y, w, h) for matte board regions
        - ``matte_color``: RGB tuple

        Uses a tolerance of 2% aspect ratio difference before applying matte.
        """
        if image_w == 0 or image_h == 0:
            return self._fill_screen()

        image_ratio = image_w / image_h
        color = matte_color or self._default_color

        # Near-match: fill screen (with minor cropping if needed)
        if abs(image_ratio - self._screen_ratio) < 0.02:
            return {
                "fit": "fill",
                "image_rect": (0, 0, self._screen_w, self._screen_h),
                "matte_rects": [],
                "matte_color": color,
            }

        if image_ratio > self._screen_ratio:
            # Image is wider → letterbox (matte top/bottom)
            display_h = self._screen_w / image_ratio
            matte_h = (self._screen_h - display_h) / 2
            return {
                "fit": "contain_letterbox",
                "image_rect": (0, matte_h, self._screen_w, display_h),
                "matte_rects": [
                    (0, 0, self._screen_w, matte_h),  # Top
                    (0, self._screen_h - matte_h, self._screen_w, matte_h),  # Bottom
                ],
                "matte_color": color,
            }
        else:
            # Image is taller → pillarbox (matte left/right)
            display_w = self._screen_h * image_ratio
            matte_w = (self._screen_w - display_w) / 2
            return {
                "fit": "contain_pillarbox",
                "image_rect": (matte_w, 0, display_w, self._screen_h),
                "matte_rects": [
                    (0, 0, matte_w, self._screen_h),  # Left
                    (self._screen_w - matte_w, 0, matte_w, self._screen_h),  # Right
                ],
                "matte_color": color,
            }

    def generate_matte_image(self, color: tuple[int, int, int] | None = None) -> np.ndarray:
        """Generate a full-screen matte image as a numpy array.

        Can be loaded as a GPU texture and drawn behind images.
        """
        c = color or self._default_color
        matte = np.full((self._screen_h, self._screen_w, 3), c, dtype=np.uint8)
        return matte

    @staticmethod
    def _fill_screen() -> dict:
        return {
            "fit": "fill",
            "image_rect": (0, 0, 0, 0),  # Will be set by caller
            "matte_rects": [],
            "matte_color": (0, 0, 0),
        }
