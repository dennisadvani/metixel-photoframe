# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""The curtain that masks a disruptive change.

The contract is an ORDERING, and that is what these tests pin:

1. fade to black;
2. only once the screen is *completely* black, run the operation.

An implementation that fired the operation at 90 % alpha, or on the same tick the
fade began, would satisfy "there is a fade" while showing exactly the tearing the
curtain exists to hide — so the assertions are about ``finished`` being gated on
full opacity, not about a fade happening at all.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from metixel.frontend.overlay.curtain_layer import FADE_SECONDS, CurtainLayer
from metixel.frontend.overlay.layer import OverlayLayer


@pytest.fixture
def curtain() -> CurtainLayer:
    return CurtainLayer(1200, 1920)


def _run_to_black(curtain: CurtainLayer, *, timeout: float = 5.0) -> None:
    """Drive the fade until it reports finished, or fail."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        curtain.update()
        if curtain.finished:
            return
        time.sleep(0.005)
    pytest.fail(f"the curtain never reached black (alpha={curtain.alpha})")


class TestItStartsInvisible:
    def test_a_fresh_curtain_paints_nothing(self, curtain: CurtainLayer) -> None:
        assert curtain.visible is False
        assert curtain.render() == []

    def test_it_does_not_report_finished(self, curtain: CurtainLayer) -> None:
        """``finished`` must not be true before anything has happened."""
        assert curtain.finished is False

    def test_it_does_not_block_the_overlay_from_idling(self, curtain: CurtainLayer) -> None:
        """An invisible layer must still count as "nothing to repaint".

        ``OverlayManager.needs_repaint`` only consults *visible* layers, so a
        curtain that stayed visible while idle would keep the render loop awake
        forever — a permanent repaint on a device that is supposed to idle.
        """
        assert curtain.visible is False


class TestItFadesToBlack:
    def test_begin_makes_it_visible(self, curtain: CurtainLayer) -> None:
        curtain.begin("a rotation")

        assert curtain.visible is True
        assert curtain.render(), "the curtain must paint once it is coming down"

    def test_the_alpha_ramps_from_zero(self, curtain: CurtainLayer) -> None:
        curtain.begin()

        assert curtain.alpha == 0.0, "the fade must start transparent, not snap to black"

    def test_it_reaches_full_opacity_and_holds(self, curtain: CurtainLayer) -> None:
        curtain.begin()

        _run_to_black(curtain)

        assert curtain.alpha == 1.0
        assert curtain.is_held

    def test_a_partial_fade_is_not_finished(self) -> None:
        """The heart of the contract: half-black must not count as black.

        Firing the operation here is the failure mode this whole layer exists to
        prevent — content would still be visible underneath.
        """
        curtain = CurtainLayer(1200, 1920)
        curtain.begin()
        # One short tick, deliberately well inside the fade window.
        time.sleep(FADE_SECONDS / 4)
        curtain.update()

        assert 0.0 < curtain.alpha < 1.0, "precondition: a partial fade"
        assert curtain.finished is False, (
            "the operation must not be allowed to run while content is still visible"
        )

    def test_the_element_covers_the_whole_screen(self, curtain: CurtainLayer) -> None:
        curtain.begin()

        elements = curtain.render()

        assert len(elements) == 1
        assert elements[0].kind == "rect"
        assert elements[0].rect == (0, 0, 1200, 1920)
        assert elements[0].colour == "#000000"

    def test_the_element_alpha_tracks_the_fade(self, curtain: CurtainLayer) -> None:
        curtain.begin()
        time.sleep(FADE_SECONDS / 4)
        curtain.update()
        partial = curtain.render()[0].alpha

        assert partial == pytest.approx(curtain.alpha)
        assert 0.0 < partial < 1.0

    def test_it_paints_nothing_once_released(self, curtain: CurtainLayer) -> None:
        curtain.begin()
        _run_to_black(curtain)

        curtain.release()

        assert curtain.visible is False
        assert curtain.render() == []
        assert curtain.alpha == 0.0


class TestItOutranksEveryOtherLayer:
    def test_the_z_is_below_the_boot_screen(self) -> None:
        """Lower z is closer to the camera in this stack.

        The boot screen claims ``Z_BOOT = 0.0`` and is documented as closest, so
        a curtain that must cover *it* has to be below zero.  Tying with it would
        be an undefined order rather than a guaranteed win.
        """
        from metixel.frontend.overlay.layer import OverlayLayer

        assert CurtainLayer.Z_CURTAIN < OverlayLayer.Z_BOOT

    def test_it_paints_last_across_the_whole_overlay(self) -> None:
        """The property that actually matters, asserted through the manager.

        The manager flattens every visible layer's elements into ONE list and
        sorts that list descending by z, so the smallest z is composited last and
        therefore lands on top.  Layer *registration* order is irrelevant — only
        the element z decides — which is why this checks the flattened output
        rather than the layer list.
        """
        from metixel.frontend.overlay.layer import OverlayLayer
        from metixel.frontend.overlay.manager import OverlayManager

        manager = OverlayManager()
        boot_elements = [
            _stub_element(z=OverlayLayer.Z_BOOT),
            _stub_element(z=OverlayLayer.Z_BOOT - 0.001),
        ]
        boot = _StubLayer("boot", OverlayLayer.Z_BOOT, boot_elements)
        curtain = CurtainLayer(1200, 1920)
        curtain.begin()
        # Registered before the curtain on purpose: order must not matter.
        manager.add_layer(boot)
        manager.add_layer(curtain)

        captured: list[list[Any]] = []
        backend = _CapturingBackend(captured)
        manager.draw(backend)  # type: ignore[arg-type]

        assert captured, "the manager must have composited something"
        painted = captured[-1]
        assert painted[-1].z == CurtainLayer.Z_CURTAIN, (
            "the curtain must be the LAST element painted, i.e. on top of everything"
        )
        assert painted[-1].z < min(e.z for e in boot_elements)


class TestRepeatedRequestsAreIdempotent:
    def test_a_second_begin_does_not_restart_the_fade(self, curtain: CurtainLayer) -> None:
        """A save can trigger several restarts; the fade must not stutter.

        Restarting the ramp on each request would leave the screen oscillating
        toward black without ever getting there on a busy path — and the
        operation waits for black, so it would stall indefinitely.
        """
        curtain.begin()
        time.sleep(FADE_SECONDS / 2)
        curtain.update()
        mid = curtain.alpha
        assert mid > 0.0

        curtain.begin()  # second request mid-fade

        assert curtain.alpha == mid, "the ramp must continue, not restart from zero"

    def test_begin_on_a_held_curtain_leaves_it_black(self, curtain: CurtainLayer) -> None:
        curtain.begin()
        _run_to_black(curtain)

        curtain.begin("again")

        assert curtain.finished is True
        assert curtain.alpha == 1.0

    def test_the_first_reason_wins_over_a_blank_later_one(self, curtain: CurtainLayer) -> None:
        curtain.begin("a rotation")
        curtain.begin()

        assert curtain.reason == "a rotation"


class TestItCanCoverTheNewGeometry:
    def test_retarget_follows_a_rotation(self, curtain: CurtainLayer) -> None:
        """A rotation swaps width and height.

        A curtain still laid out for the old geometry would leave an uncovered
        strip at precisely the moment it is needed — the rotation itself.
        """
        curtain.begin()
        curtain.retarget(1920, 1200)

        assert curtain.render()[0].rect == (0, 0, 1920, 1200)


class TestTheTeardownPathHoldsBlackWithoutAnimating:
    def test_hold_black_now_is_immediately_opaque(self, curtain: CurtainLayer) -> None:
        """At shutdown there is no loop left to drive a fade.

        The guarantee that matters — the screen is black before the disruptive
        step — still holds; it is just reached without the animation, because a
        ramp over a surface that is about to stop being presented would be cut
        off part-way and read as a glitch of its own.
        """
        curtain.hold_black_now("shutdown")

        assert curtain.finished is True
        assert curtain.alpha == 1.0
        assert curtain.render()[0].alpha == 1.0

    def test_holding_black_needs_no_ticks(self, curtain: CurtainLayer) -> None:
        curtain.hold_black_now()

        assert curtain.finished is True, "no update() call may be required"


class TestVisibilityCannotBeOverridden:
    def test_the_visible_setter_is_inert(self, curtain: CurtainLayer) -> None:
        """A settable ``visible`` could hide a curtain mid-fade.

        That would silently defeat the precondition — the operation would run
        with content still showing — so visibility is derived from the state
        machine and not assignable.
        """
        curtain.begin()

        curtain.visible = False

        assert curtain.visible is True, "the curtain must not be hideable while fading"

    def test_release_is_the_only_way_down(self, curtain: CurtainLayer) -> None:
        curtain.begin()
        _run_to_black(curtain)

        curtain.release()

        assert curtain.visible is False


class _StubLayer(OverlayLayer):
    """A layer that paints whatever elements it is handed.

    Subclasses the real ``OverlayLayer`` rather than duck-typing it, so a change
    to the interface (the ``needs_repaint`` default, a new abstract method) shows
    up here as a failure instead of as a silently thinner test.
    """

    def __init__(self, name: str, z_base: float, elements: list[Any] | None = None) -> None:
        super().__init__(name, z_base)
        self._elements = elements or []

    def update(self, shared_state: dict[str, Any] | None = None) -> None:
        pass

    def draw(self, backend: Any) -> None:
        """No-op — the manager composites via ``render``."""

    def render(self) -> list[Any]:
        return list(self._elements)


def _stub_element(z: float) -> _StubElement:
    return _StubElement(z)


class _StubElement:
    """An element carrying only a z, which is all the sort consults."""

    def __init__(self, z: float) -> None:
        self.z = z


class _CapturingBackend:
    """Records what ``present_overlay`` was handed."""

    def __init__(self, sink: list[list[Any]]) -> None:
        self._sink = sink

    def present_overlay(self, elements: list[Any]) -> None:
        self._sink.append(list(elements))
