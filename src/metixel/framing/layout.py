# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Layout engine — framing geometry in millimetres, converted to pixels once.

This is the single bridge between the pure framing engine and a renderer.  It
takes the engine's millimetre :class:`~metixel.framing.framing_engine.
FramingResult` and produces a :class:`RenderPlan` of **pixel** rectangles in
the exact paint order the canvas needs.

Two boundaries are load-bearing:

* **No Qt here.**  The plan is plain tuples, so every geometry rule is testable
  on a machine without PySide6 installed (which is what CI is).  The canvas
  layer is the only place that turns a plan into ``QRect``/``QPainter`` calls.
* **No display backend here.**  The plan depends on a screen *size*, not on a
  rendering surface, so it can be computed before the display exists — which is
  what lets the same code path serve the live frontend, a preview endpoint and
  the tests.

Paint order is defined by the engine's specification and is **not** the same as
nesting order: ambient fill first (the only full-rectangle layer), then the
artwork, then whitespace, mat and moulding as disjoint annuli.  Because the
annuli never overlap the artwork, drawing the artwork first is safe — and it
means the canvas can paint a single list in order without any z-buffer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from metixel.framing.framing_engine import (
    FramingRequest,
    FramingResult,
    Rect,
    calculate_framing,
    check_invariants,
)
from metixel.framing.resolve import MediaSize, resolve, screen_for

logger = logging.getLogger(__name__)

#: A pixel rectangle as ``(x, y, width, height)``.
PxRect = tuple[float, float, float, float]


@dataclass(frozen=True)
class RenderPlan:
    """Everything a canvas needs to paint one framed item, in pixels.

    Rectangles are in screen coordinates with the origin at the top-left, and
    are already scaled for the target resolution.  A rectangle with zero width
    or height is omitted from the lists rather than included as a no-op.
    """

    #: The full-canvas rectangle, for clearing.
    screen: PxRect

    #: Ambient fill — the only full-rectangle layer.  ``None`` when absent.
    #: Present only when a *fixed* Mat Window meets mismatched artwork, which
    #: for the virtual branch means a borderless style (``ring == 0``).
    ambient: PxRect | None

    #: Where the artwork is drawn on screen.
    artwork_dst: PxRect

    #: The sub-rectangle of the *source* image to sample.  For ``overflow ==
    #: "crop"`` this is the centred crop window; otherwise it is the whole
    #: image.  Expressed in source pixels.
    artwork_src: PxRect

    #: Whitespace band, as up to four annulus rectangles.
    whitespace: tuple[PxRect, ...]

    #: Mat ring, as up to four annulus rectangles.
    matte: tuple[PxRect, ...]

    #: Frame moulding ring, as up to four annulus rectangles.
    moulding: tuple[PxRect, ...]

    #: Declared band colours, already resolved by the templates (``#rrggbb``).
    matte_colour: str
    whitespace_colour: str
    ambient_colour: str

    #: Derived diagnostics — not used for painting.
    style: str
    branch: str
    overflow: str

    @property
    def image_rect(self) -> PxRect:
        """Legacy alias for :attr:`artwork_dst`.

        Kept so callers migrating from the old ``LayoutEngine`` (which returned
        ``{"image_rect": ..., "matte_rects": [...]}``) can be ported in stages
        without a silent behaviour change.
        """
        return self.artwork_dst

    @property
    def matte_rects(self) -> tuple[PxRect, ...]:
        """Legacy alias for :attr:`matte`."""
        return self.matte


def _to_px(rect: Rect, sx: float, sy: float) -> PxRect:
    """Scale a millimetre rectangle to pixels, per axis.

    Scaling each axis by its own factor is what keeps a square artwork square
    on a panel whose pixels are not perfectly square, and it is why the engine
    reports ``px_per_mm`` as a pair rather than a single number.
    """
    return (rect.x * sx, rect.y * sy, rect.width * sx, rect.height * sy)


def _annulus(outer: PxRect, inner: PxRect) -> tuple[PxRect, ...]:
    """Decompose the band between two nested rectangles into up to four rects.

    Returns the top, bottom, left and right bands, skipping any that are empty
    on either axis.  Drawing four disjoint rectangles is cheaper than a
    clipping path and needs no compositing — which matters on a Pi 3's VC4.

    The early-exit test is on the *insets*, not on the widths: an inner rect
    flush with an edge (e.g. ``inner.x == outer.x``) still has valid top and
    bottom bands, and comparing widths would discard them.  That case is not
    hypothetical — it is what a full-bleed or near-full-width Mat Window
    produces.
    """
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner

    # Nothing to draw only when the inner rect has consumed the outer one on
    # BOTH axes (each inset is at or past the outer edge).
    top_h = iy - oy
    left_w = ix - ox
    bottom_h = (oy + oh) - (iy + ih)
    right_w = (ox + ow) - (ix + iw)
    if min(top_h, left_w, bottom_h, right_w) < 0:
        # Inner rect is not nested inside the outer — nothing meaningful to
        # draw.  Reported rather than raised: a canvas should not crash.
        return ()
    if max(top_h, left_w, bottom_h, right_w) <= 0:
        return ()

    side_h = ih
    bottom_y = iy + ih
    right_x = ix + iw

    bands: list[PxRect] = []
    if top_h > 0:
        bands.append((ox, oy, ow, top_h))
    if bottom_h > 0:
        bands.append((ox, bottom_y, ow, bottom_h))
    if left_w > 0:
        bands.append((ox, iy, left_w, side_h))
    if right_w > 0:
        bands.append((right_x, iy, right_w, side_h))
    return tuple(bands)


def _crop_source(result: FramingResult, media: MediaSize) -> PxRect:
    """Return the source sub-rectangle to sample for ``overflow == "crop"``.

    ``crop`` means the artwork covers a *fixed* Mat Window, so the parts of the
    image outside the window are discarded.  The engine reports the resulting
    on-screen rectangle; the source window is derived from the aspect mismatch
    between that rectangle and the source image, centred.

    For every other case the whole image is used, so this returns the full
    source rectangle and the canvas scales it into ``artwork_dst``.
    """
    full: PxRect = (0.0, 0.0, float(media.width), float(media.height))
    if result.overflow != "crop" or not media.is_valid:
        return full

    dst_w, dst_h = result.artwork.bounds.width, result.artwork.bounds.height
    if dst_w <= 0 or dst_h <= 0:
        return full

    dst_ratio = dst_w / dst_h
    src_ratio = media.width / media.height

    if abs(src_ratio - dst_ratio) < 1e-9:
        return full

    if src_ratio > dst_ratio:
        # Source is wider than the window → crop the sides.
        keep_w = media.height * dst_ratio
        return ((media.width - keep_w) / 2.0, 0.0, keep_w, float(media.height))

    # Source is taller than the window → crop top and bottom.
    keep_h = media.width / dst_ratio
    return (0.0, (media.height - keep_h) / 2.0, float(media.width), keep_h)


class LayoutEngine:
    """Compute a :class:`RenderPlan` for a media item.

    One instance per screen geometry.  Construct it with the **effective**
    (already-rotated) pixel size the frontend is rendering at, plus the
    rotation so the correct physical preset is chosen.
    """

    def __init__(
        self,
        screen_w: int = 1920,
        screen_h: int = 1200,
        *,
        rotation: int = 0,
        style: str = "gallery",
        overflow: str | None = None,
        edge_margin: float | None = None,
        moulding_width: float | None = None,
    ) -> None:
        self._screen_w = int(screen_w) if screen_w > 0 else 1920
        self._screen_h = int(screen_h) if screen_h > 0 else 1200
        self._rotation = rotation
        self._style = style
        self._overflow = overflow
        self._edge_margin = edge_margin
        self._moulding_width = moulding_width

        self._screen = screen_for(rotation, width_px=self._screen_w, height_px=self._screen_h)
        self._sx, self._sy = self._screen.px_per_mm  # type: ignore[misc]
        logger.info(
            "LayoutEngine: %dx%d px, %s, style=%s, overflow=%s (%.4f px/mm)",
            self._screen_w,
            self._screen_h,
            "portrait" if rotation % 360 in (90, 270) else "landscape",
            style,
            overflow or "style default",
            self._sx or 0.0,
        )

    # -- Properties ----------------------------------------------------------

    @property
    def screen_w(self) -> int:
        return self._screen_w

    @property
    def screen_h(self) -> int:
        return self._screen_h

    # -- Public -------------------------------------------------------------

    def compute(
        self,
        media: MediaSize,
        *,
        style: str | None = None,
        overflow: str | None = None,
        whitespace: bool | None = None,
    ) -> RenderPlan:
        """Return the pixel layout for *media*.

        An item with unusable dimensions (a file still being probed) is drawn
        full-bleed with no mat rather than raising — a photo frame must never
        show a traceback, and the caller's next playlist refresh will replace
        it once the backend has probed it.
        """
        full: PxRect = (0.0, 0.0, float(self._screen_w), float(self._screen_h))
        if not media.is_valid:
            logger.debug(
                "LayoutEngine: unusable media size %dx%d — full-bleed fallback",
                media.width,
                media.height,
            )
            return RenderPlan(
                screen=full,
                ambient=None,
                artwork_dst=full,
                artwork_src=(0.0, 0.0, float(max(media.width, 1)), float(max(media.height, 1))),
                whitespace=(),
                matte=(),
                moulding=(),
                matte_colour="#ffffff",
                whitespace_colour="#ffffff",
                ambient_colour="#101014",
                style=style or self._style,
                branch="virtual",
                overflow=overflow or self._overflow or "fill",
            )

        request: FramingRequest = resolve(
            self._screen,
            media,
            style=style or self._style,
            overflow=overflow if overflow is not None else self._overflow,
            whitespace=whitespace,
            edge_margin=self._edge_margin,
            moulding_width=self._moulding_width,
        )
        result = calculate_framing(request)

        # The invariant checker is the specification's own contract; a
        # violation means a geometry bug, and the honest response is to log it
        # loudly and still render (a frame with a slightly wrong mat band beats
        # a black screen on a wall).
        problems = check_invariants(result)
        if problems:
            logger.warning(
                "Framing invariants violated for %dx%d %s: %s",
                media.width,
                media.height,
                style or self._style,
                "; ".join(problems),
            )

        sx, sy = result.screen.px_per_mm
        assert sx is not None and sy is not None  # screen preset carries px dims

        mat_window = _to_px(result.mat.window, sx, sy)
        frame_opening = _to_px(result.frame.opening, sx, sy)
        frame_outer = _to_px(result.frame.outer, sx, sy)
        artwork_dst = _to_px(result.artwork.bounds, sx, sy)

        # Paint order matters: ambient fill is the only full-rectangle layer,
        # and the annuli are disjoint from the artwork, which is why the
        # artwork can be drawn before the rings without a depth buffer.
        ambient: PxRect | None = None
        if result.ambient_fill.present:
            ambient = _to_px(result.ambient_fill.region, sx, sy)

        # Whitespace sits *inside* the Mat Window, between the window edge and
        # the artwork — not between the artwork and the ring.
        whitespace_outer = _to_px(result.whitespace.outer, sx, sy)
        whitespace_bands = (
            _annulus(whitespace_outer, artwork_dst) if result.whitespace.enabled else ()
        )

        return RenderPlan(
            screen=full,
            ambient=ambient,
            artwork_dst=artwork_dst,
            artwork_src=_crop_source(result, media),
            whitespace=whitespace_bands,
            matte=_annulus(frame_opening, mat_window) if mat_window != frame_opening else (),
            moulding=_annulus(frame_outer, frame_opening),
            matte_colour=result.mat.colour,
            whitespace_colour=result.whitespace.colour,
            ambient_colour=result.ambient_fill.colour,
            style=style or self._style,
            branch=result.branch,
            overflow=result.overflow,
        )

    # -- Debug --------------------------------------------------------------

    def describe(self, plan: RenderPlan) -> dict[str, Any]:
        """Return a JSON-serialisable summary of *plan*.

        Used by the framing preview endpoint and by ``visualize_framing`` so a
        geometry question can be answered without a screenshot.
        """
        return {
            "screen": plan.screen,
            "style": plan.style,
            "branch": plan.branch,
            "overflow": plan.overflow,
            "ambient": plan.ambient,
            "artwork_dst": plan.artwork_dst,
            "artwork_src": plan.artwork_src,
            "whitespace": plan.whitespace,
            "matte": plan.matte,
            "moulding": plan.moulding,
            "colours": {
                "matte": plan.matte_colour,
                "whitespace": plan.whitespace_colour,
                "ambient": plan.ambient_colour,
            },
        }
