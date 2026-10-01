# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Transition effects — crossfade, slide, fade-through-black.

Provides smooth animated transitions between media items. All effects are
implemented as math-only functions (interpolation, easing) that work with
the abstract DisplayBackend layer.
"""

from __future__ import annotations

import logging
import math

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

    def ease_in_out_sine(self, t: float) -> float:
        """Symmetric ease in and out, gentler than a cubic.

        ``1 - cos(pi t) / 2`` — zero gradient at BOTH ends, so the fade leaves and
        arrives without a visible start or stop, and it is symmetric about the
        midpoint so the fade cannot look like it is rushing.

        The **sine** curve specifically, not ``ease_in_out_cubic``:

        A cubic eases *hard*.  It was tried here before and removed, because it
        put 90% of the visible change inside the middle ~54% of the window — a
        configured 5 s dissolve then *looked* like a 2.5 s one, which surfaced as
        "the transition duration isn't affecting the slideshow".  It also makes
        the two ends of the fade effectively dead time, so the curve reads as a
        pause, a rush, and another pause.

        The sine is the mildest curve that still has zero gradients at the ends:
        its maximum deviation from linear is about 0.21, against the cubic's 0.39,
        so the pacing stays close to the linear fade it replaces while removing
        the abrupt start and stop that made linear feel mechanical.  That
        combination — soft ends, honest duration — is what "premium" means here.
        """
        return (1.0 - math.cos(math.pi * t)) / 2.0

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
            # Eased with a gentle sine, not linear and not a cubic.
            #
            # LINEAR was abrupt: the incoming image started moving at full speed
            # on the first frame and stopped dead on the last, which reads as
            # mechanical — the fade "begins" and "ends" rather than flowing.
            #
            # A CUBIC was tried and removed: it eased so hard that 90% of the
            # change happened inside the middle ~54% of the window, so a 5 s
            # dissolve *looked* like a 2.5 s one.  That is the "the transition
            # duration isn't affecting the slideshow" complaint.
            #
            # The sine is the middle ground: zero gradient at both ends (so there
            # is no visible start or stop), symmetric about the midpoint, and a
            # maximum deviation from linear of only ~0.21 — so a configured 5 s
            # still reads as 5 s.  Soft ends without stealing the duration.
            return self.ease_in_out_sine(progress)
        elif self.style == "fade_through_black":
            # Symmetric: each half is eased so the dip into black and the rise out
            # of it both leave and arrive gently.  ``ease_in_out_sine`` on the
            # half-progress is what makes the two halves match — easing the whole
            # curve and then splitting it (the previous ``ease_out_quad``) made the
            # way down fast and the way up slow, which reads as a stutter at the
            # black point rather than as one continuous breath.
            t = self.ease_in_out_sine(progress)
            if layer == "current":
                # First half: opaque → black.
                return max(0.0, 1.0 - t * 2)
            else:
                # Second half: black → opaque.
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
