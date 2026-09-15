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

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from metixel.display.overlay_element import OverlayElement
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
        self._overlay: list[OverlayElement] = []
        # Artwork opacity, used by the crossfade.  Rings stay opaque.
        self._image_alpha: float = 1.0
        # Outgoing crossfade layer.  Held separately from the primary layer so a
        # single paintEvent can composite BOTH images at their complementary
        # alphas.  Two `update_plan()` + `update()` calls in one frame would
        # collapse into one paint (Qt coalesces update requests), leaving only
        # the last call stored — which is what made a crossfade fade to black
        # before the next slide appeared instead of blending into it.
        self._prev_plan: RenderPlan | None = None
        self._prev_image: QImage | None = None
        self._prev_alpha: float = 0.0
        # When True, the layer inside the Mat Window is left UNPAINTED so a
        # sibling widget underneath (the mpv surface) shows through.  That is the
        # whole "virtual mat over live video" mechanism: this canvas paints the
        # ring layers opaquely and leaves the middle transparent.
        self._video_underlay: bool = False
        # Notified with (width, height) whenever the surface resizes, so the
        # backend can track the real display size.  See set_resize_callback.
        self._resize_callback: Any = None
        # True while a video plays: paint ONLY the overlay, on transparency.
        # See set_overlay_only for why the paint attributes matter.
        self._overlay_only: bool = False
        # The canvas paints every pixel of itself, so Qt can skip the erase pass.
        #
        # Do NOT reintroduce a mode where part of the canvas is left unpainted to
        # let a video show through.  That was tried: leaving a hole while Qt still
        # believed the widget was opaque produced a solid black rectangle over the
        # video, because the unpainted region showed uninitialised framebuffer.
        # The video widget now paints its own matte instead
        # (see MpvRenderWidget._paint_matte_over_video), so this canvas is only
        # ever used for photos, overlays, and the boot screen.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    # -- Public API ----------------------------------------------------------

    def set_overlay_only(self, enabled: bool) -> None:
        """Switch to painting ONLY the overlay, on a transparent background.

        Used while a video plays: the mpv widget underneath owns the frame and
        the matte, so this canvas must contribute nothing but the overlay (clock,
        messages) — otherwise its opaque background covers the video, which is
        what produced a solid black centre.

        The load-bearing part is ``WA_OpaquePaintEvent``.  It is a CONTRACT with
        Qt meaning "this widget paints every pixel of itself".  When it is set and
        the widget paints nothing in a region, Qt does not fall back to showing
        what is underneath — the region shows uninitialised framebuffer, i.e.
        black.  So the attribute must be cleared for the duration of overlay-only
        mode, and the widget must also be told not to erase to black.

        The z-order is raised here, and only here, because the mode is exactly
        what decides it: overlay-only means a video is underneath and the canvas
        must sit above it; leaving the mode means the canvas is the only surface
        again.  Asking for that order from ``present()`` and ``present_overlay()``
        instead meant a ``raise_()`` every frame — a restack request to the
        compositor, 31 times a second, for an order that was already in place.
        """
        if self._overlay_only == enabled:
            return
        self._overlay_only = enabled
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, not enabled)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, enabled)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, enabled)
        self.raise_()
        self.update()
        logger.debug("Canvas overlay-only mode: %s", "on" if enabled else "off")

    def set_resize_callback(self, callback: Any) -> None:
        """Register a callable invoked with ``(width, height)`` on resize.

        The canvas is a ``QWidget``, so it can legitimately receive resize events;
        the backend is a plain Python object and cannot use ``installEventFilter``
        (which requires a ``QObject``).  Routing through the canvas is therefore
        the correct way to observe the surface size from the backend.
        """
        self._resize_callback = callback

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        callback = self._resize_callback
        if callback is None:
            return
        try:
            callback(int(self.width()), int(self.height()))
        except Exception:
            # A callback failure must never break painting or the event loop.
            logger.debug("resize callback failed", exc_info=True)

    def update_plan(self, plan: RenderPlan, image: Any = None, alpha: float = 1.0) -> None:
        """Store the plan (and optional artwork) for the next repaint.

        ``alpha`` applies to the artwork only — the frame rings always paint
        opaque, so a fading photo never reveals the matte behind it.

        Any pending outgoing crossfade layer is dropped: this is the single-layer
        entry point, and leaving a stale outgoing image behind would make the
        next ordinary slide paint a ghost of the previous one.

        Repaints only if this is genuinely a different picture — see
        :meth:`_store_layers`.  A static slide calls this every tick with the
        same plan and the same image, and must therefore cost nothing.
        """
        self._store_layers(
            plan,
            image if isinstance(image, QImage) else None,
            max(0.0, min(1.0, alpha)),
            None,
            None,
            0.0,
        )

    def update_transition(
        self,
        plan: RenderPlan,
        image: Any,
        alpha: float,
        prev_plan: RenderPlan | None,
        prev_image: Any,
        prev_alpha: float,
    ) -> None:
        """Store BOTH crossfade layers so one repaint composites them together.

        This is what actually implements a crossfade.  The two images are painted
        into the same frame at their complementary alphas — outgoing first, then
        incoming on top — so the result is the two photos mixing, not the incoming
        one fading up over the background (which reads as "fade to black, then
        the next slide appears").

        Both alphas come from :class:`~metixel.frontend.presentation.transitions.
        TransitionEngine`, so the easing curves stay in one place.

        The incoming alpha moves every tick of a transition, so this repaints
        throughout — which is the point.  Between transitions, with both layers
        and both alphas unchanged, it does not.
        """
        self._store_layers(
            plan,
            image if isinstance(image, QImage) else None,
            max(0.0, min(1.0, alpha)),
            prev_plan,
            prev_image if isinstance(prev_image, QImage) else None,
            max(0.0, min(1.0, prev_alpha)),
        )

    def clear_plan(self) -> None:
        """Drop the current plan so the next paint is a bare background."""
        self._store_layers(None, None, 1.0, None, None, 0.0)

    def _store_layers(
        self,
        plan: RenderPlan | None,
        image: QImage | None,
        alpha: float,
        prev_plan: RenderPlan | None,
        prev_image: QImage | None,
        prev_alpha: float,
    ) -> None:
        """Store the layers to composite, repainting ONLY if that changed.

        This is the whole idle-rendering mechanism, and it lives here because
        this class is the only thing that knows what was last painted.  Qt's
        contract for a custom widget is exactly this: ``paintEvent`` paints
        whatever is stored, and only the code that changes what is stored may ask
        for a repaint.  Qt cannot make that judgement itself — it has no way to
        know what a ``paintEvent`` draws — so ``update()`` is an explicit request,
        never something Qt does on its own.

        Skipping the repaint is what makes a static slide free.  The presenter
        calls ``present()`` every tick with the SAME cached plan and the SAME
        cached image handle, so identity (``is``) is both an exact and a free
        test.  Equality would be neither: comparing ``QImage`` values walks every
        pixel.

        Measured on a Pi 5: asking Qt to composite an unchanging 1920x1200 frame
        31 times a second was **83% of a core**, while Qt's own event loop used
        **0.9%** to run 178 timer ticks and paint once.  The overhead was
        entirely self-inflicted.
        """
        if (
            plan is self._plan
            and image is self._image
            and alpha == self._image_alpha
            and prev_plan is self._prev_plan
            and prev_image is self._prev_image
            and prev_alpha == self._prev_alpha
        ):
            return
        self._plan = plan
        self._image = image
        self._image_alpha = alpha
        self._prev_plan = prev_plan
        self._prev_image = prev_image
        self._prev_alpha = prev_alpha
        self.update()

    def set_background(self, color: tuple[float, float, float, float]) -> None:
        """Set the canvas clear colour."""
        r, g, b = (int(max(0.0, min(1.0, c)) * 255) for c in color[:3])
        background = QColor(r, g, b)
        if background == self._background:
            return
        self._background = background
        self.update()

    def update_overlay(self, elements: list[OverlayElement]) -> None:
        """Store the overlay elements for the next repaint.

        The list arrives already flattened and sorted (largest ``z`` first) from
        the overlay manager, so the canvas only has to paint it in order.

        The reference is kept rather than copied, and compared by identity, so
        the canvas can tell whether the overlay changed without comparing element
        values — :class:`OverlayElement` carries image handles, and comparing
        those compares every pixel.  **The caller must not mutate the list after
        handing it over.**  The overlay manager rebuilds a fresh list whenever a
        layer reports a change and passes the identical object when none has,
        which is what makes the comparison meaningful.
        """
        if elements is self._overlay:
            return
        self._overlay = elements
        self.update()

    def clear_overlay(self) -> None:
        """Drop the overlay so the next frame paints only the slideshow."""
        if self._overlay:
            self.update_overlay([])

    # -- Painting ------------------------------------------------------------

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        try:
            if self._overlay_only:
                # Video plays underneath and owns the frame and the matte.
                # Paint nothing but the overlay: no fill, no artwork, no rings.
                # The widget is non-opaque in this mode, so the unpainted area
                # is genuinely transparent rather than black.
                for element in self._overlay:
                    self._draw_element(painter, element)
                return

            # The canvas always paints every pixel here (WA_OpaquePaintEvent
            # holds), so Qt can skip the erase pass.
            painter.fillRect(self.rect(), self._background)
            plan = self._plan
            if plan is not None:
                # 1. Ambient fill — the only full-rectangle layer.  Absent
                #    whenever a mat ring exists, because the Mat Window is then
                #    cut to the artwork and no residue is left for fill.
                if plan.ambient is not None:
                    self._fill(painter, plan.ambient, _qcolor(plan.ambient_colour))

                # 2. Outgoing crossfade layer, then the incoming artwork.
                #
                #    Order is load-bearing: the outgoing image paints first, at
                #    its fading alpha, and the incoming one composites on top at
                #    its rising alpha.  Painting only the incoming layer (the old
                #    behaviour, because two present() calls collapsed into one
                #    repaint) faded the new photo up from the background, which is
                #    what looked like "fade to black before the next slide".
                #
                #    The outgoing layer uses ITS OWN plan so a different aspect
                #    ratio is not drawn with the incoming item's geometry.
                if self._prev_plan is not None and self._prev_alpha > 0.01:
                    painter.setOpacity(self._prev_alpha)
                    try:
                        self._draw_artwork(painter, self._prev_plan, self._prev_image)
                    finally:
                        painter.setOpacity(1.0)

                if self._image is not None and self._image_alpha > 0.01:
                    painter.setOpacity(self._image_alpha)
                    try:
                        self._draw_artwork(painter, plan)
                    finally:
                        painter.setOpacity(1.0)

                # 3–5. Ring layers, outermost last so the moulding reads as the
                #      frame edge.  Annuli: disjoint from the artwork.
                for rect in plan.whitespace:
                    self._fill(painter, rect, _qcolor(plan.whitespace_colour))
                for rect in plan.matte:
                    self._fill(painter, rect, _qcolor(plan.matte_colour))
                for rect in plan.moulding:
                    self._fill(painter, rect, QColor(0, 0, 0))

            # Overlay last: boot screen, messages, widgets all paint above the
            # slideshow.  Already z-sorted by the manager.
            for element in self._overlay:
                self._draw_element(painter, element)
        except Exception:
            # A paint error must never propagate into Qt's event loop, where it
            # would be swallowed and leave a blank window with no clue why.
            logger.exception("FrameCanvas paint failed")
        finally:
            painter.end()

    def _draw_element(self, painter: QPainter, element: OverlayElement) -> None:
        """Paint one overlay element.

        Dispatches on ``kind`` rather than probing attributes, so a malformed
        element is impossible: the dataclass validates at construction.
        """
        if element.alpha <= 0.01:
            return

        painter.setOpacity(element.alpha)
        try:
            if element.kind == "rect":
                x, y, w, h = element.rect
                if w > 0 and h > 0:
                    painter.fillRect(QRectF(x, y, w, h), _qcolor(element.colour))

            elif element.kind == "image":
                if not isinstance(element.image, QImage):
                    return
                x, y, w, h = element.rect
                if w <= 0 or h <= 0:
                    return
                target = QRectF(x, y, w, h)
                source = QRectF(0, 0, element.image.width(), element.image.height())
                if element.rotation:
                    # Rotate about the element's centre — the boot spinner is the
                    # only user of this, and it must spin in place.
                    painter.save()
                    painter.translate(x + w / 2.0, y + h / 2.0)
                    painter.rotate(element.rotation)
                    painter.translate(-(x + w / 2.0), -(y + h / 2.0))
                    painter.drawImage(target, element.image, source)
                    painter.restore()
                else:
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                    painter.drawImage(target, element.image, source)

            elif element.kind == "text":
                x, y, _w, _h = element.rect
                font = painter.font()
                font.setPointSize(element.size)
                painter.setFont(font)
                painter.setPen(_qcolor(element.colour))
                # Baseline offset ≈ 0.8em so text sits where the old
                # draw_text(x, y) anchored it, keeping widget layout unchanged.
                painter.drawText(QPointF(x, y + element.size * 0.8), element.text)
        finally:
            painter.setOpacity(1.0)

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

    def _draw_artwork(
        self, painter: QPainter, plan: RenderPlan, image: QImage | None = None
    ) -> None:
        """Blit the artwork through the plan's source→destination mapping.

        Honouring ``artwork_src`` is what implements ``overflow="crop"``: the
        covering region of the source is drawn into the Mat Window, discarding
        the parts outside it.  Qt does the scaling, which keeps this identical
        in behaviour to the Tk backend without sharing pixel code.

        ``image`` defaults to the primary layer's artwork; the crossfade's
        outgoing layer passes its own, so both frames are drawn with the geometry
        each was actually laid out for.
        """
        source_image = self._image if image is None else image
        if source_image is None:
            return
        sx, sy, sw, sh = plan.artwork_src
        dx, dy, dw, dh = plan.artwork_dst
        if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0:
            return

        source = QRectF(sx, sy, sw, sh)
        target = QRectF(dx, dy, dw, dh)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(target, source_image, source)
