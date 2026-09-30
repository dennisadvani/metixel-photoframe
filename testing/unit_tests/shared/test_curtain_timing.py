# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""One definition of the curtain's timing, shared by both processes.

The backend cannot see the frontend's curtain, so "do not act until the screen is
black" is implemented as a *wait*, not a request/response.  That makes the wait a
bet, and a bet is only safe while both sides use the same number:

* the frontend animates for ``CURTAIN_FADE_SECONDS``;
* the backend sleeps for ``CURTAIN_SETTLE_SECONDS`` before acting.

Two independent literals would drift, and the failure is silent and one-sided —
a wait that is too short still "works", it just lands part-way through the ramp
and shows the tearing the curtain exists to hide, with nothing in the log.

These tests pin the single-source-of-truth property rather than the values, so
tuning the fade cannot leave the waits behind.
"""

from __future__ import annotations

from pathlib import Path

from metixel.shared.timing import CURTAIN_FADE_SECONDS, CURTAIN_SETTLE_SECONDS

_REPO = Path(__file__).resolve().parents[3]
_SHARED_TIMING = _REPO / "src" / "metixel" / "shared" / "timing.py"
_CURTAIN = _REPO / "src" / "metixel" / "frontend" / "overlay" / "curtain_layer.py"
_SYSTEM_ROUTE = _REPO / "src" / "metixel" / "backend" / "web" / "routes" / "system.py"
_CONFIG_ROUTE = _REPO / "src" / "metixel" / "backend" / "web" / "routes" / "config.py"


class TestTheConstantHasOneDefinition:
    def test_the_fade_duration_lives_in_the_shared_module(self) -> None:
        src = _SHARED_TIMING.read_text(encoding="utf-8")

        assert "CURTAIN_FADE_SECONDS = " in src

    def test_the_curtain_re_exports_it_rather_than_restating_it(self) -> None:
        """A literal here would be a second definition, which is the whole hazard."""
        src = _CURTAIN.read_text(encoding="utf-8")

        assert "FADE_SECONDS = CURTAIN_FADE_SECONDS" in src, (
            "the curtain must re-export the shared fade duration, not define its own"
        )

    def test_the_curtain_holds_no_bare_fade_literal(self) -> None:
        src = _CURTAIN.read_text(encoding="utf-8")

        assert "FADE_SECONDS = 0." not in src, (
            "a bare literal is exactly the duplicate this guards against"
        )

    def test_the_settle_delay_derives_from_the_fade(self) -> None:
        src = _SHARED_TIMING.read_text(encoding="utf-8")

        assert "CURTAIN_SETTLE_SECONDS = CURTAIN_FADE_SECONDS +" in src, (
            "the settle delay must be derived, not written as its own number"
        )


class TestTheValuesAreCoherent:
    def test_the_fade_is_a_sensible_duration(self) -> None:
        assert 0.1 <= CURTAIN_FADE_SECONDS <= 2.0

    def test_the_settle_delay_exceeds_the_fade(self) -> None:
        """The only property that makes the bet safe in the right direction."""
        assert CURTAIN_SETTLE_SECONDS > CURTAIN_FADE_SECONDS

    def test_the_margin_covers_the_ipc_and_a_stretched_tick(self) -> None:
        """A margin of a frame or two would not.

        The datagram has to be delivered and picked up on the next tick, and the
        fade is driven by the render loop, so a throttled tick stretches it in
        wall-clock terms.  Anything under half a second of headroom is not really
        a margin.
        """
        assert CURTAIN_SETTLE_SECONDS - CURTAIN_FADE_SECONDS >= 0.5

    def test_the_settle_delay_is_not_so_long_it_feels_broken(self) -> None:
        """A save that appears to hang is its own defect."""
        assert CURTAIN_SETTLE_SECONDS <= 5.0


class TestBothSidesConsumeTheSharedValue:
    def test_the_power_routes_use_the_shared_settle_delay(self) -> None:
        src = _SYSTEM_ROUTE.read_text(encoding="utf-8")

        assert "from metixel.shared.timing import CURTAIN_SETTLE_SECONDS" in src
        assert src.count("delay=CURTAIN_SETTLE_SECONDS") >= 3, (
            "every curtained power endpoint must use the derived delay"
        )

    def test_the_config_route_uses_the_shared_settle_delay(self) -> None:
        """The pipeline-rebuild restart is the one that applies a rotation."""
        src = _CONFIG_ROUTE.read_text(encoding="utf-8")

        assert "from metixel.shared.timing import CURTAIN_SETTLE_SECONDS" in src
        assert "delay=CURTAIN_SETTLE_SECONDS" in src

    def test_neither_route_restates_the_number(self) -> None:
        """A route may alias the imported constant, but must not re-derive it.

        An alias (``X = IMPORTED_X``) is fine — it just gives the module a local
        name.  A *numeric literal* is the hazard, because that is the copy which
        silently drifts away from the fade it is supposed to outlast.
        """
        for path in (_SYSTEM_ROUTE, _CONFIG_ROUTE):
            src = path.read_text(encoding="utf-8")
            assert not any(
                line.strip().startswith("CURTAIN_SETTLE_SECONDS =") and " + " in line
                for line in src.splitlines()
            ), f"{path.name} derives its own settle delay instead of importing one"
            assert "CURTAIN_SETTLE_SECONDS = 2" not in src, (
                f"{path.name} hardcodes the settle delay"
            )
