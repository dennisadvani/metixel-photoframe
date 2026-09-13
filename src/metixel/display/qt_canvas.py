# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Frame canvas — paints one :class:`RenderPlan` per frame with ``QPainter``.

This is the whole rendering surface: a single ``QWidget`` that composites the
framing specification's layers in order,

    ambient fill -> artwork -> whitespace -> mat -> moulding,

and nothing else.  There is no second framebuffer, no shader, and no depth
buffer, because the plan's ring layers are *disjoint from the artwork* — the
framing engine guarantees that, and it is what makes a flat paint order correct.

Why a canvas rather than widgets-per-layer
------------------------------------------
The overlay layers (boot screen, messages) need to composite *over* a playing
video, and the matte must sit over mpv's output too.  A single widget that
paints in a known order makes that trivial; a tree of child widgets would put
each layer in its own native surface and make the video path depend on Qt's
composition order instead.

Video compatibility
-------------------
``update_plan(plan, image=None)`` is the video case: the artwork layer is
skipped so mpv's frames (rendered by a sibling widget underneath) show through
the Mat Window, while the matte ring is still painted on top.  That is the whole
trick behind "virtual mat over live video" — see the module docstring's warning
against the ``glBlitFramebuffer`` alternative, which segfaults on the Pi.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from metixel.framing.layout import RenderPlan

logger = logging.getLogger(__name__)


def _qcolor(spec: str) -> QColor:
    """Build a ``QColor`` from a plan colour.

    The plan reports ``#rrggbb`` (the templates' own vocabulary).  An
    unparseable value falls back to mid-grey rather than transparent: a mat band
    that silently disappears looks like a geometry bug, whereas a grey band
    points at the colour.
    """
    colour = QColor(spec)
    if not colour.isValid():
        logger.debug("Unparseable plan colour %r — using grey", spec)
        return QColor(128, 128, 128)
    return colour


class FrameCanvas(QWidget):
    """Paints a :class:`RenderPlan` and optionally its artwork."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plan: RenderPlan | None = None
        self._image: QImage | None = None
        self._background = QColor(0, 0, 0)
        # The canvas fully repaints every frame, so Qt does not need to erase
        # first — and skipping the erase avoids a visible flash on the video
        # path where the widget beneath is showing through.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    # -- Public API ----------------------------------------------------------

    def update_plan(self, plan: RenderPlan, image: Any = None) -> None:
        """Store the plan (and optional artwork) for the next repaint."""
        self._plan = plan
        self._image = image if isinstance(image, QImage) else None

    def clear_plan(self) -> None:
        """Drop the current plan so the next paint is a bare background."""
        self._plan = None
        self._image = None

    def set_background(self, color: tuple[float, float, float, float]) -> None:
        """Set the canvas clear colour."""
        r, g, b = (int(max(0.0, min(1.0, c)) * 255) for c in color[:3])
        self._background = QColor(r, g, b)

    # -- Painting ------------------------------------------------------------

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), self._background)
            plan = self._plan
            if plan is None:
                return

            # 1. Ambient fill — the only full-rectangle layer.  Absent whenever a
            #    mat ring exists, because the Mat Window is then cut to the
            #    artwork and no residue is left for fill to occupy.
            if plan.ambient is not None:
                self._fill(painter, plan.ambient, _qcolor(plan.ambient_colour))

            # 2. Artwork.  Skipped for video (image is None), which is what lets
            #    mpv's frames show through the Mat Window.
            if self._image is not None:
                self._draw_artwork(painter, plan)

            # 3–5. The ring layers, outermost last so the moulding reads as the
            #      frame edge.  These are annuli: disjoint from the artwork.
            for rect in plan.whitespace:
                self._fill(painter, rect, _qcolor(plan.whitespace_colour))
            for rect in plan.matte:
                self._fill(painter, rect, _qcolor(plan.matte_colour))
            for rect in plan.moulding:
                self._fill(painter, rect, QColor(0, 0, 0))
        except Exception:
            # A paint error must never propagate into Qt's event loop, where it
            # would be swallowed and leave a blank window with no clue why.
            logger.exception("FrameCanvas paint failed")
        finally:
            painter.end()

    def _fill(
        self,
        painter: QPainter,
        rect: tuple[float, float, float, float],
        colour: QColor,
    ) -> None:
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            return
        painter.fillRect(QRectF(x, y, w, h), colour)

    def _draw_artwork(self, painter: QPainter, plan: RenderPlan) -> None:
        """Blit the artwork through the plan's source→destination mapping.

        Honouring ``artwork_src`` is what implements ``overflow="crop"``: the
        covering region of the source is drawn into the Mat Window, discarding
        the parts outside it.  Qt does the scaling, which keeps this identical
        in behaviour to the Tk backend without sharing pixel code.
        """
        assert self._image is not None
        sx, sy, sw, sh = plan.artwork_src
        dx, dy, dw, dh = plan.artwork_dst
        if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0:
            return

        source = QRectF(sx, sy, sw, sh)
        target = QRectF(dx, dy, dw, dh)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(target, self._image, source)
