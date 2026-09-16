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

import io
import logging
import threading
from typing import Any

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import QWidget

from metixel.display.overlay_element import OverlayElement
from metixel.framing.layout import RenderPlan

logger = logging.getLogger(__name__)


def _blur_payload(image: QImage, target_w: int, target_h: int, radius: float) -> QImage | None:
    """Stretch *image* to fill the target size, then blur it.

    Returns a plain ``QImage``, which — unlike ``QPixmap`` — is safe to create and
    hold on a worker thread.  That is what lets the warm worker do the expensive
    part off the GUI thread; the caller converts to a pixmap when it adopts the
    result.

    Stretching IGNORES the aspect ratio on purpose, so the backdrop covers every
    pixel and leaves no gaps for the letterbox effect to fail on.
    """
    stretched = image.scaled(
        max(1, target_w),
        max(1, target_h),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    if stretched.isNull():
        return None
    return _blur_qimage(stretched, radius)


def _blur_qimage(image: QImage, radius: float) -> QImage | None:
    """Return *image* blurred by *radius* pixels, or ``None`` on failure.

    Uses Pillow's ``BoxBlur``: a separable running-sum box filter.  That choice
    is measured, not assumed.

    The previous implementation downscaled to ``1/radius`` and scaled back up.
    At display size that is a bilinear round trip through an ~80x50 image, and it
    produces unmistakable **rectangular blocking** — the "JPEG-like artefacts"
    this replaces.  A 4x-magnified side-by-side on the target hardware made it
    obvious, where three different numeric proxies had all failed to distinguish
    the two.

    Why ``BoxBlur`` and not ``GaussianBlur``, measured at 1920x1200 on a Pi 5:

        downscale/upscale   25.1 ms   (blocky)
        BoxBlur(24)         55.9 ms   (smooth)
        GaussianBlur(24)   133.9 ms   (smooth)

    ``GaussianBlur`` is marginally smoother still, but 2.4x the cost for a
    difference invisible behind a dimmed backdrop.  This runs on the render
    thread once per slide, so the cheaper of two good options wins.

    Pillow is a hard runtime dependency (the optimisation pipeline uses it), so
    this adds nothing.  Any failure returns ``None`` and the caller falls back to
    the flat ambient look rather than blanking the frame.
    """
    try:
        from PIL import Image, ImageFilter
    except ImportError:  # pragma: no cover - Pillow is a runtime dependency
        logger.warning("Pillow unavailable — cannot blur the ambient backdrop")
        return None

    try:
        # QImage -> PNG bytes -> PIL.  Going through an encoded buffer is the
        # supported round trip; there is no direct QImage/PIL bridge.
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        payload = bytes(buffer.data())
        buffer.close()
        if not payload:
            return None

        with Image.open(io.BytesIO(payload)) as opened:
            source = opened.convert("RGB")
            blurred = source.filter(ImageFilter.BoxBlur(radius))

            out = QBuffer()
            out.open(QIODevice.OpenModeFlag.WriteOnly)
            blurred.save(out, format="PNG")
            data = bytes(out.data())
            out.close()

        result = QImage.fromData(data)
        return None if result.isNull() else result
    except Exception:
        logger.debug("Ambient blur failed — falling back to the flat fill", exc_info=True)
        return None


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


def _int_rect(rect: tuple[float, float, float, float]) -> QRect:
    """Round a plan rect to integers for ``QRegion``.

    ``QRegion`` is integer-only and takes a ``QRect``; it does **not** accept a
    plain 4-tuple (PySide6 does not coerce one into its
    ``(int, int, int, int)`` overload, so passing a tuple raises at paint time —
    on the device, not in CI).

    The far edge rounds OUTWARD.  Clipping a pixel too far into the incoming
    artwork is invisible, whereas stopping a pixel short leaves an unwiped
    sliver of the outgoing image, which is a visible seam in exactly the case
    this curtain exists to fix.
    """
    x, y, w, h = rect
    left, top = int(x), int(y)
    right = int(x + w + 0.9999)
    bottom = int(y + h + 0.9999)
    return QRect(left, top, max(0, right - left), max(0, bottom - top))


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
        # Full-bleed blurred backdrop, for ``ambient_strategy == "blur"``.
        #
        # One entry, not two: the backdrop is a single full-screen layer painted
        # UNDER whichever artwork is on top, so it does not need a per-layer copy
        # the way the pre-scaled artwork does.  During a crossfade it is the
        # incoming item's, which is what the outgoing item's residue fades into.
        #
        # Built by stretching the artwork to the whole screen (aspect IGNORED, so
        # there are no gaps) and then blurring it with a downscale/upscale — the
        # TV letterbox effect.  That is a per-SLIDE cost, not per-frame: the key
        # below changes only when the image or one of the blur parameters does.
        #
        # Darkening is NOT baked in.  It is applied at paint time as a translucent
        # black overlay, so moving the brightness slider costs one fillRect rather
        # than a full rebuild.
        self._blur_pixmap: QPixmap | None = None
        self._blur_key: tuple[Any, ...] | None = None
        #: The image the cached backdrop was built from, so a changed image can
        #: drop its pixmap without comparing pixels (as with the pre-scaled one).
        self._blur_image: QImage | None = None
        #: The outgoing item's backdrop, so a crossfade has both in flight.  The
        #: outgoing one is what shows first and the incoming one fades over it,
        #: exactly like the two artworks — that is what stops the outgoing photo
        #: popping out from behind an already-opaque incoming backdrop.
        self._prev_blur_pixmap: QPixmap | None = None
        self._prev_blur_key: tuple[Any, ...] | None = None
        self._prev_blur_image: QImage | None = None
        # Background backdrop warming.  The blur is the one operation here
        # expensive enough to blow a frame budget (~56 ms at 1920x1200), so it is
        # computed ahead of time on a worker thread and adopted on the GUI
        # thread.  See ``warm_backdrop`` / ``collect_warm_backdrop``.
        self._warm_thread: threading.Thread | None = None
        self._warm_key: tuple[Any, ...] | None = None
        self._warm_image: QImage | None = None
        #: ``(key, blurred QImage)`` published by the worker, consumed on the GUI
        #: thread.  Guarded because it crosses a thread boundary.
        self._warm_result: tuple[tuple[Any, ...], QImage | None] | None = None
        self._warm_lock = threading.Lock()
        # Background backdrop warming.  The blur is the one operation here
        # expensive enough to blow a frame budget, so it is computed ahead of
        # time on a worker thread and adopted on the GUI thread.  See
        # ``warm_backdrop`` / ``collect_warm_backdrop``.
        self._warm_thread: threading.Thread | None = None
        self._warm_key: tuple[Any, ...] | None = None
        self._warm_image: QImage | None = None
        #: (key, blurred QImage) published by the worker, consumed on the GUI
        #: thread.  Guarded because it crosses a thread boundary.
        self._warm_result: tuple[tuple[Any, ...], QImage | None] | None = None
        self._warm_lock = threading.Lock()
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

        # The blurred backdrops belong to their images, so each is released when
        # its image goes.  A full-screen pixmap is ~9 MB at 1920x1200, and two of
        # them pinned for the rest of the run is not something to leave on a 1 GB
        # Pi.  Both slots are pruned independently: the outgoing one only becomes
        # free once the crossfade has actually ended (``prev_image`` is None).
        if image is not self._blur_image:
            self._blur_pixmap = None
            self._blur_key = None
            self._blur_image = None
        if prev_image is not self._prev_blur_image:
            self._prev_blur_pixmap = None
            self._prev_blur_key = None
            self._prev_blur_image = None

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

    def backdrop_ready(self, plan: RenderPlan, image: QImage | None) -> bool:
        """Whether *plan*'s blurred backdrop is already cached and usable.

        The presenter asks this BEFORE starting a transition, so a slide is held
        until its backdrop exists rather than a transition beginning against a
        backdrop that is not there yet.  See ``PresentationEngine.render``.

        Cheap by design — a key comparison, no pixel work — because it is called
        every frame while a slide is being held.

        A non-blur plan is always ready: there is no backdrop to wait for.
        """
        if plan.ambient_strategy != "blur":
            return True
        if image is None:
            return False
        return (
            self._blur_key == self._backdrop_key(plan, image) and self._blur_pixmap is not None
        ) or (
            self._prev_blur_key == self._backdrop_key(plan, image)
            and self._prev_blur_pixmap is not None
        )

    def _backdrop_key(self, plan: RenderPlan, image: QImage) -> tuple[Any, ...]:
        """The cache key a backdrop for (*plan*, *image*) is stored under.

        Shared by :meth:`warm_backdrop`, :meth:`backdrop_ready` and
        :meth:`_blurred_backdrop` so the three cannot disagree about identity —
        a mismatch here would make the readiness check permanently False and hold
        every slide forever.
        """
        rect = _int_rect(plan.screen)
        radius = max(1.0, min(100.0, float(plan.ambient_blur_radius)))
        return (
            id(image),
            max(1, rect.width()),
            max(1, rect.height()),
            round(radius, 2),
        )

    def warm_backdrop(self, plan: RenderPlan, image: QImage | None) -> None:
        """Build *plan*'s blurred backdrop off the GUI thread, if not cached.

        Called for the NEXT item while the current slide is on screen, so the
        expensive part of a transition is already done by the time it starts.
        Until this existed the blur was built lazily inside ``paintEvent``, which
        cost ~56 ms on the first frame of every transition — visible as the whole
        crossfade juddering.

        Non-blocking: the work happens on a worker thread and the result is
        collected by :meth:`collect_warm_backdrop` on the GUI thread, mirroring
        ``ImageCache``.  Qt objects must not be created off the GUI thread, so the
        worker hands back only the blurred ``QImage`` payload.
        """
        if plan.ambient_strategy != "blur" or image is None:
            return
        key = self._backdrop_key(plan, image)
        if self._blur_key == key or self._prev_blur_key == key:
            return
        if self._warm_key == key and self._warm_thread is not None:
            return  # already in flight
        if self._warm_thread is not None and self._warm_thread.is_alive():
            # One job at a time, like the image cache: a newer request
            # supersedes the old one and the stale result is discarded because
            # its key no longer matches.
            self._warm_thread.join(timeout=0)
        if self._warm_thread is not None and self._warm_thread.is_alive():
            return

        self._warm_key = key
        self._warm_image = image
        self._warm_thread = threading.Thread(
            target=self._warm_worker,
            args=(key, image, plan),
            name="backdrop-warm",
            daemon=True,
        )
        self._warm_thread.start()

    def _warm_worker(self, key: tuple[Any, ...], image: QImage, plan: RenderPlan) -> None:
        """Blur *image* off the GUI thread and publish the finished payload.

        Never raises: a failed warm means the backdrop stays uncached and the
        presenter keeps holding the slide, which the stall timeout then breaks out
        of.  Crashing the warm thread would be worse.
        """
        try:
            rect = _int_rect(plan.screen)
            target_w, target_h = max(1, rect.width()), max(1, rect.height())
            radius = max(1.0, min(100.0, float(plan.ambient_blur_radius)))
            payload = _blur_payload(image, target_w, target_h, radius)
        except Exception:
            logger.debug("Backdrop warm failed", exc_info=True)
            payload = None
        with self._warm_lock:
            self._warm_result = (key, payload)

    def collect_warm_backdrop(self) -> bool:
        """Adopt a finished warmed backdrop on the GUI thread.

        Returns ``True`` when one was applied (so the caller can repaint).  Qt
        objects are created HERE, never in the worker — ``QPixmap`` in particular
        is a GUI-thread resource.
        """
        with self._warm_lock:
            result = self._warm_result
            self._warm_result = None
        if result is None:
            return False
        key, payload = result
        self._warm_key = None
        if payload is None or payload.isNull():
            return False
        pixmap = QPixmap.fromImage(payload)
        if pixmap.isNull():
            return False

        image = self._warm_image
        if image is self._blur_image:
            self._blur_pixmap, self._blur_key = pixmap, key
        elif image is self._prev_blur_image:
            self._prev_blur_pixmap, self._prev_blur_key = pixmap, key
        elif self._blur_image is None:
            self._blur_pixmap, self._blur_key, self._blur_image = pixmap, key, image
        else:
            self._prev_blur_pixmap, self._prev_blur_key, self._prev_blur_image = (
                pixmap,
                key,
                image,
            )
        self.update()
        return True

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
        region = QRegion(self.rect()).subtracted(QRegion(_int_rect(plan.artwork_dst)))
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
        """Paint the flat ambient colour in place of an unready backdrop.

        Only reached when a backdrop genuinely does not exist — a cold start, or
        the first slide after the blur parameters changed.  Mid-slideshow the
        presenter HOLDS the slide until its backdrop is ready (see
        ``PresentationEngine.render``), so this is not the normal path for a
        transition; it exists so the very first frame after a change, and the
        blank-screen case, still paint something sensible instead of nothing.
        """
        region = QRegion(self.rect()).subtracted(QRegion(_int_rect(plan.artwork_dst)))
        if region.isEmpty():
            return
        painter.save()
        try:
            painter.setClipRegion(region)
            painter.setOpacity(max(0.0, min(1.0, alpha)))
            painter.fillRect(self.rect(), _qcolor(plan.ambient_colour))
        finally:
            painter.setOpacity(1.0)
            painter.restore()

    def _draw_backdrop_layer(
        self,
        painter: QPainter,
        plan: RenderPlan,
        image: QImage | None,
        alpha: float,
    ) -> None:
        """Draw one backdrop layer (blurred pixmap + its dimming) at *alpha*.

        One layer, not the pair: the caller paints the outgoing item's and then
        the incoming item's, in that order, so both sit under both artworks.  See
        the z-order note in :meth:`paintEvent`.

        Keeping the dimming inside the same opacity scope means the dim fades
        with its backdrop rather than sitting at full strength over a partially
        faded image.
        """
        pixmap = self._blurred_backdrop(plan, image)
        if pixmap is None:
            # Not ready (or failed).  Paint the FLAT ambient colour for this
            # frame instead of building the blur here.
            #
            # This is the load-bearing half of the "no stall" guarantee.  The
            # blur costs tens of milliseconds, so building it inside a paint is a
            # guaranteed dropped frame — and on a transition's FIRST frame that
            # reads as the whole animation juddering.  A flat band for a frame or
            # two is imperceptible; a 56 ms hitch is not.
            #
            # The backdrop is warmed ahead of time (see ``warm_backdrop``), so in
            # practice this branch is only reached for the very first item after
            # a config change or a cold start.
            self._paint_flat_backdrop(painter, plan, alpha)
            return

        # Clipped to the complement of this item's own artwork, so the backdrop
        # shows only in the band around it and never intrudes into the photo.
        #
        # The blur itself is built screen-sized from the whole image (see
        # :meth:`_blurred_backdrop`), so clipping here — rather than building a
        # band-shaped pixmap — is what keeps the backdrop geometrically
        # consistent with the item it belongs to: it reads as the same photo
        # continuing behind the artwork, not a separately-scaled smear.
        region = QRegion(self.rect()).subtracted(QRegion(_int_rect(plan.artwork_dst)))
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

    def _blurred_backdrop(  # noqa: PLR0911 - each bail-out is a distinct failure
        self, plan: RenderPlan, image: QImage | None = None
    ) -> QPixmap | None:
        """Return a blurred, screen-sized copy of *image*, caching it.

        ``radius`` is the blur's pixel radius, so **larger means blurrier** — the
        intuitive direction.  (An earlier version used it as a downscale divisor,
        which made larger mean *less* blur and produced the blocky artefacts see
        :func:`_blur_qimage`.)

        The aspect ratio is deliberately IGNORED when stretching to the screen, so
        the backdrop always covers every pixel; using ``KeepAspectRatio`` would
        reintroduce exactly the letterbox gaps this effect exists to remove.

        ``image`` defaults to the primary layer's artwork; the crossfade's
        outgoing layer passes its own, so each backdrop is built from the item it
        belongs to rather than the incoming one.

        Rebuilt only when the image, the screen size, or a blur parameter changes.
        That is the "once per showing of the slide" requirement: a crossfade
        repaints ~75 times and calls this on every frame, and all but the first
        return the cached pixmap.  ``None`` means the caller must skip the
        backdrop — a failed build must degrade to the flat ambient look, never
        blank the frame.
        """
        image = self._image if image is None else image
        if image is None:
            return None

        key = self._backdrop_key(plan, image)
        # Two slots, because a crossfade has two backdrops in flight.  The key
        # carries the image identity, so a slot can be matched by key alone and
        # the two never need swapping: whichever slot holds this key wins.
        if self._blur_key == key and self._blur_pixmap is not None:
            return self._blur_pixmap
        if self._prev_blur_key == key and self._prev_blur_pixmap is not None:
            return self._prev_blur_pixmap

        rect = _int_rect(plan.screen)
        radius = max(1.0, min(100.0, float(plan.ambient_blur_radius)))
        blurred = _blur_payload(image, rect.width(), rect.height(), radius)
        if blurred is None or blurred.isNull():
            return None

        pixmap = QPixmap.fromImage(blurred)
        if pixmap.isNull():
            return None

        # Store into the slot that already belongs to this image if there is one,
        # otherwise take the free slot.  Matching on the image keeps the primary
        # and outgoing roles stable across a transition, so the LayerRole pass
        # below cannot thrash between slots each frame.
        # Store into the slot already owned by this image, else an empty slot,
        # else the outgoing one.  Matching on the image first is what keeps each
        # backdrop's role stable across the frames of a transition, so the two do
        # not thrash between slots.
        #
        # At most two are ever held (one per crossfade layer), so overwriting
        # ``prev`` when both are taken cannot lose a pixmap still on screen: the
        # third distinct image can only appear after the previous transition has
        # ended and released its slot.
        if image is self._blur_image:
            self._blur_pixmap, self._blur_key = pixmap, key
        elif image is self._prev_blur_image:
            self._prev_blur_pixmap, self._prev_blur_key = pixmap, key
        elif self._blur_image is None:
            self._blur_pixmap, self._blur_key, self._blur_image = pixmap, key, image
        else:
            self._prev_blur_pixmap, self._prev_blur_key, self._prev_blur_image = (
                pixmap,
                key,
                image,
            )
        return pixmap

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
                #
                #    Skipped in blur mode: the per-item backdrop below covers the
                #    same region, and painting a flat colour first would be hidden.
                if plan.ambient is not None and plan.ambient_strategy != "blur":
                    self._fill(painter, plan.ambient, _qcolor(plan.ambient_colour))

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
                            painter, self._prev_plan, self._prev_image, self._prev_alpha
                        )
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
                    ):
                        self._paint_transition_curtain(painter, plan, self._image_alpha)

                    if plan.ambient_strategy == "blur":
                        self._draw_backdrop_layer(painter, plan, self._image, self._image_alpha)
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
            rect = _int_rect(plan.artwork_dst)
            painter.drawPixmap(rect.left(), rect.top(), pixmap)
            return

        # Fallback — a pre-scale that failed (e.g. an allocation refused on a
        # low-memory device).  Correct, just slower, and it must never be a
        # blank frame: showing the photo is the whole job.
        #
        # Drawn into the SAME rounded rect as the cached path, so a fallback
        # frame has no seam either.
        drawn = _int_rect(plan.artwork_dst)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(
            QRectF(
                float(drawn.left()), float(drawn.top()), float(drawn.width()), float(drawn.height())
            ),
            source_image,
            QRectF(float(int(sx)), float(int(sy)), float(int(sw)), float(int(sh))),
        )

    def _scaled_artwork(self, plan: RenderPlan, image: QImage) -> QPixmap | None:
        """Return *image* scaled to *plan*'s artwork rectangle, caching the result.

        Rebuilt only when the (image, source rect, destination rect) identity
        changes, which happens once per layer per transition rather than once
        per frame.  ``None`` means the caller must fall back to a direct scaled
        draw — a failed scale must degrade, never blank the frame.

        The returned pixmap is exactly :func:`_int_rect`'s size for
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

        sx, sy, sw, sh = plan.artwork_src
        # The destination is the curtain's own rect, so the artwork and the
        # region the curtain fills are complements BY CONSTRUCTION.  Deriving
        # the size any other way (e.g. ``int(dw)``) reintroduces the seam: the
        # curtain rounds its far edge outward, so a truncated pixmap is one
        # pixel short of it and neither paints that column.
        target = _int_rect(plan.artwork_dst)
        target_w, target_h = max(1, target.width()), max(1, target.height())

        # Crop to the source rect first, so the scale is a pure resample of
        # exactly the pixels the plan wants — ``artwork_src`` is a sub-rectangle
        # for ``overflow="crop"``, and scaling before cropping would sample
        # pixels the plan discards.
        cropped = image.copy(QRect(int(sx), int(sy), max(1, int(sw)), max(1, int(sh))))
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
