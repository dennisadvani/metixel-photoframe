# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Layout engine — fit-to-screen, virtual matte board, smart crop.

Determines how each media item should be positioned and sized on screen
based on its aspect ratio and the user's fit preferences.
"""

from __future__ import annotations

import logging

from metixel.shared.models import MediaItem

logger = logging.getLogger(__name__)


class LayoutEngine:
    """Calculates image placement and matte board sizing.

    Supports three fit modes:
    - ``contain``: Show entire image with matte bars (letterbox/pillarbox)
    - ``cover``: Fill screen, cropping excess (like CSS background-size: cover)
    - ``fill``: Stretch to fill (distorts aspect ratio — not recommended)
    """

    def __init__(self, screen_w: int = 1920, screen_h: int = 1080) -> None:
        self._screen_w = screen_w
        self._screen_h = screen_h
        self._screen_ratio = screen_w / screen_h if screen_h > 0 else 1.0
        logger.info(
            "LayoutEngine: screen=%dx%d (ratio %.3f)",
            self._screen_w,
            self._screen_h,
            self._screen_ratio,
        )

    def compute(
        self,
        item: MediaItem,
        fit_mode: str = "contain",
        matte_color: tuple[int, int, int] = (20, 20, 20),
    ) -> dict:
        """Compute the layout for a media item.

        Returns:
            dict with:
            - ``image_rect``: (x, y, w, h) for the image on screen
            - ``matte_rects``: list of (x, y, w, h) for matte board regions
        """
        if item.width == 0 or item.height == 0:
            return {
                "image_rect": (0, 0, self._screen_w, self._screen_h),
                "matte_rects": [],
            }

        image_ratio = item.aspect_ratio

        if fit_mode == "fill":
            return {
                "image_rect": (0, 0, self._screen_w, self._screen_h),
                "matte_rects": [],
            }

        if fit_mode == "cover":
            return self._compute_cover(image_ratio)

        # Default: contain
        return self._compute_contain(image_ratio)

    def _compute_contain(self, image_ratio: float) -> dict:
        """Fit entire image on screen with matte bars."""
        # Near match within 2% — just fill
        if abs(image_ratio - self._screen_ratio) < 0.02:
            return {
                "image_rect": (0, 0, self._screen_w, self._screen_h),
                "matte_rects": [],
            }

        if image_ratio > self._screen_ratio:
            # Image wider → letterbox
            display_h = self._screen_w / image_ratio
            matte_h = (self._screen_h - display_h) / 2
            return {
                "image_rect": (0, matte_h, self._screen_w, display_h),
                "matte_rects": [
                    (0, 0, self._screen_w, matte_h),
                    (0, self._screen_h - matte_h, self._screen_w, matte_h),
                ],
            }
        else:
            # Image taller → pillarbox
            display_w = self._screen_h * image_ratio
            matte_w = (self._screen_w - display_w) / 2
            return {
                "image_rect": (matte_w, 0, display_w, self._screen_h),
                "matte_rects": [
                    (0, 0, matte_w, self._screen_h),
                    (self._screen_w - matte_w, 0, matte_w, self._screen_h),
                ],
            }

    def _compute_cover(self, image_ratio: float) -> dict:
        """Fill screen, cropping the excess."""
        if image_ratio > self._screen_ratio:
            # Image is wider → crop sides
            display_h = self._screen_h
            display_w = self._screen_h * image_ratio
            offset_x = (self._screen_w - display_w) / 2
            return {
                "image_rect": (offset_x, 0, display_w, display_h),
                "matte_rects": [],
            }
        else:
            # Image is taller → crop top/bottom
            display_w = self._screen_w
            display_h = self._screen_w / image_ratio
            offset_y = (self._screen_h - display_h) / 2
            return {
                "image_rect": (0, offset_y, display_w, display_h),
                "matte_rects": [],
            }
