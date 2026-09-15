# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the render timer's interval calculation.

``display.fps_limit`` is a user-facing setting, and on the Qt backend it was
plumbed all the way into ``create()`` and then never read — ``schedule()`` used
``QTimer.setInterval(0)``.  The trap is that ``0`` does **not** mean "no timer":
it means "fire as soon as the event loop can drain", i.e. unbounded.  Measured on
a Pi 5 that produced **56 fps against a configured 30**, nearly doubling the
full-screen composite budget (156% CPU) for no benefit.

Importing :mod:`metixel.display.qt_backend` needs no Qt — it defers every PySide6
import into the functions that use it — which is why this arithmetic lives in a
module-level helper that CI can test.
"""

from __future__ import annotations

import pytest

from metixel.display.qt_backend import DEFAULT_FPS_LIMIT, tick_interval_ms

FRAME_HZ = 1000


class TestTickInterval:
    def test_thirty_fps_is_a_33ms_interval(self) -> None:
        assert tick_interval_ms(30) == 33

    @pytest.mark.parametrize("fps", [10, 24, 30, 60])
    def test_interval_matches_the_requested_rate(self, fps: int) -> None:
        assert tick_interval_ms(fps) == round(FRAME_HZ / fps)

    def test_interval_is_never_zero(self) -> None:
        """The whole bug: a 0 ms interval renders as fast as the loop allows."""
        for fps in (1, 5, 30, 240, 10_000):
            assert tick_interval_ms(fps) >= 1

    @pytest.mark.parametrize("missing", [None, 0, -1])
    def test_missing_or_nonsense_falls_back_to_the_default(self, missing) -> None:
        assert tick_interval_ms(missing) == tick_interval_ms(DEFAULT_FPS_LIMIT)

    def test_default_matches_the_config_default(self) -> None:
        """Drift here would silently change the frame budget on every device."""
        from metixel.shared.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["display"]["fps_limit"] == DEFAULT_FPS_LIMIT

    def test_higher_limit_means_a_shorter_interval(self) -> None:
        assert tick_interval_ms(60) < tick_interval_ms(30) < tick_interval_ms(15)
