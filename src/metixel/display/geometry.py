# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Pixel-rectangle conversions shared by the Qt backend and the frame canvas.

Deliberately Qt-free: ``qt_backend`` must stay importable on a machine with no
PySide6 (CI has none), so anything it and ``qt_canvas`` share cannot live in
either.  ``backend.py`` already upholds the same rule, and this module follows it.

Why a shared module for one function
------------------------------------
The mpv widget is a *sibling* of the frame canvas, not a child of it, and the
video only looks right when the widget's geometry EXACTLY covers the rectangle
the canvas leaves unpainted.  Two independent roundings of the same plan rect
would disagree by a pixel and show up as a black seam along the artwork edge —
a symptom that points at the paint code rather than at the arithmetic.  So the
convention lives here, once, and both callers use it.
"""

from __future__ import annotations

#: A pixel rectangle as ``(x, y, width, height)``.
IntRect = tuple[int, int, int, int]


def int_rect(rect: tuple[float, float, float, float]) -> IntRect:
    """Round a plan rectangle to exact integers.

    The origin floors and the far edge rounds **outward**.

    That asymmetry is the point.  ``QRegion`` is integer-only, and the regions
    built from these rects are used to *clip* painting: a region that stops a
    pixel short leaves an unwiped sliver of whatever was painted before, which is
    the visible seam this rounding exists to avoid.  Overshooting into the
    incoming artwork by a pixel is invisible.

    Width and height are clamped at zero so a degenerate rect (an annulus band of
    zero thickness, say) becomes empty rather than negative — ``QRect`` with a
    negative extent is not the empty rect, it is a different rectangle.
    """
    x, y, w, h = rect
    left, top = int(x), int(y)
    right = int(x + w + 0.9999)
    bottom = int(y + h + 0.9999)
    return (left, top, max(0, right - left), max(0, bottom - top))
