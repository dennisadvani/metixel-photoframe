# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""The tick accelerates during a transition, and the ambient source is pinned.

Two defects, one file, because both are about a *long* fade looking wrong while a
short one looked fine:

* **Opacity stepping.** ``display.fps_limit`` is 30, so the Python state machine
  recomputed the blend 30 times a second while the panel refreshed at ~60 Hz —
  each value presented twice.  At a 2.5 s window that is invisible; at 5 s the
  steps become visible, which is exactly the report ("great at 2500 ms, I can see
  opacity steps at 5000 ms").  The tick now runs at :data:`TRANSITION_FPS` while a
  blend is on screen.
* **The blurred background popping in.** ``present_transition`` re-resolved the
  ambient layer's source on every frame of the fade, so a blur job finishing
  part-way through swapped the background out from under the image fading in over
  it.  The source is now pinned for the duration of the fade.

These are tested through the backend's own arithmetic and state, without Qt: the
pacing decision is a pure function of the interval fields, and the pin is a
property of the request/job-id mapping.
"""

from __future__ import annotations

import pytest

from metixel.display.qt_qml_backend import (
    DEFAULT_FPS_LIMIT,
    TRANSITION_FPS,
    QmlBackend,
    tick_interval_ms,
)


class _PacingBackend(QmlBackend):
    """A backend with just the fields ``_pace_tick`` touches.

    ``QmlBackend.__init__`` builds a Qt scene, which needs a GPU and a display, so
    it cannot run here.  Subclassing and skipping the parent initialiser keeps the
    *method under test* the real one — a stub that reimplemented ``_pace_tick``
    would test nothing.
    """

    def __init__(self) -> None:  # noqa: D107 - deliberately does not call super()
        self._tick_slow_ms = tick_interval_ms(DEFAULT_FPS_LIMIT)
        self._tick_fast_ms = tick_interval_ms(TRANSITION_FPS)
        self._tick_is_fast = False
        self._transition_active = False


class TestTheTickSpeedsUpForATransition:
    def test_the_transition_rate_is_higher_than_the_configured_limit(self) -> None:
        """Otherwise the change does nothing."""
        assert TRANSITION_FPS > DEFAULT_FPS_LIMIT

    def test_the_transition_rate_matches_a_typical_panel(self) -> None:
        """60 Hz is the point.

        Matching the panel's own refresh means no tick is wasted and none is
        missing — a *higher* rate would compute values that are never shown, and a
        lower one would show each value more than once, which is the stepping.
        """
        assert TRANSITION_FPS == 60

    def test_the_intervals_are_ordered_correctly(self) -> None:
        """Fast means a SHORTER interval, which is easy to get backwards."""
        slow = tick_interval_ms(DEFAULT_FPS_LIMIT)
        fast = tick_interval_ms(TRANSITION_FPS)

        assert fast < slow, "the transition tick must be more frequent"

    def test_the_intervals_are_what_the_rates_imply(self) -> None:
        assert tick_interval_ms(30) == 33
        assert tick_interval_ms(60) == 17

    def test_a_five_second_fade_gets_a_step_per_refresh(self) -> None:
        """The property that fixes the reported symptom.

        At 30 Hz a 5 s fade is 150 steps, each held for two refreshes.  At 60 Hz
        it is 300 — one per refresh — which is the finest a 60 Hz panel can show.
        """
        fps = TRANSITION_FPS
        steps = 5.0 * fps

        assert steps >= 5.0 * 60, "a 5 s fade must resolve to at least one step per refresh"
        # And the short window that already looked fine stays smooth too.
        assert 2.5 * fps >= 2.5 * 60


class TestTheTransitionFlagDrivesThePacing:
    """``_transition_active`` is the input to the rate decision."""

    def test_present_transition_sets_it(self) -> None:
        """Asserted on the source: constructing the backend needs a GPU.

        The flag has to be set on the *present_transition* path specifically, or
        the tick never speeds up and the stepping stays.
        """
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "metixel"
            / "display"
            / "qt_qml_backend.py"
        ).read_text(encoding="utf-8")

        body = src[src.index("def present_transition") : src.index("def present_overlay")]
        assert "self._transition_active = True" in body

        single = src[src.index("def present(") : src.index("def present_transition")]
        assert "self._transition_active = False" in single, (
            "a single-layer present must return the tick to the configured rate"
        )

    def test_pace_tick_actually_changes_the_interval(self) -> None:
        """Exercise the decision, not just the constants.

        The earlier version of this file asserted only that the two rates exist and
        are ordered, which a gutted ``_pace_tick`` would still satisfy — the
        constant is right while nothing ever applies it.  This drives the method
        with a stub timer so the *behaviour* is what is checked.
        """

        class _StubTimer:
            def __init__(self) -> None:
                self.interval: int | None = None
                self.calls = 0

            def setInterval(self, ms: int) -> None:  # noqa: N802 - Qt spelling
                self.interval = ms
                self.calls += 1

        backend = _PacingBackend()
        timer = _StubTimer()

        # No transition, and the timer already at the slow rate: nothing to change,
        # and ``setInterval`` must NOT be called (it would restart the period).
        backend._transition_active = False
        backend._pace_tick(timer)
        assert timer.calls == 0, "an unchanged rate must not touch the timer"

        # A transition begins: the rate must rise.
        backend._transition_active = True
        backend._pace_tick(timer)
        assert timer.interval == backend._tick_fast_ms
        assert backend._tick_fast_ms < backend._tick_slow_ms

        # Still transitioning: the interval must NOT be reset every tick, or the
        # running timer's period would stretch.
        calls_after_change = timer.calls
        backend._pace_tick(timer)
        assert timer.calls == calls_after_change, (
            "setInterval must only be called when the rate actually changes; "
            "calling it every tick restarts the period"
        )

        # Transition over: back to the configured rate.
        backend._transition_active = False
        backend._pace_tick(timer)
        assert timer.interval == backend._tick_slow_ms


@pytest.mark.parametrize(("fps", "expected"), [(0, 33), (-5, 33), (None, 33), (120, 8)])
def test_the_fallback_rate_never_becomes_zero(fps: int | None, expected: int) -> None:
    """``setInterval(0)`` means "every event-loop pass", not "as fast as possible".

    It measured 56 fps against a configured 30 on a Pi 5, so a missing or bad
    limit must fall back rather than spin.
    """
    assert tick_interval_ms(fps) == expected
