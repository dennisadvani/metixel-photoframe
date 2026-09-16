# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Framing style templates and screen presets for the v2 framing engine.

Specification: ``docs/geometry-model.md``.

This module holds **all** style knowledge so :mod:`metixel.framing_engine` can
stay a pure dimensions calculator:

* :data:`STYLES` — the framing styles (Mat Ring proportion + ambient look).
* :data:`SCREENS` — named screen presets.
* :func:`build_request` — resolve a style + screen + media into a fully
  specified :class:`~metixel.framing_engine.FramingRequest` in millimetres.

Style and overflow are **separate axes** (spec/"Framing style and overflow are
separate axes"):

===============  ====================================================
Axis             Values
===============  ====================================================
Framing style    ``borderless``, ``modern``, ``classic``, ``gallery``,
                 ``museum``, ``floating``, ``polaroid``, ``custom``
Overflow         ``crop`` (cover the fixed window) | ``fill``
                 (contain and let ambient fill absorb the residue)
Ambient look     ``solid`` | ``blur`` | ``bars``
===============  ====================================================

``immersive`` and ``immersive_fill`` are **not** framing styles: they are
overflow presets with a borderless ring.  See :data:`OVERFLOW_PRESETS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .framing_engine import (
    AmbientFillSpec,
    Effects,
    FocalPoint,
    FramingRequest,
    Insets,
    MediaDescriptor,
    Rect,
    Screen,
    WhitespaceSpec,
)

# ---------------------------------------------------------------------------
# Screen presets
# ---------------------------------------------------------------------------

# The panel: a 518 x 324 mm active area inside a 528 x 337 mm housing.  The
# 5 mm side / 6.5 mm vertical border is the non-screen area a frame must cover.
# ``minimum_rebate`` (528 x 337 mm) is the smallest frame rebate that will hold
# the panel, and is reported for customer guidance when mounting without a mat.
#
# The rebate is a FIT CHECK, never a drawn element: it answers "will this panel
# fit in a real frame?", and nothing in the render path paints or applies it.
METIXEL_16_10_1920x1200 = Screen(
    width_mm=518.0,
    height_mm=324.0,
    width_px=1920.0,
    height_px=1200.0,
    housing_width_mm=528.0,
    housing_height_mm=337.0,
    unit="mm",
)

#: The minimum frame rebate (mm) for the Metixel panel, i.e. the size a frame
#: must be at least in order to contain the panel.  Fit check only — reported to
#: the user, never drawn.
METIXEL_MINIMUM_REBATE = METIXEL_16_10_1920x1200.minimum_rebate

#: The same panel mounted **portrait** — the assembly rotated 90°.  The active
#: area and panel swap axes, and so do the pixel dimensions.  The reference
#: dimension is unchanged at 320 mm, which is why a style keeps the same ring
#: width whichever way up the screen is.
METIXEL_16_10_PORTRAIT = Screen(
    width_mm=324.0,
    height_mm=518.0,
    width_px=1200.0,
    height_px=1920.0,
    housing_width_mm=337.0,
    housing_height_mm=528.0,
    unit="mm",
)

#: The two mountings of the Metixel panel, keyed by orientation.
METIXEL_SCREENS: dict[str, Screen] = {
    "landscape": METIXEL_16_10_1920x1200,
    "portrait": METIXEL_16_10_PORTRAIT,
}

SCREENS: dict[str, Screen] = {
    "metixel_16_10_1920x1200": METIXEL_16_10_1920x1200,
    "metixel_16_10_1200x1920": METIXEL_16_10_PORTRAIT,
    "metixel_landscape": METIXEL_16_10_1920x1200,
    "metixel_portrait": METIXEL_16_10_PORTRAIT,
}

# ---------------------------------------------------------------------------
# Framing styles
# ---------------------------------------------------------------------------

# Ring fractions are expressed as a percentage of the **reference dimension** —
# the shorter side of ``screen - 2 x edge_margin`` (spec/"Mat Ring").  For the
# Metixel screen that reference is 320 mm, so gallery's 17.5% is a 56 mm ring.
# The value is independent of mounting orientation, because the reference is the
# shorter side either way up.
#
# Ranges are the product guideline; ``ring`` uses the midpoint of each range.
# ``bottom_ring`` is set only where the style is deliberately bottom-heavy.
#
# NOTE: the ring is the *presentation* default.  ``overflow`` and
# ``ambient_strategy`` are deliberately NOT style properties — they are user
# choices, because the same frame can be presented cropped or filled.


@dataclass(frozen=True)
class Style:
    """A framing style: a ring proportion plus presentation defaults."""

    name: str
    ring: float
    """Mat Ring as a fraction of the reference dimension (the shortest ring)."""

    bottom_ring: float | None = None
    """Bottom ring fraction, for deliberately bottom-heavy styles."""

    guideline: str = ""
    """The percentage range this style is drawn from, for documentation/UI."""

    mental_model: str = ""
    whitespace_enabled: bool = False
    """The style's *suggested* whitespace state.

    A suggestion only: the runtime decides the effective state (see
    :func:`resolve_whitespace`), because whether whitespace is appropriate
    depends on the casing and the presentation, not on the ring alone.
    """

    whitespace_gap: float = 0.0
    whitespace_colour: str = "#ffffff"
    """The band colour.  A toned value reads as a mat reveal rather than paper."""
    shadow: bool = False
    shadow_strength: float = 0.0
    border: bool = False
    border_width: float = 0.0
    mat_colour: str = "warm-white"

    @property
    def is_borderless(self) -> bool:
        return self.ring == 0.0 and not self.bottom_ring


STYLES: dict[str, Style] = {
    "borderless": Style(
        name="borderless",
        ring=0.0,
        guideline="0%",
        mental_model="No mat / full bleed",
    ),
    "modern": Style(
        name="modern",
        ring=0.06,
        guideline="4-8%",
        mental_model="Just enough separation",
        border=True,
        border_width=0.5,
        mat_colour="neutral-white",
    ),
    "classic": Style(
        name="classic",
        ring=0.11,
        guideline="8-14%",
        mental_model="Traditional photographic mount",
    ),
    "gallery": Style(
        name="gallery",
        ring=0.175,
        guideline="14-21%",
        mental_model="Generous fine-art presentation",
    ),
    "museum": Style(
        name="museum",
        ring=0.25,
        guideline="20-31%",
        mental_model="Formal, substantial presentation",
        # A double mat is deferred (spec/"Deferred").  Museum's layered look is
        # a toned whitespace band reading as an inner reveal.  With a virtual
        # mat this is the default; with a physical mat it must be opted into,
        # and in print presentation it is a white paper border instead.
        whitespace_enabled=True,
        whitespace_gap=12.0,
        whitespace_colour="#ece3d2",
        shadow=True,
        shadow_strength=0.15,
    ),
    "floating": Style(
        name="floating",
        ring=0.15,
        guideline="11-20%",
        mental_model="Like gallery, with artwork separation",
        shadow=True,
        shadow_strength=0.25,
        mat_colour="neutral-white",
    ),
    "polaroid": Style(
        name="polaroid",
        ring=0.14,
        bottom_ring=0.28,
        guideline="11-17% sides / 21-35% bottom",
        mental_model="Deliberately bottom-heavy",
    ),
    "custom": Style(
        name="custom",
        ring=0.0,
        guideline="user-defined",
        mental_model="Explicit mm geometry",
    ),
}

# ---------------------------------------------------------------------------
# Overflow presets (NOT framing styles)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverflowPreset:
    """One of the two ways a fixed Mat Window resolves an aspect mismatch."""

    name: str
    style: str
    """The framing style this preset pairs with (borderless)."""

    overflow: Literal["crop", "fill"]
    ambient_strategy: Literal["solid", "blur", "bars"]
    description: str


OVERFLOW_PRESETS: dict[str, OverflowPreset] = {
    "immersive": OverflowPreset(
        name="immersive",
        style="borderless",
        overflow="crop",
        ambient_strategy="bars",
        description="Borderless; the artwork covers the frame, cropping edges.",
    ),
    "immersive_fill": OverflowPreset(
        name="immersive_fill",
        style="borderless",
        overflow="fill",
        ambient_strategy="blur",
        description="Borderless; the artwork is contained and blurred ambient "
        "fill absorbs the residue.",
    ),
}


# ---------------------------------------------------------------------------
# Request builder
# ---------------------------------------------------------------------------


def physical_ring_factor(ring_fraction: float) -> float:
    """Multiplier that makes a **uniform** physical mat match a virtual one.

    The two branches produce different ring-to-artwork proportions for the same
    style fraction ``f``:

    * **virtual** — the artwork is fitted inside ``opening - 2 x f``, so the ring
      is ``f / (1 - 2f)`` of the artwork's **short** dimension.  That ratio is
      what the eye reads, and it is what makes the mat look generous.
    * **physical** — the Mat Window is the whole active area, so a ring of
      ``f x reference`` is only ``f x reference / artwork`` — far smaller,
      because the artwork fills the window rather than being inset by the ring.

    Matching the **ratio** rather than the width gives the multiplier:

        multiplier = 1 / (1 - 2f)

    which takes the base ring ``f x reference`` to
    ``f x reference / (1 - 2f) == reference x f/(1-2f)``, i.e. the same
    proportion of the artwork as the virtual branch produces.

    For ``f = 0.25`` the multiplier is 2.0, so museum's 80 mm virtual ring
    becomes 160 mm uniform — both 50 % of a 320 mm artwork.

    ``f`` must be below 0.5, or the ring would consume the whole opening.
    """
    f = float(ring_fraction)
    if not 0.0 <= f < 0.5:
        raise ValueError(
            f"ring fraction must be in [0, 0.5) to scale for a physical mat, got {ring_fraction!r}"
        )
    return 1.0 / (1.0 - 2.0 * f)


@dataclass(frozen=True)
class WhitespaceChoice:
    """The effective whitespace state for one presentation."""

    enabled: bool
    gap: float
    colour: str
    source: str
    """Why this state was chosen: ``override``, ``casing`` or ``style``."""


#: Presentation styles, which decide whether whitespace means a mount gap or a
#: printed paper border.
#:
#: ``virtual``  a software mat is drawn — whitespace is a mount gap
#: ``print``    the artwork is presented as a print — whitespace is the paper
#: ``matte``    a flat mat, no mount gap
PRESENTATION_STYLES = ("virtual", "print", "matte")

PresentationStyle = Literal["virtual", "print", "matte"]


def resolve_whitespace(
    style: Style | str,
    *,
    has_physical_mat: bool = False,
    presentation: PresentationStyle = "virtual",
    override: bool | None = None,
) -> WhitespaceChoice:
    """Decide the whitespace state for one presentation.

    Whitespace is not simply a style property, because whether it is
    appropriate depends on the casing and the presentation:

    ==================  =====================================================
    Casing / style      Default
    ==================  =====================================================
    physical mat        **off** — a mount gap is only wanted if asked for
    virtual + museum    **on** — museum's toned inner reveal
    virtual + other     off
    print               **on for every style** — the print's white paper border
    ==================  =====================================================

    Priority is ``override`` then the casing/presentation default, so a user
    choice always wins.
    """
    spec = STYLES[style] if isinstance(style, str) else style

    if override is not None:
        enabled, source = bool(override), "override"
    elif presentation == "print":
        enabled, source = True, "casing"
    elif has_physical_mat:
        enabled, source = False, "casing"
    else:
        enabled, source = spec.whitespace_enabled, "style"

    if not enabled:
        return WhitespaceChoice(False, 0.0, spec.whitespace_colour, source)

    gap = spec.whitespace_gap or 12.0
    colour = spec.whitespace_colour if presentation != "print" else "#ffffff"
    return WhitespaceChoice(True, gap, colour, source)


def build_request(
    screen: Screen,
    media: MediaDescriptor,
    style: str = "gallery",
    *,
    physical: bool = False,
    mat_window: Rect | None = None,
    ring: object | None = None,
    mat_window_offset: tuple[float, float] = (0.5, 0.5),
    moulding_width: float = 40.0,
    moulding: Insets | None = None,
    edge_margin: float = 2.0,
    overflow: Literal["crop", "fill"] | None = None,
    ambient_strategy: Literal["solid", "blur", "bars"] | None = None,
    ambient_colour: str | None = None,
    whitespace: bool | None = None,
    whitespace_gap: float | None = None,
    presentation: PresentationStyle = "virtual",
    focal_position: Literal["auto", "center", "manual"] = "auto",
    manual_focal_point: FocalPoint | None = None,
) -> FramingRequest:
    """Resolve a style into an explicit millimetre :class:`FramingRequest`.

    The engine never sees the style name: this function converts the style's
    fraction into a millimetre ring using the screen's reference dimension.

    Parameters
    ----------
    physical
        Select the physical branch.  ``mat_window`` then supplies the fixed
        Mat Window; if omitted it defaults to ``screen - 2 x edge_margin``,
        which is centred.  ``ring`` defaults to the style's ring, and the Frame
        Opening grows around the window.
    ring
        Any of: ``None`` (use the style fraction), a scalar, an
        :class:`~metixel.framing_engine.Insets`, or a ``(left, right, top, bottom)``
        tuple in millimetres.  Explicit values bypass the style fraction.
    mat_window_offset
        Virtual branch only.  A fraction of the slack; ignored (and rejected)
        in the physical branch, where the offset is achieved physically.
    """
    if style not in STYLES:
        raise ValueError(f"Unknown style {style!r}; expected one of {sorted(STYLES)}")
    spec = STYLES[style]

    # -- ring resolution --------------------------------------------------
    from .framing_engine import ring_target_mm  # local import avoids cycle noise

    if ring is None:
        if spec.bottom_ring is not None:
            resolved = ring_target_mm(screen, spec.ring, edge_margin, spec.bottom_ring)
        else:
            resolved = ring_target_mm(screen, spec.ring, edge_margin)

        # A physical mat is the whole active area, so its ring is far thinner
        # than the virtual mat's for the same style.  Scale it to match the
        # virtual ring-to-artwork ratio.  See ``physical_ring_factor``.
        #
        # Scaling all four sides by the same factor preserves the style's
        # asymmetry, so polaroid stays bottom-heavy.
        if physical:
            k = physical_ring_factor(spec.ring)
            resolved = Insets(*(v * k for v in resolved))
    elif isinstance(ring, Insets):
        resolved = ring
    elif isinstance(ring, (tuple, list)):
        if len(ring) != 4:
            raise ValueError("ring tuple must be (left, right, top, bottom) in mm")
        resolved = Insets(*(float(v) for v in ring))
    else:
        resolved = Insets.uniform(float(ring))  # type: ignore[arg-type]

    # -- branch-specific defaults ----------------------------------------
    resolved_window = mat_window
    if physical and resolved_window is None:
        resolved_window = screen.inset(edge_margin)

    # -- whitespace -------------------------------------------------------
    # Not simply a style property: the casing and presentation decide the
    # default, and an explicit `whitespace` argument always wins.
    choice = resolve_whitespace(
        spec,
        has_physical_mat=physical,
        presentation=presentation,
        override=whitespace,
    )
    ws_gap = choice.gap if whitespace_gap is None else float(whitespace_gap)
    if not choice.enabled:
        ws_gap = 0.0

    # -- overflow and ambient appearance (user choices) -------------------
    # Both are independent of the style, because the same frame can be
    # presented cropped or filled.  Defaults: fill, with the ambient look
    # implied by the presentation.
    chosen_overflow = overflow or "fill"
    # The default strategy is keyed to the overflow: a cropped image has no
    # residue to absorb, so "bars" is the honest default there, whereas a
    # contained one wants a flat colour behind it.  A caller that sets the
    # strategy explicitly (the slideshow does, from config) wins.
    strategy = ambient_strategy or ("bars" if chosen_overflow == "crop" else "solid")

    # A white border and ambient fill must never appear together: two competing
    # borders around one image reads as a mistake.  Whitespace therefore forces
    # the crop presentation.
    if choice.enabled and chosen_overflow == "fill":
        chosen_overflow = "crop"

    # -- offset -----------------------------------------------------------
    offset = mat_window_offset
    if physical and mat_window is not None:
        # A physical offset is produced by cutting the mat and mounting the
        # screen, so it is expressed by the rect, not by an offset parameter.
        offset = (0.5, 0.5)

    return FramingRequest(
        screen=screen,
        media=media,
        moulding_width=moulding_width,
        moulding=moulding,
        mat_window=resolved_window,
        ring=resolved,
        edge_margin=edge_margin,
        mat_window_offset=offset,
        whitespace=WhitespaceSpec(enabled=choice.enabled, gap=ws_gap, colour=choice.colour),
        ambient=AmbientFillSpec(
            strategy=strategy,
            colour=ambient_colour or AmbientFillSpec().colour,
            darken=0.35,
        ),
        overflow=chosen_overflow,
        focal_position=focal_position,
        manual_focal_point=manual_focal_point,
        effects=Effects(
            shadow=spec.shadow,
            shadow_strength=spec.shadow_strength,
            border=spec.border,
            border_width=spec.border_width,
        ),
        mat_colour=spec.mat_colour,
    )


def build_request_for_preset(
    screen: Screen, media: MediaDescriptor, preset: str, **kwargs: Any
) -> FramingRequest:
    """Build a request from an overflow preset (``immersive`` etc.).

    A convenience alias for *borderless plus an overflow behaviour*.  The same
    result is reachable directly with ``style="borderless"`` and an explicit
    ``overflow``, which is the preferred route for new code — the presets exist
    so the two familiar names keep working.
    """
    if preset not in OVERFLOW_PRESETS:
        raise ValueError(f"Unknown overflow preset {preset!r}; expected {sorted(OVERFLOW_PRESETS)}")
    entry = OVERFLOW_PRESETS[preset]
    kwargs.setdefault("overflow", entry.overflow)
    kwargs.setdefault("ambient_strategy", entry.ambient_strategy)
    return build_request(screen, media, entry.style, **kwargs)


def style_table() -> dict[str, dict[str, Any]]:
    """A UI-friendly summary of the styles, with the Metixel mm equivalent.

    Handy for a settings page: shows the guideline, the mental model, and what
    the proportion means in millimetres on the default screen.
    """
    screen = METIXEL_16_10_1920x1200
    reference = screen.reference_dimension(2.0)
    table: dict[str, dict[str, Any]] = {}
    for name, spec in STYLES.items():
        row: dict[str, Any] = {
            "guideline": spec.guideline,
            "mental_model": spec.mental_model,
            "fraction": spec.ring,
            "ring_mm": round(spec.ring * reference, 2),
        }
        if spec.bottom_ring is not None:
            row["bottom_fraction"] = spec.bottom_ring
            row["bottom_ring_mm"] = round(spec.bottom_ring * reference, 2)
        table[name] = row
    return table


def screen_table() -> dict[str, dict[str, Any]]:
    """Screen presets: active area, panel, and the rebate a frame must provide.

    ``minimum_rebate`` is the overlap needed to hide the non-screen area when
    the Frame Opening matches the active area; the actual requirement per
    configuration is ``FramingResult.frame.required_rebate``.

    Both are **fit checks, not drawn geometry.**  They exist so the UI and the
    documentation can tell a customer which real frames will hold the panel.
    Nothing renders them; do not add either to the visualisation as a component.
    """
    table: dict[str, dict[str, Any]] = {}
    for name, screen in SCREENS.items():
        table[name] = {
            "active_area": [screen.width_mm, screen.height_mm],
            "panel": list(screen.panel_size),
            "non_screen": [
                screen.bezel.horizontal,
                screen.bezel.vertical,
            ],
            "minimum_rebate": list(screen.minimum_rebate.as_dict().values()),
            "pixels": [screen.width_px, screen.height_px],
            "aspect": round(screen.aspect, 3),
        }
    return table


__all__ = [
    "Screen",
    "METIXEL_16_10_1920x1200",
    "SCREENS",
    "Style",
    "STYLES",
    "OverflowPreset",
    "OVERFLOW_PRESETS",
    "build_request",
    "build_request_for_preset",
    "resolve_whitespace",
    "WhitespaceChoice",
    "PRESENTATION_STYLES",
    "style_table",
    "screen_table",
    "METIXEL_MINIMUM_REBATE",
    "METIXEL_16_10_PORTRAIT",
    "METIXEL_SCREENS",
]
