# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

"""Timing shared by the two processes, where the number must not drift.

Two sides of the app have to agree on how long a visual transition takes, but
they run in **different processes** and cannot observe each other's state:

* the frontend owns the curtain — it fades the screen to black and knows, to the
  frame, when that is done;
* the backend owns the action the curtain is hiding (restarting the services,
  rebooting), and it can only *wait*.

That makes a duplicated constant a genuine hazard rather than a tidiness issue.
The backend is not asking "is it black yet?"; it is betting that enough time has
passed.  If its copy of the duration drifts below the frontend's, the action
lands part-way through the fade and shows exactly the tearing the curtain exists
to hide — and it fails *silently*, because a delay that is too short is
indistinguishable from a delay that worked.  So the number lives here, once, in
a module both sides may import.
"""

from __future__ import annotations

#: How long the curtain takes to reach full black.
#:
#: This is the frontend's animation duration, stated here rather than in the
#: curtain module only so the backend can derive its wait from it without
#: importing the frontend package — which would invert the intended dependency
#: direction and pull the whole overlay stack into the backend's import graph for
#: the sake of one float.  ``metixel.frontend.overlay.curtain_layer`` re-exports
#: it as :data:`~metixel.frontend.overlay.curtain_layer.FADE_SECONDS`, so the
#: animation and every delay below it still trace back to one definition.
#:
#: Short enough to read as "the frame is going dark for a reason" rather than as
#: a hang, long enough that the transition is not itself a jarring cut.
CURTAIN_FADE_SECONDS = 0.5

#: How long the backend must wait before assuming the screen is black.
#:
#: The margin covers the two things the frontend's own clock cannot: the datagram
#: has to be delivered and picked up on the next tick (up to one frame), and the
#: fade is driven by the render loop, so a stalled or throttled tick stretches it
#: in wall-clock terms.  Two seconds of headroom on a half-second fade is
#: deliberate — it is cheap, and it is the difference between "always covered"
#: and "usually covered".
CURTAIN_SETTLE_SECONDS = CURTAIN_FADE_SECONDS + 2.0
