# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Transition effects — crossfade, slide, fade-through-black.

Provides smooth animated transitions between media items. All effects are
implemented as math-only functions (interpolation, easing) that work with
the abstract DisplayBackend layer.
"""

from __future__ import annotations

import logging

from metixel.shared.config import Config

logger = logging.getLogger(__name__)


class TransitionEngine:
    """Manages transition styles and easing functions.

    Provides interpolation helpers used by the PresentationEngine to
    render smooth crossfades, slides, and other effects between slides.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._style: str = str(config.slideshow.get("transition_style", "crossfade"))
        self._duration_ms: int = int(config.slideshow.get("transition_duration_ms", 1500))

    @property
    def style(self) -> str:
        return self._style

    @property
    def duration_ms(self) -> int:
        return self._duration_ms

    @property
    def duration_s(self) -> float:
        return self._duration_ms / 1000.0

    def ease_in_out_cubic(self, t: float) -> float:
        """Cubic ease-in-out.

        Args:
            t: Progress from 0.0 to 1.0.

        Returns:
            Eased progress value.
        """
        if t < 0.5:
            return 4.0 * t * t * t
        else:
            return 1.0 - pow(-2.0 * t + 2.0, 3) / 2.0

    def ease_out_quad(self, t: float) -> float:
        """Quadratic ease-out."""
        return 1.0 - (1.0 - t) * (1.0 - t)

    def get_alpha(self, progress: float, layer: str) -> float:
        """Get the alpha for a transition layer at a given progress.

        Args:
            progress: Transition progress 0.0 → 1.0.
            layer: "current" or "next".

        Returns:
            Alpha value 0.0–1.0.
        """
        if self._style == "crossfade":
            t = self.ease_in_out_cubic(progress)
            if layer == "current":
                return 1.0 - t
            else:
                return t
        elif self._style == "fade_through_black":
            t = self.ease_out_quad(progress)
            if layer == "current":
                return max(0.0, 1.0 - t * 2)
            else:
                return max(0.0, (t - 0.5) * 2)
        elif self._style == "none":
            # No transition — hard cut at any progress
            return 1.0 if layer == "current" else 0.0
        else:
            # Unknown style — hard cut at midpoint
            return (
                1.0
                if (layer == "current" and progress < 0.5) or (layer == "next" and progress >= 0.5)
                else 0.0
            )

    def reload_config(self, config: Config) -> None:
        """Update transition settings from config."""
        self._style = config.slideshow.get("transition_style", "crossfade")
        self._duration_ms = config.slideshow.get("transition_duration_ms", 1500)
