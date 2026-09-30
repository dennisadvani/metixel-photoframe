# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Curtain layer — a fade to black that hides an imminent disruptive change.

Some operations visibly tear the picture: a rotation reconfigures the DRM output
underneath the running window, and a settings save restarts the services, so the
Qt window is destroyed and rebuilt.  In both cases the viewer sees a frame or two
of garbage — a stretched, sheared or half-composited image — before the new state
settles.

The cure is to get to a black screen *before* the disruptive step begins, and the
requirement is ordering, not decoration:

1. fade to black;
2. **only once the screen is completely black**, perform the operation.

So this layer owns no timing of its own beyond the fade.  It reports
:attr:`finished` when the fade has completed, and the caller waits for that
before acting — the fade is a precondition, not a parallel animation.

Why a curtain at the very top of the z-stack
--------------------------------------------
It sits at :data:`CurtainLayer.Z_CURTAIN` = ``-1.0`` — below zero, therefore in
front of every other layer including the boot screen (z=0.0), because in this
stack *lower z is closer to the camera*.  A curtain that only covered the
slideshow would be defeated by any on-screen message or the boot screen itself,
which is exactly the kind of content most likely to be up when a restart lands.

Honest limits
-------------
An application cannot cover a window it no longer owns.  Holding black as the
last frame means the compositor has black to show while the new process starts,
rather than the previous content or a partially torn surface — but the gap during
which nothing of ours is alive is not something this layer can paint over.  That
is a deliberate boundary, not an oversight: covering it needs a compositor-level
backdrop, which is out of scope here.
"""

from __future__ import annotations

import time
from typing import Any

from metixel.display.overlay_element import OverlayElement
from metixel.frontend.overlay.layer import OverlayLayer
from metixel.shared.timing import CURTAIN_FADE_SECONDS
from metixel.shared.timing import CURTAIN_SETTLE_SECONDS as _SHARED_SETTLE_SECONDS

#: How long the fade to black takes.
#: How long the fade to black takes.
#:
#: Re-exported from :mod:`metixel.shared.timing`, where it lives so the backend
#: can derive its settle delay from the same number without importing this
#: package.  The animation and every delay built on it therefore trace back to
#: one definition — see that module for why a duplicate is a silent failure.
FADE_SECONDS = CURTAIN_FADE_SECONDS

#: How long a caller on the other side of the IPC boundary must wait before
#: assuming the screen is black.
#:
#: Also from :mod:`metixel.shared.timing`; re-exported here so a reader of this
#: module finds the whole protocol — fade, hold, release, and how long the other
#: process waits — in one place.
CURTAIN_SETTLE_SECONDS = _SHARED_SETTLE_SECONDS


class CurtainLayer(OverlayLayer):
    """A full-screen fade to black, used to mask a disruptive change.

    Usage is a three-step handshake, and the middle step is the whole point::

        curtain.begin("Rotating display")
        while not curtain.finished:      # drive this from the render loop
            ...
        do_the_disruptive_thing()        # only now, with the screen black
        curtain.release()

    ``begin`` is idempotent while a fade is already running, so a second request
    arriving mid-fade extends the hold rather than restarting the ramp — which
    would otherwise make a repeated save flicker the screen back toward its
    content.
    """

    #: Layer name, used by ``OverlayManager.get_layer``.
    NAME = "curtain"

    #: Below every other layer, so the curtain is drawn closest to the camera.
    #:
    #: *Negative on purpose.*  The boot screen reserves ``0.0`` and the docstring
    #: on the base class lists it as "closest to camera", so anything that must
    #: outrank it has to be lower still.  Reusing ``0.0`` would tie with the boot
    #: screen, and a tie in this stack is not a defined order.
    Z_CURTAIN = -1.0

    def __init__(self, screen_w: int, screen_h: int) -> None:
        super().__init__(self.NAME, self.Z_CURTAIN)
        self._screen_w = screen_w
        self._screen_h = screen_h
        self._state = "idle"  # idle | fading | held
        self._started_at = 0.0
        self._alpha = 0.0
        self._reason = ""

    # -- Properties ----------------------------------------------------------

    @property
    def visible(self) -> bool:
        """Whether the curtain paints anything.

        Overridden rather than assigned: an ``idle`` curtain must not be drawn
        *and* must not keep the overlay asking for repaints, or the render loop
        would never idle.  The base class's ``needs_repaint`` defaults to True,
        so the way to stop a permanent repaint is to make the layer invisible —
        ``OverlayManager.needs_repaint`` only consults visible layers.
        """
        return self._state != "idle"

    @visible.setter
    def visible(self, value: bool) -> None:
        """Ignored — visibility is derived from the state machine.

        A settable ``visible`` would let a caller hide a curtain mid-fade and
        silently defeat the guarantee that the screen is black before the
        operation runs.
        """

    @property
    def is_held(self) -> bool:
        """Whether the fade has finished and the screen is being held black."""
        return self._state == "held"

    @property
    def finished(self) -> bool:
        """Whether the screen is completely black and the operation may proceed.

        The single question the caller asks.  It is deliberately the *hold*
        state and not "the fade is over": a fully opaque curtain is the
        precondition, so reporting finished at any partial alpha would let the
        operation start while content is still visible.
        """
        return self._state == "held"

    @property
    def alpha(self) -> float:
        """Current opacity, 0.0-1.0.  Exposed for tests and logging."""
        return self._alpha

    @property
    def reason(self) -> str:
        """Why the curtain is down, for the log."""
        return self._reason

    # -- Control -------------------------------------------------------------

    def begin(self, reason: str = "") -> None:
        """Start fading to black.

        Idempotent: a second call while fading does not restart the ramp, so a
        burst of requests (a save that triggers several restarts) produces one
        clean fade rather than a stutter that never completes.
        """
        self._reason = reason or self._reason
        if self._state == "fading":
            return
        if self._state == "held":
            return
        self._state = "fading"
        self._started_at = time.monotonic()
        self._alpha = 0.0

    def release(self) -> None:
        """Drop the curtain, animating back to the content.

        Deliberately instant rather than a reverse fade: this exists to avoid
        *garbage*, and the state after a restart is the fresh, correct frame.  A
        reverse fade would animate over pixels that changed identity underneath
        it (the rotation has happened by then), which reads as the same tearing
        the curtain was added to hide.
        """
        self._state = "idle"
        self._alpha = 0.0
        self._reason = ""

    def reset(self) -> None:
        """Return to idle without animating.  Used on startup."""
        self.release()

    def retarget(self, screen_w: int, screen_h: int) -> None:
        """Follow a screen-size change, so the curtain always covers the panel.

        A rotation swaps width and height.  A curtain laid out for the previous
        geometry would leave a strip of the frame uncovered at exactly the moment
        it is needed most.
        """
        self._screen_w = screen_w
        self._screen_h = screen_h

    # -- Layer interface -----------------------------------------------------

    def update(self, shared_state: dict | None = None) -> None:  # noqa: ARG002
        """Advance the fade.

        Driven from the render loop rather than a timer so the ramp is tied to
        frames that are actually being painted.  A wall-clock animation would
        reach "finished" while the compositor was stalled, and the operation
        would start on a screen that had never shown the black.
        """
        if self._state != "fading":
            return
        elapsed = time.monotonic() - self._started_at
        if elapsed >= FADE_SECONDS:
            self._alpha = 1.0
            self._state = "held"
            return
        self._alpha = elapsed / FADE_SECONDS

    def hold_black_now(self, reason: str = "") -> None:
        """Go fully opaque immediately, without fading.

        For teardown, where there is no longer a loop to animate a fade: the
        surface is about to stop being presented, so a ramp would be cut off
        part-way and read as a glitch of its own.  The guarantee that matters —
        the screen is black before the disruptive step — still holds, it is just
        reached without the animation.
        """
        self._reason = reason or self._reason
        self._state = "held"
        self._alpha = 1.0

    def render(self) -> list[OverlayElement]:
        """Return the curtain's single full-screen element.

        One opaque-ish rect, no text: anything drawn on top would be content the
        curtain exists to hide, and the reason string is for the journal.
        """
        if self._state == "idle":
            return []

        self.reset_z()
        return [
            OverlayElement.rect_element(
                (0, 0, self._screen_w, self._screen_h),
                "#000000",
                alpha=self._alpha,
                z=self.next_z(),
            )
        ]

    def draw(self, backend: Any) -> None:
        """Deprecated — the overlay manager composites via :meth:`render`.

        Required only to satisfy the layer interface.  It deliberately does not
        draw: issuing primitives is no longer possible against the reduced
        backend, and compositing here as well would paint the curtain twice.
        """
