# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Resolve a framing style + screen + artwork into a pinned :class:`FramingRequest`.

This module is the **only** place that decides which :class:`~metixel.framing.
framing_templates.Screen` preset and which style fraction apply to a piece of
media.  It exists so that neither the renderer, nor ``config.py``, nor the web
UI ever hand-rolls panel geometry: the millimetre constants live in
:mod:`metixel.framing.framing_templates`, and this module only *selects*
between them.

That separation matters because the same panel is described in two places that
must agree — the physical screen (518 x 324 mm active area inside a 528 x 337 mm
housing) and its pixel dimensions (1920 x 1200).  Routing every caller through
here means a future panel (or a Pi 4 owner's 4K monitor) is added as a template
preset, not as a literal in the render path.

The engine itself stays pure: this module does no I/O, imports no Qt, and
touches no media.  It is deterministic in its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from metixel.framing import framing_templates as templates
from metixel.framing.framing_engine import (
    AmbientBlurFilter,
    AmbientStrategy,
    FramingRequest,
    MediaDescriptor,
    MediaType,
    Overflow,
    Screen,
)

__all__ = ["ORIENTATIONS", "MediaSize", "resolve", "scale_screen", "screen_for"]

#: The mountings a :class:`Screen` preset can be expressed in.  A ``rotation``
#: of 90/270 turns the panel on its side, which changes both the millimetre
#: active area and the pixel dimensions — and, importantly, leaves the style's
#: reference dimension unchanged (it is the *shorter* side either way up).
ORIENTATIONS = ("landscape", "portrait")


def _orientation_for_rotation(rotation: int) -> str:
    """Map a clockwise rotation in degrees to a mounting orientation.

    0 and 180 leave the panel in its landscape mounting; 90 and 270 stand it
    on end.  Anything unrecognised is treated as landscape, matching the
    display layer's own tolerance for unexpected rotation values.
    """
    try:
        rot = int(rotation) % 360
    except (TypeError, ValueError):
        rot = 0
    return "portrait" if rot in (90, 270) else "landscape"


def scale_screen(base: Screen, width_px: int, height_px: int) -> Screen:
    """Return *base* re-expressed at a different pixel resolution.

    The physical millimetres are held constant and only ``width_px`` /
    ``height_px`` change, which is what keeps mat bands physically identical
    (in mm) across panels of differing pixel density.  ``to_px()`` divides the
    millimetre geometry by the *new* pixels-per-mm, so a 2560 x 1600 panel
    still renders a 40 mm ``gallery`` ring as 40 mm — not as 40 px.

    A non-positive pixel dimension means "unknown", and the base preset's own
    pixel dimensions are kept rather than inventing a resolution.
    """
    if width_px <= 0 or height_px <= 0:
        return base
    return Screen(
        width_mm=base.width_mm,
        height_mm=base.height_mm,
        width_px=float(width_px),
        height_px=float(height_px),
        housing_width_mm=base.housing_width_mm,
        housing_height_mm=base.housing_height_mm,
        unit=base.unit,
    )


def screen_for(rotation: int = 0, *, width_px: int = 0, height_px: int = 0) -> Screen:
    """Return the Metixel panel preset for *rotation*, at the given resolution.

    Args:
        rotation: Screen rotation in degrees clockwise (0, 90, 180, 270).
        width_px: Detected on-screen width in pixels (0 = use the preset's).
        height_px: Detected on-screen height in pixels (0 = use the preset's).

    The pixel dimensions are the ones the frontend has *already* rotated, so a
    1920 x 1200 panel at ``rotation=90`` must be passed as 1200 x 1920.
    Passing the panel's native resolution with a rotation of 90 would double
    apply the rotation, so callers should take these from the display backend.
    """
    orientation = _orientation_for_rotation(rotation)
    return scale_screen(templates.METIXEL_SCREENS[orientation], width_px, height_px)


@dataclass(frozen=True)
class MediaSize:
    """The pixel dimensions of the artwork being framed.

    Only the ratio matters to the engine, but the absolute size is kept so a
    caller can report it.  ``media_type`` selects between image and video
    presentation defaults (a video has no whitespace, for instance).
    """

    width: int
    height: int
    media_type: MediaType = "image"

    @property
    def is_valid(self) -> bool:
        """Whether the dimensions are usable for framing.

        ``MediaItem`` defaults both fields to 0 and the frontend legitimately
        encounters zero-sized items (a file still being probed).  Those cannot
        be framed, so callers use this to fall back rather than divide by zero.
        """
        return self.width > 0 and self.height > 0


def resolve(
    screen: Screen,
    media: MediaSize,
    *,
    style: str = "gallery",
    overflow: str | None = None,
    whitespace: bool | None = None,
    ambient_strategy: str | None = None,
    ambient_colour: str | None = None,
    ambient_blur_radius: float | None = None,
    ambient_darken: float | None = None,
    ambient_blur_filter: str | None = None,
    edge_margin: float | None = None,
    moulding_width: float | None = None,
) -> FramingRequest:
    """Build a fully specified :class:`FramingRequest` in millimetres.

    The engine never sees a style *name* — this function converts the style's
    fraction into a millimetre Mat Ring using the screen's reference dimension,
    so the returned request is explicit and reproducible.

    Args:
        screen: The panel preset (see :func:`screen_for`).
        media: The artwork's pixel dimensions.
        style: A key from :data:`~metixel.framing.framing_templates.STYLES`.
        overflow: ``"crop"`` or ``"fill"``; ``None`` uses the style's default
            (``"fill"``, i.e. contain plus ambient fill).
        whitespace: Force the whitespace band on/off; ``None`` defers to the
            style and the presentation (see ``resolve_whitespace``).
        ambient_strategy: ``"solid"``, ``"blur"`` or ``"bars"``.
        ambient_colour: ``"#rrggbb"`` for the ambient fill (and therefore for the
            residue visible in ``contain``).  ``None`` keeps the framing
            engine's own default.
        edge_margin: Inset from the screen edge, in mm (``None`` = the
            template default, which hides the bezel).
        moulding_width: Frame moulding width in mm (``None`` = template default).

    Returns:
        A :class:`FramingRequest` for the **virtual** branch — the digital
        frame draws a software mat, so ``mat_window`` is deliberately not
        supplied and the Mat Window is cut to the artwork's aspect.

    The **physical** branch is not reachable from here by design.  It is
    exercised by :mod:`metixel.framing.framing_templates` directly and must
    stay supported: the Web UI and the on-screen menu represent a *real* frame
    with it, which needs ``required_rebate`` and a Frame Outer that can extend
    beyond the panel.  Do not prune it as dead code.

    Note that ``required_rebate`` on the physical branch is a **fit check** — the
    overlap a real frame must provide to hide the panel edge — and is never
    drawn.  It is not part of the composition and has no render step.
    """
    descriptor = MediaDescriptor(
        width=float(media.width),
        height=float(media.height),
        type=media.media_type,
    )

    # Each override is passed explicitly rather than splatted from a dict.
    # ``**kwargs`` erases the per-argument types, so mypy cannot verify that
    # (say) ``overflow`` is a valid literal — and that check is worth keeping,
    # because a typo in a style/overflow name would otherwise fail only at
    # render time on the device.
    overflow_literal = None if overflow is None else _as_overflow(overflow)
    strategy_literal = None if ambient_strategy is None else _as_ambient_strategy(ambient_strategy)

    return templates.build_request(
        screen,
        descriptor,
        style,
        overflow=overflow_literal,
        whitespace=whitespace,
        ambient_strategy=strategy_literal,
        ambient_colour=ambient_colour,
        ambient_blur_radius=(
            _DEFAULT_AMBIENT_BLUR if ambient_blur_radius is None else float(ambient_blur_radius)
        ),
        ambient_darken=(
            _DEFAULT_AMBIENT_DARKEN if ambient_darken is None else float(ambient_darken)
        ),
        ambient_blur_filter=(
            None if ambient_blur_filter is None else _as_ambient_blur_filter(ambient_blur_filter)
        ),
        edge_margin=edge_margin if edge_margin is not None else _UNSET_EDGE_MARGIN,
        moulding_width=moulding_width if moulding_width is not None else _UNSET_MOULDING_WIDTH,
    )


def _build_request_defaults() -> dict[str, float]:
    """Read the template's own defaults so this module never repeats them.

    ``edge_margin`` and ``moulding_width`` live in ``framing_templates``; a
    literal repeated here would be exactly the duplication this module exists
    to remove.  Introspecting once at import time keeps that guarantee while
    still letting the values be passed explicitly (so mypy can type them).
    """
    import inspect

    params = inspect.signature(templates.build_request).parameters
    return {
        "edge_margin": float(params["edge_margin"].default),
        "moulding_width": float(params["moulding_width"].default),
        "ambient_blur_radius": float(params["ambient_blur_radius"].default),
        "ambient_darken": float(params["ambient_darken"].default),
    }


#: Template defaults, read once.  Forwarded verbatim when the caller omits the
#: argument, so the number is never restated in this module.
_DEFAULTS: dict[str, float] = _build_request_defaults()
_UNSET_EDGE_MARGIN: float = _DEFAULTS["edge_margin"]
_UNSET_MOULDING_WIDTH: float = _DEFAULTS["moulding_width"]
_DEFAULT_AMBIENT_BLUR: float = _DEFAULTS["ambient_blur_radius"]
_DEFAULT_AMBIENT_DARKEN: float = _DEFAULTS["ambient_darken"]


def _as_overflow(value: str) -> Overflow:
    """Validate an overflow string, failing loudly on a typo.

    A bad value reaching the engine would raise deep inside ``calculate_framing``
    with a less obvious message, and — worse — it could do so per frame on a
    device.  Validating at the configuration boundary keeps the failure early.
    """
    if value not in ("crop", "fill"):
        raise ValueError(f"overflow must be 'crop' or 'fill', got {value!r}")
    return cast(Overflow, value)


def _as_ambient_strategy(value: str) -> AmbientStrategy:
    """Validate an ambient strategy string."""
    if value not in ("solid", "blur", "bars"):
        raise ValueError(f"ambient_strategy must be 'solid', 'blur' or 'bars', got {value!r}")
    return cast(AmbientStrategy, value)


def _as_ambient_blur_filter(value: str) -> AmbientBlurFilter:
    """Validate a blur-kernel name."""
    if value not in ("box", "gaussian"):
        raise ValueError(f"ambient_blur_filter must be 'box' or 'gaussian', got {value!r}")
    return cast(AmbientBlurFilter, value)
