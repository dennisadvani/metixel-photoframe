# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the transition alpha contract.

The alpha pair is not decoration — it *is* the effect.  A backend composites
``incoming @ next_alpha`` drawn OVER ``outgoing @ current_alpha``, on top of an
opaque background, so the pair decides what the panel shows:

    composite = next_alpha * incoming + (1 - next_alpha) * current_alpha * outgoing

That arithmetic is the reason this file exists.  Returning the "obvious"
complementary pair ``(1 - t, t)`` looks right in isolation and is wrong on
screen: the composite becomes ``t*B + (1-t)²*A``, which dims the panel by 25% at
the midpoint *and* squeezes the visible change into the middle of the window — so
the configured duration barely appears to matter.  That was measured on hardware
(Pi 5, 1920x1200) before it was fixed, and
:meth:`TestCrossfade.test_composite_never_dips_below_the_endpoints` is the guard.

These tests need no display, no Qt and no media — only the arithmetic and the
compositing order, which is the part that was wrong.
"""

from __future__ import annotations

import pytest

from metixel.frontend.presentation.transitions import TransitionEngine
from metixel.shared.config import Config


def _engine(style: str) -> TransitionEngine:
    cfg = Config()
    cfg.update("slideshow", {"transition_style": style, "transition_duration_ms": 1000})
    return TransitionEngine(cfg)


def _coverage(engine: TransitionEngine, progress: float) -> float:
    """Fraction of the frame the two artwork layers account for between them.

    ``1.0`` means the two layers fully cover the frame and nothing of the
    background shows through; ``0.0`` means a fully black frame.  This is the
    quantity that was wrong: with the old complementary-alpha pair the layers
    covered only ``next + (1 - next) * current``, which dipped as low as 0.75 —
    the panel dimmed through every transition.
    """
    current = engine.get_alpha(progress, "current")
    upcoming = engine.get_alpha(progress, "next")
    return upcoming + (1.0 - upcoming) * current


def _composite(
    engine: TransitionEngine, progress: float, outgoing: float, incoming: float
) -> float:
    """The pixel the canvas paints for two solid greys.

    Mirrors the backend's compositing order exactly: the incoming artwork is
    drawn OVER the outgoing layer, on top of an opaque background.
    """
    current = engine.get_alpha(progress, "current")
    upcoming = engine.get_alpha(progress, "next")
    return upcoming * incoming + (1.0 - upcoming) * current * outgoing


class TestCrossfade:
    def test_outgoing_layer_stays_opaque_throughout(self) -> None:
        """Only the incoming layer's alpha may vary.

        Fading the outgoing layer as well is the bug this pins: because the two
        layers composite, an outgoing alpha of ``1 - t`` darkens the panel instead
        of blending it away.
        """
        engine = _engine("crossfade")
        for step in range(0, 101, 5):
            assert engine.get_alpha(step / 100, "current") == 1.0

    def test_incoming_rises_monotonically_from_transparent_to_opaque(self) -> None:
        engine = _engine("crossfade")
        alphas = [engine.get_alpha(step / 100, "next") for step in range(101)]

        assert alphas[0] == pytest.approx(0.0)
        assert alphas[-1] == pytest.approx(1.0)
        assert alphas == sorted(alphas), "the dissolve must never reverse"

    def test_incoming_alpha_is_proportional_to_elapsed_time(self) -> None:
        """The fade must take the *whole* configured duration, not just its middle.

        The alpha used to be ``ease_in_out_cubic(progress)``.  That still started at
        0 and ended at 1, so the endpoints looked right — but the curve did almost
        nothing for the first and last ~20% of the window, so a configured 5 s
        dissolve *looked* like a ~2.5 s one and the duration control appeared not to
        work.

        The alpha is now a **sine ease**, which is deliberately not linear — it has
        zero gradient at both ends so the fade has no visible start or stop.  So the
        assertion is a *bounded deviation* from proportional, not equality: the
        point of this test is to catch a curve that steals the duration, and the
        sine does not.

        The bound is what separates the two: the sine peaks at ~0.105 of deviation
        and keeps 10–90% of the change inside 59% of the window, where the cubic
        peaks at ~0.192 and squeezes it into 42%.  A curve worse than the sine is a
        regression; the old cubic is more than twice as bad and still fails here.
        """
        engine = _engine("crossfade")
        #: Generous enough for the sine's soft ends, far too tight for a cubic.
        tolerance = 0.12
        for percent in (5, 10, 25, 50, 75, 90, 95):
            progress = percent / 100
            assert engine.get_alpha(progress, "next") == pytest.approx(progress, abs=tolerance), (
                f"{percent}% through the transition the incoming layer should still be "
                f"close to {percent}% opacity — a curve that races ahead here is "
                "stealing the configured duration"
            )

    def test_most_of_the_change_uses_most_of_the_window(self) -> None:
        """A 10%→90% fade must occupy a large majority of the duration.

        The threshold is set so a gentle ease passes and the old cubic does not:
        sine = 0.59, cubic = 0.42.  Anything under half the window means the
        configured duration is mostly spent not moving.
        """
        engine = _engine("crossfade")

        def progress_at(alpha: float) -> float:
            for step in range(1001):
                candidate = step / 1000
                if engine.get_alpha(candidate, "next") >= alpha:
                    return candidate
            return 1.0

        span = progress_at(0.9) - progress_at(0.1)
        assert span > 0.55, (
            f"only {span:.0%} of the window carries the visible change — the rest of "
            "the configured duration is spent barely moving"
        )

    def test_frame_stays_fully_opaque_throughout(self) -> None:
        """No background may show through mid-dissolve.

        This is the guard for the measured hardware bug.  The old pair
        ``(1 - t, t)`` produced a coverage of ``t + (1-t)^2``, which is 0.75 at
        the midpoint — a 25% dimming pulse, confirmed on a Pi 5 by screenshotting
        a transition (observed midpoint 73.25 against the 73.8 this predicts, and
        112.7 for a correct dissolve).
        """
        engine = _engine("crossfade")
        for step in range(101):
            assert _coverage(engine, step / 100) == pytest.approx(1.0)

    def test_midpoint_shows_an_even_mix(self) -> None:
        """The mix must progress across the whole window, not jump early.

        With the outgoing layer also fading, the panel reached a 50/50 *mix* at
        roughly 16% of the window and then spent the remaining two-thirds barely
        changing — which is why the configured duration appeared to do nothing.
        Half-way through, a dissolve must genuinely be half-way.
        """
        engine = _engine("crossfade")
        outgoing, incoming = 200.0, 40.0

        assert _composite(engine, 0.0, outgoing, incoming) == pytest.approx(outgoing)
        assert _composite(engine, 1.0, outgoing, incoming) == pytest.approx(incoming)

        midpoint = _composite(engine, 0.5, outgoing, incoming)
        assert midpoint == pytest.approx(
            (outgoing + incoming) / 2, abs=0.05 * (outgoing - incoming)
        ), "half-way through the transition the frame should be half-way between the two photos"


class TestFadeThroughBlack:
    def test_outgoing_fades_to_black_then_incoming_rises(self) -> None:
        """This style *needs* both alphas — its dip is the intended effect.

        Pinning it here keeps the crossfade fix from being generalised into
        "never fade the outgoing layer", which would silently turn this style
        into a plain dissolve.
        """
        engine = _engine("fade_through_black")

        assert engine.get_alpha(0.0, "current") == pytest.approx(1.0)
        assert engine.get_alpha(0.0, "next") == pytest.approx(0.0)
        assert engine.get_alpha(1.0, "current") == pytest.approx(0.0)
        assert engine.get_alpha(1.0, "next") == pytest.approx(1.0)

        # The frame must reach fully black on the way through: essentially zero
        # coverage is what "fade through black" means, and it is the opposite of
        # the crossfade's requirement that coverage stay at 1.0.  The dip is a
        # single instant in the window, so a percentage-step grid lands just
        # either side of it — hence a threshold rather than an exact zero.
        darkest = min(_coverage(engine, step / 100) for step in range(101))
        assert darkest < 0.02, (
            "fade_through_black must pass through a black frame; the darkest "
            f"point found was {darkest:.4f} coverage"
        )


class TestNone:
    def test_is_a_hard_cut(self) -> None:
        """No transition: the outgoing stays and the incoming never appears."""
        engine = _engine("none")
        for step in range(0, 101, 10):
            progress = step / 100
            assert engine.get_alpha(progress, "current") == 1.0
            assert engine.get_alpha(progress, "next") == 0.0


class TestUnknownStyle:
    def test_falls_back_to_a_cut_at_the_midpoint(self) -> None:
        """A typo in config must not leave the panel showing nothing."""
        engine = _engine("not-a-real-style")
        assert engine.get_alpha(0.25, "current") == 1.0
        assert engine.get_alpha(0.75, "current") == 0.0
        assert engine.get_alpha(0.25, "next") == 0.0
        assert engine.get_alpha(0.75, "next") == 1.0


class TestTheEaseIsSymmetricAndGentle:
    """The crossfade and fade-through-black are eased, not linear.

    A linear ramp starts at full speed on the first frame and stops dead on the
    last, which reads as mechanical: the fade "begins" and "ends" rather than
    flowing.  A gentle ease removes both abrupt edges.

    The curve is a **sine**, chosen over a cubic for a reason that these tests
    pin: a cubic eases so hard it steals the configured duration (that is the
    historical bug), whereas the sine's soft ends cost almost no pacing.
    """

    def test_it_has_zero_gradient_at_both_ends(self) -> None:
        """The definition of "eased": no visible start, no visible stop.

        Measured as a small first and last step, which is what the eye reads as
        smoothness at the joins.
        """
        engine = _engine("crossfade")
        step = 0.001

        first = engine.get_alpha(step, "next") - engine.get_alpha(0.0, "next")
        last = engine.get_alpha(1.0, "next") - engine.get_alpha(1.0 - step, "next")

        assert first < engine.ease_in_out_sine(0.01), "the fade must not lurch at the start"
        assert last < engine.ease_in_out_sine(0.01), "the fade must not stop dead"

    def test_it_is_symmetric_about_the_midpoint(self) -> None:
        """Symmetric, so the fade cannot look like it is rushing one way.

        ``f(0.5 - d) + f(0.5 + d) == 1`` is what symmetry means for a curve that
        runs 0 → 1.
        """
        engine = _engine("crossfade")
        for d in (0.05, 0.15, 0.25, 0.4):
            low = engine.get_alpha(0.5 - d, "next")
            high = engine.get_alpha(0.5 + d, "next")
            assert low + high == pytest.approx(1.0, abs=1e-6)

    def test_it_still_hits_both_endpoints(self) -> None:
        """An ease must not stop short of fully opaque, or the frame dims."""
        engine = _engine("crossfade")

        assert engine.get_alpha(0.0, "next") == pytest.approx(0.0)
        assert engine.get_alpha(1.0, "next") == pytest.approx(1.0)

    def test_it_is_slower_than_linear_at_the_midpoint(self) -> None:
        """The ease must actually be an ease, not a no-op.

        A curve that matched linear exactly would be indistinguishable from no
        easing at all — this is the assertion that would catch someone
        "simplifying" ``ease_in_out_sine`` back to ``return t``.
        """
        engine = _engine("crossfade")

        assert engine.get_alpha(0.25, "next") < 0.25
        assert engine.get_alpha(0.75, "next") > 0.75

    def test_the_deviation_stays_bounded(self) -> None:
        """Gentle, not a cubic: the pacing must survive the easing.

        The maximum deviation from linear is what "how hard does this ease"
        means numerically.  The sine is ~0.105; the cubic that broke the duration
        control was ~0.192.  Keeping the bound tight means a curve that eases
        *harder* than the sine fails here rather than silently showing up as the
        duration bug again.
        """
        engine = _engine("crossfade")
        worst = max(
            abs(engine.get_alpha(step / 1000, "next") - step / 1000) for step in range(1001)
        )

        assert worst < 0.13, f"the ease deviates {worst:.3f} from linear — that is a cubic"
        assert worst > 0.05, "the curve barely eases; it is effectively linear"


class TestFadeThroughBlackIsEasedSymmetrically:
    def test_each_half_reaches_its_endpoint(self) -> None:
        engine = _engine("fade_through_black")

        assert engine.get_alpha(0.0, "current") == pytest.approx(1.0)
        assert engine.get_alpha(0.5, "current") == pytest.approx(0.0)
        assert engine.get_alpha(0.5, "next") == pytest.approx(0.0)
        assert engine.get_alpha(1.0, "next") == pytest.approx(1.0)

    def test_the_two_halves_mirror_each_other(self) -> None:
        """The way down and the way up must take the same shape.

        Asymmetric easing (the previous ``ease_out_quad``) made the dip fast and
        the rise slow, which reads as a stutter at the black point rather than one
        continuous breath.
        """
        engine = _engine("fade_through_black")

        for d in (0.05, 0.15, 0.25, 0.4):
            going_down = engine.get_alpha(0.5 - d, "current")
            coming_up = engine.get_alpha(0.5 + d, "next")
            assert going_down == pytest.approx(coming_up, abs=1e-6), (
                "the descent and the ascent must mirror each other"
            )

    def test_it_still_passes_through_black(self) -> None:
        """Easing must not lift the floor off black."""
        engine = _engine("fade_through_black")
        darkest = min(_coverage(engine, step / 200) for step in range(201))

        assert darkest < 0.02
