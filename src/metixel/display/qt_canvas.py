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
A playing video is the artwork layer: mpv draws it in a sibling widget underneath
and this canvas leaves exactly that rectangle unpainted, so the frames show
through it while the ambient, the ring layers and the overlay are painted around
it.  :meth:`FrameCanvas.set_video_surface` owns that mode.

This is the mechanism an earlier attempt got wrong, so the constraint is worth
stating plainly: leaving part of a widget unpainted while it still claims
``WA_OpaquePaintEvent`` yields undefined framebuffer content, which Qt rendered
as a solid black rectangle.  The fix is the paint attribute, not a different
architecture — ``set_video_surface`` clears it.

A PHASE-0 spike confirmed this on hardware (Pi 5, cage/Wayland, Qt 6.8.2): a
partial hole renders correctly over a raster sibling, over a ``QOpenGLWidget``,
and over the real ``MpvRenderWidget`` while playing.  See
``scripts/dev/_spike_video_hole.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import QWidget

from metixel.display.ambient_blur import BackdropRequest, BackdropRunner
from metixel.display.geometry import int_rect
from metixel.display.overlay_element import OverlayElement
from metixel.framing.layout import RenderPlan

logger = logging.getLogger(__name__)


def _qr(rect: tuple[float, float, float, float]) -> QRect:
    """Return :func:`int_rect`'s result as a ``QRect``.

    ``QRegion`` and ``QImage.copy`` need Qt types, so a conversion has to exist
    somewhere.  Keeping it in one adaptor means the ROUNDING RULE still lives
    exactly once, in :mod:`metixel.display.geometry` — which matters because the
    mpv widget is positioned from the same rect and a disagreement of one pixel
    shows up as a black seam at the artwork edge.
    """
    return QRect(*int_rect(rect))


@dataclass(frozen=True, eq=False)
class _BackdropSlot:
    """One adopted blurred backdrop, and what it was built for.

    ``request`` is the backdrop's identity (source file, its fingerprint, the
    target size and the radius), so readiness can be answered without the
    artwork handle.  ``handle`` is the decoded artwork this backdrop belongs to,
    which is how ``paintEvent`` finds the pixmap for the layer it is drawing.

    ``eq=False`` on purpose: the slots are compared by the fields the caller
    means — ``request`` for readiness, ``handle`` by identity for painting — and
    never as whole values.
    """

    request: BackdropRequest
    handle: Any
    pixmap: QPixmap


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
        # Pre-scaled artwork, one per crossfade layer.
        #
        # A crossfade changes exactly ONE quantity per frame — the alpha — while
        # ``artwork_src``/``artwork_dst`` and the source pixels are constant for
        # its whole duration.  Drawing the artwork with ``drawImage(target,
        # image, source)`` and a non-identity scale re-runs a bilinear resample
        # EVERY frame, twice (both layers), which is the transition's dominant
        # cost and the reason a crossfade burned far more CPU than the retired
        # pi3d path, where the GPU's texture sampler did the scaling for free.
        #
        # Qt's raster engine has no equivalent "sample a texture at an opacity":
        # ``setOpacity`` scales the result of the draw, so a scaled draw still
        # pays for the scaling.  The only way to make the per-frame cost
        # proportional to the DESTINATION rather than the source is to scale
        # once into a destination-sized pixmap and then blit it 1:1.
        #
        # Keyed by the (image, plan) pair that produced it.  Identity — not
        # equality — is the test, because comparing a QImage walks every pixel.
        # Rebuilding on an identity change means a fit-mode switch, a rotation,
        # a config reload or a new slide all invalidate automatically, with no
        # explicit invalidation call that could be missed.
        #
        # Memory: a destination-sized pixmap is ~4 bytes per pixel (scale
        # 1581x1185 -> ~7.5 MB), and there are at most two live (current +
        # outgoing).  Bounded by construction and dropped when its layer is
        # dropped, so it cannot grow across a long run.
        self._scaled_pixmap: QPixmap | None = None
        self._scaled_key: tuple[Any, Any, Any] | None = None
        #: The image the cached pre-scale was built from, so a changed image can
        #: drop its pixmap without comparing pixels.  See ``_store_layers``.
        self._scaled_image: QImage | None = None
        self._prev_scaled_pixmap: QPixmap | None = None
        self._prev_scaled_key: tuple[Any, Any, Any] | None = None
        self._prev_scaled_image: QImage | None = None
        # Full-bleed blurred backdrops, for ``ambient_strategy == "blur"``.
        #
        # Two slots at most, because a crossfade has two layers in flight and
        # each layer needs ITS OWN backdrop: the incoming photo must be
        # surrounded by its own blur, never the outgoing one's.  Memory is
        # therefore bounded at two full-screen pixmaps (~9 MB each at 1920x1200)
        # with no release bookkeeping — a slot is simply reused when a third
        # backdrop arrives, which cannot happen before the transition holding
        # the second one has ended.
        #
        # Darkening is NOT baked in.  It is applied at paint time as a
        # translucent black overlay, so moving the brightness slider costs one
        # fillRect rather than a full rebuild.
        self._backdrop: _BackdropSlot | None = None
        self._prev_backdrop: _BackdropSlot | None = None
        # Backdrops are built in a throttled SUBPROCESS, never on a thread here:
        # ``nice`` and ``cpulimit`` are process-level, and the Pillow round trip
        # is exactly what used to blow the frame budget mid-crossfade.  The
        # runner is created on first use so a canvas that never shows a blur
        # never spawns anything.
        self._backdrop_runner: BackdropRunner | None = None
        #: The backdrop being built, and the artwork handle it belongs to.  The
        #: handle is required because the finished pixmap is adopted into the
        #: slot that will be painted for that artwork.
        self._backdrop_pending: BackdropRequest | None = None
        self._backdrop_pending_handle: Any = None
        #: Job ids that failed or timed out.  They are not retried, so a
        #: backdrop that cannot be built degrades to the flat fill instead of
        #: holding every slide that shows it.
        self._backdrop_failed: set[str] = set()
        # When True, the artwork rect is left UNPAINTED so the mpv surface below
        # shows through it: the video IS the artwork layer, and this canvas paints
        # the ambient, the rings and the overlay around it.  That is the whole
        # "virtual mat over live video" mechanism.  See set_video_surface.
        self._video_surface: bool = False
        # Notified with (width, height) whenever the surface resizes, so the
        # backend can track the real display size.  See set_resize_callback.
        self._resize_callback: Any = None
        # The canvas paints every pixel of itself, so Qt can skip the erase pass.
        # Cleared for the duration of video-surface mode, where it deliberately
        # does not — see set_video_surface.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    # -- Public API ----------------------------------------------------------

    def set_video_surface(self, enabled: bool) -> None:
        """Show a video through the artwork rect, or go back to painting photos.

        Enabled, the canvas paints everything it normally does for the current
        plan — background, ambient, blurred backdrop, ring layers and overlay —
        EXCEPT the artwork, which is left unpainted so the sibling mpv widget
        underneath shows through exactly there.  The plan and its artwork handle
        are still set via :meth:`update_plan`: the plan supplies the hole's
        geometry and the handle is what the ambient backdrop is keyed to, so a
        video keeps the SAME still blurred surround a photo of it would have.

        The load-bearing part is ``WA_OpaquePaintEvent``.  It is a CONTRACT with
        Qt meaning "this widget paints every pixel of itself".  When it is set and
        the widget leaves a region unpainted, Qt does not fall back to showing
        what is underneath — the region shows uninitialised framebuffer, i.e.
        black.  That was the black rectangle in the earlier attempt, and it is why
        the attribute is cleared here and ``WA_TranslucentBackground`` set in its
        place.

        Z-order is owned here, and only here, because the mode is exactly what
        decides it: a video underneath means this canvas must sit above it to
        paint the rings; leaving the mode means the canvas is the only surface
        again.  Asking for that order from ``present()`` instead meant a
        ``raise_()`` every frame — a restack request to the compositor, 31 times a
        second, for an order that was already in place.
        """
        if self._video_surface == enabled:
            return
        self._video_surface = enabled
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, not enabled)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, enabled)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, enabled)
        self.raise_()
        self.update()
        logger.debug("Canvas video-surface mode: %s", "on" if enabled else "off")

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

        # Drop a cached pre-scale whose layer has gone, so the pixmaps cannot
        # outlive the images they were made from.  Bounded at two by
        # construction, but a dropped layer would otherwise pin its pixmap
        # (~7.5 MB each) for the rest of the run, which on a 512 MB device is the
        # slow-OOM failure mode the image cache is capped to avoid.
        if image is not self._scaled_image:
            self._scaled_pixmap = None
            self._scaled_key = None
            self._scaled_image = image
        if prev_image is not self._prev_scaled_image:
            self._prev_scaled_pixmap = None
            self._prev_scaled_key = None
            self._prev_scaled_image = prev_image

        # Blurred backdrops need no release pass here: there are at most two
        # slots, and ``_store_backdrop`` reuses the one that is not on screen.
        # Pruning by image identity, as an earlier version did, is what made two
        # full-screen pixmaps look like they could be pinned for the run — they
        # cannot, because a backdrop is only ever held for a layer that is
        # showing or about to.
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

    def backdrop_request(self, plan: RenderPlan, source: Path | None) -> BackdropRequest | None:
        """The identity of *plan*'s backdrop, or ``None`` when there is none.

        The ONE place a request is derived, so the readiness check, the warm
        request and the adoption can never disagree about what a backdrop is — a
        disagreement there would hold every slide forever.
        """
        _, _, width, height = int_rect(plan.screen)
        return BackdropRequest.build(
            source,
            width,
            height,
            float(plan.ambient_blur_radius),
            str(plan.ambient_blur_filter),
        )

    def backdrop_ready(self, plan: RenderPlan, source: Path | None) -> bool:
        """Whether *plan*'s backdrop is in the buffer and usable.

        The presenter asks this before starting a transition, so a slide is held
        until its backdrop is LOADED rather than a crossfade beginning against a
        backdrop that is not there yet.

        Cheap by design — an equality test, no pixel work and no disk — because
        it is called every frame while a slide is being held.

        A non-blur plan is always ready, and so is one whose backdrop cannot be
        built at all: there is nothing to wait for, and holding would be a
        permanent stall rather than a short delay.
        """
        if plan.ambient_strategy != "blur":
            return True
        request = self.backdrop_request(plan, source)
        if request is None or request.job_id in self._backdrop_failed:
            return True
        return self._loaded_backdrop(request) is not None

    def _loaded_backdrop(self, request: BackdropRequest) -> QPixmap | None:
        """The adopted pixmap for *request*, or ``None`` if it is not loaded."""
        for slot in (self._backdrop, self._prev_backdrop):
            if slot is not None and slot.request == request:
                return slot.pixmap
        return None

    def _backdrop_for(self, handle: Any) -> QPixmap | None:
        """The adopted pixmap belonging to artwork *handle*.

        Looked up by handle IDENTITY, never by ``==``: ``QImage`` equality
        compares every pixel, and a paint path that did that would be slower than
        the blur it is trying to avoid.
        """
        if handle is None:
            return None
        if self._backdrop is not None and self._backdrop.handle is handle:
            return self._backdrop.pixmap
        if self._prev_backdrop is not None and self._prev_backdrop.handle is handle:
            return self._prev_backdrop.pixmap
        return None

    def warm_backdrop(self, plan: RenderPlan, source: Path | None, handle: Any = None) -> None:
        """Start building *plan*'s backdrop in a throttled subprocess.

        Called one slide AHEAD of the item that needs it, so the work happens in
        the idle time the current slide provides.  It is never started inside the
        render loop's critical path, and the presenter refuses to start one while
        a transition is on screen — the child is CPU-hungry even when capped, and
        a crossfade is the one moment where a stolen cycle is visible.

        Non-blocking and idempotent: a request that is already loaded or already
        in flight is ignored, and a newer request supersedes an older one, exactly
        as :class:`ImageCache` does.

        *handle* is the decoded artwork this backdrop belongs to, and is only
        used to place the result in the slot that will be painted for it.
        """
        if plan.ambient_strategy != "blur":
            return
        request = self.backdrop_request(plan, source)
        if request is None or request.job_id in self._backdrop_failed:
            return
        if self._loaded_backdrop(request) is not None:
            return
        if self._backdrop_pending is not None and self._backdrop_pending == request:
            return  # already in flight

        if self._backdrop_runner is None:
            self._backdrop_runner = BackdropRunner()
        self._backdrop_pending = request
        self._backdrop_pending_handle = handle
        self._backdrop_runner.start(request)

    def collect_warm_backdrop(self) -> bool:
        """Adopt a finished backdrop on the GUI thread.

        Returns ``True`` when the buffer changed and a repaint is worthwhile.

        Qt objects are created HERE, never in the child: ``QPixmap`` is a
        GUI-thread resource.  Reading the finished file back is a decode of one
        JPEG, which is why the caller must not run this mid-crossfade — the
        presenter's "not during a transition" gate covers this call too, not just
        the spawn.

        A job that failed or timed out is recorded so the slide it belongs to is
        never held for it: it falls back to the flat ambient fill.
        """
        runner = self._backdrop_runner
        if runner is None:
            return False
        finished = runner.take_finished()
        if finished is None:
            return False

        job_id, path = finished
        request = self._backdrop_pending
        handle = self._backdrop_pending_handle
        self._backdrop_pending = None
        self._backdrop_pending_handle = None

        if path is None or request is None or request.job_id != job_id:
            runner.release(job_id)
            self._backdrop_failed.add(job_id)
            return False

        pixmap = QPixmap(str(path))
        runner.release(job_id)
        if pixmap.isNull():
            logger.debug("Ambient backdrop %s was unreadable", path)
            self._backdrop_failed.add(job_id)
            return False

        self._store_backdrop(request, handle, pixmap)
        self.update()
        return True

    def _store_backdrop(self, request: BackdropRequest, handle: Any, pixmap: QPixmap) -> None:
        """Adopt a finished backdrop into one of the two slots.

        The slot is chosen by what is on screen, never by searching for a key: a
        slot already belonging to this handle, else a free one, else the one that
        is NOT currently painted.  That keeps two full-screen pixmaps as the hard
        bound without any release bookkeeping, because the only way to need a
        third backdrop is after the transition holding the second has ended.

        Note that a backdrop prepared for the NEXT item is adopted while that item
        is not yet on screen — which is why the caller supplies the handle rather
        than the canvas inferring it from ``self._image``.
        """
        slot = _BackdropSlot(request, handle, pixmap)
        if self._backdrop is not None and self._backdrop.handle is handle:
            self._backdrop = slot
        elif self._prev_backdrop is not None and self._prev_backdrop.handle is handle:
            self._prev_backdrop = slot
        elif self._backdrop is None or not self._is_live(self._backdrop.handle):
            self._backdrop = slot
        else:
            self._prev_backdrop = slot
        if len(self._backdrop_failed) > 64:
            # Bounded: failures are rare, and clearing only means a retry.
            self._backdrop_failed.clear()

    def _is_live(self, handle: Any) -> bool:
        """Whether *handle* is painted by a layer that is still on screen."""
        return handle is not None and (handle is self._image or handle is self._prev_image)

    def close_backdrops(self) -> None:
        """Stop any backdrop in flight.  Called when the frontend shuts down."""
        if self._backdrop_runner is not None:
            self._backdrop_runner.close()
            self._backdrop_runner = None
        self._backdrop_pending = None
        self._backdrop_pending_handle = None

    def _paint_transition_curtain(self, painter: QPainter, plan: RenderPlan, alpha: float) -> None:
        """Wipe the outgoing item's exposed residue at the incoming item's alpha.

        Only ``contain`` needs this, and the reason is geometric.  The two items
        are laid out independently, so in ``contain`` their artworks occupy
        different rectangles.  The crossfade draws the outgoing layer once, over
        the whole of ITS rect, at full opacity — because in the overlap it must
        stay opaque or the blend double-counts the transparency and the panel
        dims through the middle.  But that leaves the part of the outgoing artwork
        the incoming one never reaches (its letterbox bars) sitting at a rock
        steady 100% for the whole transition, and then ``_advance()`` drops the
        outgoing layer entirely, so the residue SNAPS away in a single frame.
        That snap is what breaks the effect.

        A plain colour rect sharing the incoming item's alpha fixes it, because
        covering a region with colour ``c`` at alpha ``a`` is equivalent to fading
        what is already there by ``(1 - a)``: the residue now ramps down exactly
        in step with the incoming artwork ramping up.

        The clip is the whole point.  Drawn unclipped, the curtain would sit in
        front of the OUTGOING layer too, and for the overlap the composite becomes
        ``in*t + out*(1-t)^2`` — a 25% dim at the midpoint.  So it is clipped to
        the complement of the incoming ``artwork_dst``, which is exactly the
        residue, and is EMPTY for a full-bleed ``cover`` frame: the common case
        draws nothing at all and the output is byte-identical to before.
        """
        if plan.artwork_dst == plan.screen:
            return
        # In blur mode the backdrop is already a full-bleed, opaque layer under
        # the artwork, so the outgoing residue is covered by the incoming item's
        # own backdrop rather than sitting on a flat colour.  Painting the
        # curtain here would wipe that backdrop with a flat colour and reintroduce
        # the very flicker the curtain exists to remove.
        if plan.ambient_strategy == "blur":
            return
        _, _, dw, dh = plan.artwork_dst
        if dw <= 0 or dh <= 0:
            return
        region = QRegion(self.rect()).subtracted(QRegion(_qr(plan.artwork_dst)))
        if region.isEmpty():
            return
        painter.save()
        painter.setClipRegion(region)
        painter.setOpacity(alpha)
        try:
            painter.fillRect(self.rect(), _qcolor(plan.ambient_colour))
        finally:
            painter.setOpacity(1.0)
            painter.restore()

    # -- Blurred backdrop ----------------------------------------------------

    def _paint_flat_backdrop(self, painter: QPainter, plan: RenderPlan, alpha: float) -> None:
        """Paint BLACK in place of a blur backdrop that is not available.

        Only reached when a backdrop genuinely does not exist — a cold start, the
        first slide after the blur settings changed, or a build that failed.
        Mid-slideshow the presenter HOLDS the slide until its backdrop is ready
        (see ``PresentationEngine.render``), so this is not the normal path for a
        transition.

        Black, and not ``plan.ambient_colour``: the configured colour belongs to
        the ``solid``/``bars`` looks.  Under a blur the only honest stand-in is
        the neutral one — a coloured band appearing for a frame or two reads as a
        flash of the wrong look, which is precisely what a glitch looks like.
        """
        region = QRegion(self.rect()).subtracted(QRegion(_qr(plan.artwork_dst)))
        if region.isEmpty():
            return
        painter.save()
        try:
            painter.setClipRegion(region)
            painter.setOpacity(max(0.0, min(1.0, alpha)))
            painter.fillRect(self.rect(), QColor(0, 0, 0))
        finally:
            painter.setOpacity(1.0)
            painter.restore()

    def _draw_backdrop_layer(
        self,
        painter: QPainter,
        plan: RenderPlan,
        pixmap: QPixmap | None,
        alpha: float,
    ) -> None:
        """Draw one backdrop layer (blurred pixmap + its dimming) at *alpha*.

        One layer, not the pair: the caller paints the outgoing item's and then
        the incoming item's, in that order, so both sit under both artworks.  See
        the z-order note in :meth:`paintEvent`.

        ``pixmap`` is looked up by the caller from the layer's own artwork, so
        each photo is surrounded by ITS OWN blur and never the neighbouring
        item's.

        Keeping the dimming inside the same opacity scope means the dim fades
        with its backdrop rather than sitting at full strength over a partially
        faded image.
        """
        if pixmap is None or pixmap.isNull():
            # Not loaded, or failed to build.  Paint the FLAT ambient colour for
            # this frame — nothing is ever blurred here.
            #
            # This is the load-bearing half of the "no stall" guarantee: a blur
            # inside a paint is a guaranteed dropped frame, and on a transition's
            # first frame that reads as the whole animation juddering.  A flat
            # band for a frame or two is imperceptible; a 56 ms hitch is not.
            #
            # The backdrop is built a slide ahead by the presenter, so in
            # practice this is only reached for the very first item after a cold
            # start, or after a backdrop failed to build.
            self._paint_flat_backdrop(painter, plan, alpha)
            return

        # Clipped to the complement of this item's own artwork, so the backdrop
        # shows only in the band around it and never intrudes into the photo.
        #
        # The backdrop is stored screen-sized (see ``ambient_blur``), so clipping
        # here — rather than building a band-shaped image — is what keeps it
        # geometrically consistent with the item it belongs to: it reads as the
        # same photo continuing behind the artwork, not a separately-scaled smear.
        region = QRegion(self.rect()).subtracted(QRegion(_qr(plan.artwork_dst)))
        if region.isEmpty():
            return

        painter.save()
        try:
            painter.setClipRegion(region)
            painter.setOpacity(max(0.0, min(1.0, alpha)))
            painter.drawPixmap(0, 0, pixmap)
            darken = plan.ambient_darken
            if darken > 0.0:
                dim = QColor(0, 0, 0, int(max(0.0, min(1.0, darken)) * 255))
                painter.fillRect(self.rect(), dim)
        finally:
            painter.setOpacity(1.0)
            painter.restore()

    # -- Painting ------------------------------------------------------------

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        try:
            plan = self._plan
            # 1. Base layers — the background, then the ambient fill.  Both are
            #    full-rectangle, so while a video surface is showing they are
            #    clipped out of the artwork rect: that rect is the hole the mpv
            #    widget shows through, and painting over it would hide the video.
            #
            #    Kept LINEAR (setClipRegion, then setClipping(False)) rather than
            #    wrapped in a try/finally: everything below — backdrops, artwork,
            #    overlay — may legitimately paint inside the artwork rect, and a
            #    plain reset says that without a second block.  It also keeps the
            #    ambient guard a direct statement here, which is what the
            #    blur-mode structural test inspects.
            if self._video_surface and plan is not None:
                painter.setClipRegion(
                    QRegion(self.rect()).subtracted(QRegion(_qr(plan.artwork_dst)))
                )
            # The canvas paints every pixel of itself otherwise
            # (WA_OpaquePaintEvent holds), so Qt can skip the erase pass.
            painter.fillRect(self.rect(), self._background)
            if plan is not None and plan.ambient is not None and plan.ambient_strategy != "blur":
                # Ambient fill — the only full-rectangle layer.  Absent whenever a
                # mat ring exists, because the Mat Window is then cut to the
                # artwork and no residue is left for fill.
                #
                # Skipped in blur mode: the per-item backdrop below covers the
                # same region, and painting a flat colour first would be hidden.
                self._fill(painter, plan.ambient, _qcolor(plan.ambient_colour))
            if self._video_surface:
                # Release the artwork clip.  The blurred backdrop needs no clip of
                # its own — it already excludes its own artwork rect (see
                # _draw_backdrop_layer).
                painter.setClipping(False)
            if plan is not None:
                # 2–5. The two crossfade items, each as a (backdrop, artwork)
                #      PAIR, stacked outgoing-first.
                #
                #      Z-ORDER IS LOAD-BEARING, and this is the arrangement that
                #      makes the blur effect correct in ``contain``:
                #
                #        2. outgoing backdrop   (its own blur, in its own band)
                #        3. outgoing artwork
                #        4. incoming backdrop   (its own blur, in its own band)
                #        5. incoming artwork
                #
                #      Each backdrop sits immediately behind ITS OWN artwork, so
                #      a letterboxed photo is always surrounded by its own blur —
                #      never the next item's.  The incoming pair composites over
                #      the outgoing pair as a unit, so the whole frame resolves
                #      together and nothing appears to vanish from behind
                #      something else when ``_advance()`` drops the outgoing
                #      layer.
                #
                #      Painting a backdrop above the other item's artwork (or
                #      below both) was the defect: an opaque full-screen backdrop
                #      occluded the outgoing photo, which then seemed to pop out
                #      of existence at the end of the transition.
                #
                #      The outgoing layer uses ITS OWN plan so a different aspect
                #      ratio is not drawn with the incoming item's geometry.
                if self._prev_plan is not None and self._prev_alpha > 0.01:
                    if self._prev_plan.ambient_strategy == "blur":
                        self._draw_backdrop_layer(
                            painter,
                            self._prev_plan,
                            self._backdrop_for(self._prev_image),
                            self._prev_alpha,
                        )
                    # The outgoing artwork is skipped while a video surface is
                    # showing: that layer IS the video, and the mpv widget below
                    # already has it on screen.
                    if not self._video_surface:
                        painter.setOpacity(self._prev_alpha)
                        try:
                            self._draw_artwork(painter, self._prev_plan, self._prev_image)
                        finally:
                            painter.setOpacity(1.0)

                if self._image is not None and self._image_alpha > 0.01:
                    # 3b. Transition curtain — wipes the outgoing item's exposed
                    #     residue (its letterbox bars in ``contain``) at the
                    #     incoming item's alpha, so the residue fades instead of
                    #     snapping when ``_advance()`` drops the outgoing layer.
                    #     Clipped to the incoming artwork's complement, and a
                    #     no-op for a full-bleed ``cover`` frame.
                    #
                    #     Skipped in blur mode: the incoming item's own backdrop,
                    #     just below, already fills that band with the incoming
                    #     photo's blur, so the residue is replaced rather than
                    #     needing a flat colour painted over it.  Running the
                    #     curtain there would wipe the backdrop with the flat
                    #     ambient colour and defeat the effect.
                    #
                    #     Restored for ``solid``/``bars``: removing the call
                    #     entirely (rather than gating it) dropped the fix for the
                    #     residue snap in those modes, where no backdrop exists.
                    if (
                        self._prev_plan is not None
                        and self._prev_alpha > 0.01
                        and plan.ambient_strategy != "blur"
                        and not self._video_surface
                    ):
                        self._paint_transition_curtain(painter, plan, self._image_alpha)

                    if plan.ambient_strategy == "blur":
                        self._draw_backdrop_layer(
                            painter,
                            plan,
                            self._backdrop_for(self._image),
                            self._image_alpha,
                        )
                    # Skipped for a video surface, where this layer is the video
                    # itself and mpv is already showing it (the hole above).
                    if not self._video_surface:
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
        the parts outside it.

        The scaling happens ONCE per layer, into a destination-sized pixmap that
        is then blitted 1:1 (see :meth:`_scaled_artwork`).  A crossfade only
        changes the alpha between frames, so re-scaling every frame — which is
        what a direct ``drawImage`` with a non-identity transform does — was
        pure waste, and was the transition's dominant CPU cost.

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

        pixmap = self._scaled_artwork(plan, source_image)
        if pixmap is not None:
            # 1:1 copy: no scaling, so no render hint and no resample.
            #
            # The origin is taken from the SAME rounded rect the curtain clips
            # to.  Rounding the two independently is what left a one-pixel seam:
            # the pixmap was ``int(dw)`` wide (truncated DOWN) while the curtain
            # stopped at ``int(x + w + 0.9999)`` (rounded UP), so the column
            # between them was painted by neither and showed the background
            # through as a thin line at the artwork edge.
            rect = _qr(plan.artwork_dst)
            painter.drawPixmap(rect.left(), rect.top(), pixmap)
            return

        # Fallback — a pre-scale that failed (e.g. an allocation refused on a
        # low-memory device).  Correct, just slower, and it must never be a
        # blank frame: showing the photo is the whole job.
        #
        # Drawn into the SAME rounded rect as the cached path, so a fallback
        # frame has no seam either.
        drawn = _qr(plan.artwork_dst)
        # The same source mapping the cached path uses: the image being drawn is
        # not always the media the plan was laid out for.  Qt CLIPS a source
        # rectangle that runs past the image and rescales whatever it did find,
        # so a stale window is not harmless here either — just wrong differently.
        window = _qr(plan.source_window(source_image.width(), source_image.height()))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(
            QRectF(
                float(drawn.left()), float(drawn.top()), float(drawn.width()), float(drawn.height())
            ),
            source_image,
            QRectF(
                float(window.left()),
                float(window.top()),
                float(window.width()),
                float(window.height()),
            ),
        )

    def _scaled_artwork(self, plan: RenderPlan, image: QImage) -> QPixmap | None:
        """Return *image* scaled to *plan*'s artwork rectangle, caching the result.

        Rebuilt only when the (image, source rect, destination rect) identity
        changes, which happens once per layer per transition rather than once
        per frame.  ``None`` means the caller must fall back to a direct scaled
        draw — a failed scale must degrade, never blank the frame.

        The returned pixmap is exactly :func:`_qr`'s size for
        ``artwork_dst``, and the caller blits it at that rect's origin.  Both
        sides must use that one rect: the transition curtain fills the
        *complement* of it, so any disagreement about where the artwork ends
        leaves an unpainted column between the two.
        """
        key = (
            id(image),
            plan.artwork_src,
            plan.artwork_dst,
        )

        is_primary = image is self._image
        if is_primary:
            if self._scaled_pixmap is not None and self._scaled_key == key:
                return self._scaled_pixmap
        else:
            if self._prev_scaled_pixmap is not None and self._prev_scaled_key == key:
                return self._prev_scaled_pixmap

        # The destination is the curtain's own rect, so the artwork and the
        # region the curtain fills are complements BY CONSTRUCTION.  Deriving
        # the size any other way (e.g. ``int(dw)``) reintroduces the seam: the
        # curtain rounds its far edge outward, so a truncated pixmap is one
        # pixel short of it and neither paints that column.
        target = _qr(plan.artwork_dst)
        target_w, target_h = max(1, target.width()), max(1, target.height())

        # Crop to the source rect first, so the scale is a pure resample of
        # exactly the pixels the plan wants — ``artwork_src`` is a sub-rectangle
        # for ``overflow="crop"``, and scaling before cropping would sample
        # pixels the plan discards.
        #
        # ``source_window`` maps the plan's media space onto THIS image, and the
        # two are not always the same thing: a video is drawn as its pre-generated
        # poster, which ffmpeg has already shrunk to fit the screen.  Cropping in
        # video pixels then reaches past the poster's edge, and ``QImage.copy``
        # pads that overhang black instead of clipping it — the poster ended up in
        # the corner of the panel with the rest of the screen black.
        window = _qr(plan.source_window(image.width(), image.height()))
        cropped = image.copy(window)
        if cropped.isNull():
            return None
        scaled = cropped.scaled(
            target_w,
            target_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.isNull():
            return None
        pixmap = QPixmap.fromImage(scaled)
        if pixmap.isNull():
            return None

        if is_primary:
            self._scaled_pixmap = pixmap
            self._scaled_key = key
        else:
            self._prev_scaled_pixmap = pixmap
            self._prev_scaled_key = key
        return pixmap
