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

    @property
    def style(self) -> str:
        """The configured transition style, read live from the config.

        Deliberately not cached at construction.  Saving the slideshow card
        hot-reloads the frontend without restarting it (``routes/config.py``
        only bounces services for processing-affecting sections), so a cached
        copy would silently keep the old style until the frame was rebooted —
        which is exactly the "Transition Style does nothing" bug.
        """
        return str(self._config.slideshow.get("transition_style", "crossfade"))

    @property
    def duration_ms(self) -> int:
        """The configured transition duration in milliseconds (read live)."""
        return int(self._config.slideshow.get("transition_duration_ms", 1500))

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0

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

        The two layers are composited by the backend as
        ``incoming @ next_alpha`` drawn OVER ``outgoing @ current_alpha``, on top
        of an opaque background.  That ordering is what dictates the values here:

        * **crossfade** keeps the outgoing layer fully opaque and dissolves the
          incoming one over it.  Fading *both* (the "complementary alpha" idea)
          double-counts the outgoing layer's transparency — the composite becomes
          ``next·t + outgoing·(1-t)²``, which dims the panel by 25% at the midpoint
          and squeezes the whole visible change into the middle of the window, so
          the configured duration barely shows.  Holding the outgoing opaque makes
          it a true dissolve: ``next·t + outgoing·(1-t)``.
        * **fade_through_black** genuinely needs the outgoing to dim — fading it to
          black, then raising the incoming from black — so it *does* use both
          alphas, and its dip to black is the intended effect rather than a bug.
        * **none** is a hard cut, so the outgoing simply stays as it is.
        """
        if self.style == "crossfade":
            if layer == "current":
                # Opaque, NOT 1 - t: see the note above.  The incoming layer's
                # rising alpha is what does the blending.
                return 1.0
            # LINEAR in progress, deliberately without an ease.
            #
            # The incoming alpha used to be ``ease_in_out_cubic(progress)``, which
            # still reached 0 and 1 at the window's ends but did almost nothing for
            # the first and last ~20% of it: 90% of the change happened inside the
            # middle ~54%.  A configured 5 s dissolve therefore *looked* like a
            # ~2.5 s one, which is what "the transition duration isn't affecting
            # the slideshow" turned out to mean.  Proportionally, 10% of the
            # duration is now 10% of the fade.
            return progress
        elif self.style == "fade_through_black":
            t = self.ease_out_quad(progress)
            if layer == "current":
                return max(0.0, 1.0 - t * 2)
            else:
                return max(0.0, (t - 0.5) * 2)
        elif self.style == "none":
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
        """Adopt a reloaded config.

        Only the object reference has to change: :attr:`style` and
        :attr:`duration_ms` are read from it live.  The frontend's hot-reload
        path builds a **new** ``Config`` (``Config.load``) rather than mutating
        the existing one, so without this the engine would keep pointing at the
        config it was constructed with.
        """
        self._config = config
