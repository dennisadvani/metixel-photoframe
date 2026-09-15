# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Guards for idle rendering: a layer must be able to say whether it changed.

Qt repaints only when asked, and ``paintEvent`` draws whatever the canvas has
stored — so the canvas requests a repaint only when that stored picture changed.
For the slideshow frame the decision is a local identity comparison
(``FrameCanvas._store_layers``).  For overlays it cannot be: a layer animates on
its own clock, and only the layer knows whether its output moved.

So every layer answers ``needs_repaint``, and the overlay manager skips the whole
pass when none asks for one.  These tests pin that contract, plus the invariant
that makes it trustworthy: ``render()`` must be a pure function of stored state.
A layer whose output depends on a clock no flag can see would hide its own change
from the very test meant to detect it.
"""

from __future__ import annotations

from unittest import mock

import pytest

from metixel.frontend.overlay import message_layer
from metixel.frontend.overlay.boot_layer import BootLayer
from metixel.frontend.overlay.layer import OverlayLayer
from metixel.frontend.overlay.manager import OverlayManager
from metixel.frontend.overlay.message_layer import MessageLayer

# Past SLIDE_IN_MS (400 ms) and SLIDE_OUT_MS (300 ms).
_PAST_SLIDE_IN = 0.5


class _Clock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock(monkeypatch):
    """Replace only message_layer's clock, so the rest of the suite is unaffected."""
    fake = _Clock()
    monkeypatch.setattr(message_layer, "time", mock.MagicMock(monotonic=fake))
    return fake


class _Layer(OverlayLayer):
    """Minimal layer with a controllable answer to ``needs_repaint``."""

    def __init__(self, repaint: bool = True) -> None:
        super().__init__("test", OverlayLayer.Z_WIDGETS)
        self._repaint = repaint
        self.renders = 0

    def update(self, shared_state=None) -> None:
        pass

    def draw(self, backend) -> None:
        pass

    @property
    def needs_repaint(self) -> bool:
        return self._repaint

    def render(self):
        self.renders += 1
        return []


def _backend(width: int = 1920):
    backend = mock.MagicMock()
    backend.width = width
    return backend


def _visible_message_layer(clock) -> MessageLayer:
    """A layer holding one message that has finished sliding in."""
    layer = MessageLayer()
    layer.ensure_ready(_backend())
    layer.show("Hello", duration=30.0)
    layer.update({})
    layer.render()
    clock.advance(_PAST_SLIDE_IN)
    layer.update({})
    layer.render()
    return layer


def _rects(elements) -> list[tuple]:
    return [e.rect for e in elements]


class TestLayerContract:
    def test_base_layer_is_conservative(self) -> None:
        """An un-migrated layer must keep repainting, never freeze."""

        class Plain(OverlayLayer):
            def update(self, shared_state=None) -> None:
                pass

            def draw(self, backend) -> None:
                pass

        assert Plain("plain", OverlayLayer.Z_WIDGETS).needs_repaint is True

    def test_boot_layer_repaints_until_it_is_done(self) -> None:
        boot = BootLayer()
        assert boot.needs_repaint is True, "the spinner animates every tick"

        boot.dismiss_immediate()
        assert boot.needs_repaint is False


class TestMessageLayerRepaint:
    def test_animating_message_asks_for_a_repaint(self, clock) -> None:
        layer = MessageLayer()
        layer.ensure_ready(_backend())
        layer.show("Hello", duration=30.0)

        layer.update({})
        assert layer.needs_repaint is True, "the slide-in is in flight"

    def test_a_merely_visible_message_idles(self, clock) -> None:
        """The whole point: a message sitting on screen costs no composites."""
        layer = _visible_message_layer(clock)

        for _ in range(5):
            clock.advance(0.05)
            layer.update({})
            assert layer.needs_repaint is False

    def test_dismissing_asks_for_a_repaint(self, clock) -> None:
        layer = _visible_message_layer(clock)
        assert layer.needs_repaint is False

        msg_id = layer._msgs[0].id
        assert layer.dismiss(msg_id) is True
        layer.update({})
        assert layer.needs_repaint is True

    def test_an_expired_message_asks_for_a_repaint(self, clock) -> None:
        """The dismiss timer is the only thing that moves a visible message."""
        layer = MessageLayer()
        layer.ensure_ready(_backend())
        layer.show("Hello", duration=5.0)
        layer.update({})
        layer.render()
        clock.advance(_PAST_SLIDE_IN)
        layer.update({})
        layer.render()
        assert layer.needs_repaint is False

        clock.advance(6.0)
        layer.update({})
        assert layer.needs_repaint is True

    def test_render_is_a_pure_function_of_state(self, clock) -> None:
        """Time passing WITHOUT an update() must not move the message.

        Regression: ``render()`` read ``time.monotonic()`` itself, so the painted
        position came from a clock no dirty flag could observe.
        """
        layer = MessageLayer()
        layer.ensure_ready(_backend())
        layer.show("Hello", duration=30.0)

        layer.update({})  # starts the slide-in
        clock.advance(0.1)  # 100 ms in: mid-slide, and alpha is above the cutoff
        layer.update({})

        before = _rects(layer.render())
        # Without this the test passes for the wrong reason: at the very start of
        # a slide-in alpha is 0, so render() emits nothing and two empty lists are
        # trivially equal whatever the clock says.
        assert before, "the message must actually be painting at this point"

        clock.advance(0.05)  # 50 ms passes with NO update() in between
        assert _rects(layer.render()) == before


class TestOverlayManagerRepaint:
    def test_no_pass_when_nothing_changed(self) -> None:
        manager = OverlayManager()
        manager.add_layer(_Layer(repaint=False))
        backend = _backend()

        manager.draw(backend)

        assert backend.present_overlay.call_count == 0

    def test_pass_when_a_layer_asks_for_one(self) -> None:
        manager = OverlayManager()
        manager.add_layer(_Layer(repaint=True))
        backend = _backend()

        manager.draw(backend)

        assert backend.present_overlay.call_count == 1

    def test_invisible_layers_are_ignored(self) -> None:
        manager = OverlayManager()
        layer = _Layer(repaint=True)
        layer.visible = False
        manager.add_layer(layer)
        assert manager.needs_repaint is False

        backend = _backend()
        manager.draw(backend)

        assert backend.present_overlay.call_count == 0

    def test_an_empty_overlay_is_still_presented(self) -> None:
        """A cleared overlay must reach the canvas, or it stays painted."""
        manager = OverlayManager()
        manager.add_layer(_Layer(repaint=True))
        backend = _backend()

        manager.draw(backend)

        assert backend.present_overlay.call_count == 1
        assert backend.present_overlay.call_args.args[0] == []

    def test_a_layer_that_raises_does_not_stop_the_pass(self) -> None:
        """An overlay must never be able to take down the slideshow."""

        class Exploding(_Layer):
            def render(self):
                raise RuntimeError("boom")

        manager = OverlayManager()
        manager.add_layer(Exploding(repaint=True))
        backend = _backend()

        manager.draw(backend)

        assert backend.present_overlay.call_count == 1
