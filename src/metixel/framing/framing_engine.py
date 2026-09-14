# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Metixel Adaptive Framing Engine — pure geometry / logic layer.

Specification: ``docs/geometry-model.md``.  Section references in this module
(``spec/<name>``) point at headings in that document, which is the authority.

This module contains **no UI code** and **no media-player / image-loading
code**.  All calculations are pure, deterministic arithmetic over dataclasses.

Scope (spec/"Framing style and overflow are separate axes"):
    This engine is **dimensions only**.  It receives explicit millimetre
    geometry and computes rectangles.  It holds no style table and makes no
    style decisions — that is the job of :mod:`framing_templates`.

Core model (spec/"The core model"):
    The screen is the constant; the Mat Window and Mat Ring are derived from it.

        Screen        518 x 324 mm
        Mat Window    screen - 2 x edge_margin      (default; overrideable)
        Mat Ring      given mm (a style proportion)
        Frame Opening Mat Window + 2 x Mat Ring     (physical)
                      screen - 2 x edge_margin      (virtual)
        Frame Outer   Frame Opening + 2 x moulding

Two branches (spec/"The two branches") share one placement pipeline; they differ
only in how the **Mat Window size** is found:

    physical   Mat Window is GIVEN, so the Frame Opening grows with the ring
    virtual    Frame Opening is GIVEN, and the Mat Window is cut to the artwork

All sizes are millimetres and all arithmetic is performed in millimetres
(spec/"Units").  Normalised views are produced only at the boundary, against a
caller-chosen reference.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# Aspect classification thresholds (spec/"Orientation").
PORTRAIT_THRESHOLD = 0.87
SQUARE_MAX = 1.15
PANORAMA_THRESHOLD = 2.0

# Focal-point movement bounds, as a fraction of the available slack.
MAX_FOCAL_SHIFT_X = 0.05
MAX_FOCAL_SHIFT_Y = 0.05

# A style ring must leave a positive inner rect.  This epsilon guards the
# degenerate case where the ring consumes the whole Frame Opening.
MIN_INNER_MM = 1e-6

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

MediaType = Literal["image", "video"]
Orientation = Literal["portrait", "landscape", "square", "panorama"]
FitMode = Literal["contain", "cover"]
FocalPosition = Literal["auto", "center", "manual"]
Overflow = Literal["crop", "fill"]
AmbientStrategy = Literal["solid", "blur", "bars"]
Unit = Literal["mm", "cm", "in", "px"]


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp a scalar into ``[lo, hi]``."""
    return lo if value < lo else (hi if value > hi else value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_aspect(aspect: float) -> Orientation:
    """Classify an aspect ratio (width / height) into an orientation.

    Used for the screen, artwork, Frame Opening and Mat Window, so any of them
    can be reported as landscape / portrait / square / panorama.
    """
    ar = float(aspect)
    _require(ar > 0, f"aspect must be > 0, got {aspect!r}")
    if ar < PORTRAIT_THRESHOLD:
        return "portrait"
    if ar <= SQUARE_MAX:
        return "square"
    if ar < PANORAMA_THRESHOLD:
        return "landscape"
    return "panorama"


# ---------------------------------------------------------------------------
# Small value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in millimetres, in screen coordinates.

    The origin ``(0, 0)`` is the top-left corner of the screen, with ``y``
    increasing downwards (the same convention the v1 engine and the visualiser
    use).
    """

    x: float
    y: float
    width: float
    height: float

    # -- derived edges ----------------------------------------------------

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.width / 2.0, self.y + self.height / 2.0

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else math.inf

    @property
    def area(self) -> float:
        return self.width * self.height

    # -- construction ----------------------------------------------------

    def inset(self, left: float, right: float, top: float, bottom: float) -> Rect:
        """Shrink by per-side amounts."""
        return Rect(
            self.x + left,
            self.y + top,
            self.width - left - right,
            self.height - top - bottom,
        )

    def expand(self, left: float, right: float, top: float, bottom: float) -> Rect:
        """Grow by per-side amounts."""
        return self.inset(-left, -right, -top, -bottom)

    def centred_at(self, cx: float, cy: float) -> Rect:
        """A copy of this rect recentred on ``(cx, cy)``."""
        return Rect(cx - self.width / 2.0, cy - self.height / 2.0, self.width, self.height)

    def shifted(self, dx: float, dy: float) -> Rect:
        return Rect(self.x + dx, self.y + dy, self.width, self.height)

    # -- predicates ------------------------------------------------------

    def contains(self, other: Rect, tol: float = 1e-6) -> bool:
        return (
            other.x >= self.x - tol
            and other.y >= self.y - tol
            and other.right <= self.right + tol
            and other.bottom <= self.bottom + tol
        )

    def is_positive(self, tol: float = MIN_INNER_MM) -> bool:
        return self.width > tol and self.height > tol

    # -- views -----------------------------------------------------------

    def normalised(self, reference: Rect) -> dict[str, float]:
        """Normalise this rect against ``reference`` (spec/"Output views").

        A uniform mm band is *not* a uniform normalised band on a non-square
        reference, which is exactly why geometry is computed in mm.
        """
        _require(
            reference.width > 0 and reference.height > 0,
            "normalisation reference must have positive size",
        )
        return {
            "x": (self.x - reference.x) / reference.width,
            "y": (self.y - reference.y) / reference.height,
            "width": self.width / reference.width,
            "height": self.height / reference.height,
        }


@dataclass(frozen=True)
class Insets:
    """Per-side millimetre amounts."""

    left: float = 0.0
    right: float = 0.0
    top: float = 0.0
    bottom: float = 0.0

    @classmethod
    def uniform(cls, value: float) -> Insets:
        return cls(value, value, value, value)

    def __iter__(self) -> Iterator[float]:
        return iter((self.left, self.right, self.top, self.bottom))

    @property
    def horizontal(self) -> float:
        return self.left + self.right

    @property
    def vertical(self) -> float:
        return self.top + self.bottom

    @property
    def minimum(self) -> float:
        return min(self.left, self.right, self.top, self.bottom)

    def as_dict(self) -> dict[str, float]:
        return {
            "left": self.left,
            "right": self.right,
            "top": self.top,
            "bottom": self.bottom,
        }


SideInput = float | Insets | tuple[float, float, float, float]


def _resolve_insets(value: SideInput) -> Insets:
    """Accept a scalar (uniform), an :class:`Insets`, or a 4-tuple."""
    if isinstance(value, Insets):
        return value
    if isinstance(value, (tuple, list)):
        _require(
            len(value) == 4,
            f"ring tuple must be (left, right, top, bottom) in mm, got {value!r}",
        )
        return Insets(*(float(v) for v in value))
    return Insets.uniform(float(value))


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


@dataclass
class FocalPoint:
    """A focal point in normalised image coordinates (0..1)."""

    x: float = 0.5
    y: float = 0.5

    def __post_init__(self) -> None:
        self.x = _clamp(float(self.x))
        self.y = _clamp(float(self.y))


@dataclass
class Face:
    """A detected face rectangle in normalised image coordinates (0..1)."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        _require(
            float(self.width) >= 0 and float(self.height) >= 0,
            "Face width/height must be >= 0",
        )
        self.x = _clamp(float(self.x))
        self.y = _clamp(float(self.y))
        self.width = _clamp(float(self.width))
        self.height = _clamp(float(self.height))


@dataclass
class MediaDescriptor:
    """The artwork being framed.

    ``width`` / ``height`` are pixel dimensions; only the ratio matters, and
    they are assumed to be square pixels so the ratio is directly comparable
    with millimetre geometry.
    """

    width: float
    height: float
    type: MediaType = "image"
    focal_point: FocalPoint | None = None
    faces: list[Face] | None = None

    def __post_init__(self) -> None:
        _require(
            float(self.width) > 0 and float(self.height) > 0,
            f"MediaDescriptor width/height must be > 0, got {self.width!r} x {self.height!r}",
        )
        _require(
            self.type in ("image", "video"),
            f"MediaDescriptor.type must be 'image' or 'video', got {self.type!r}",
        )

    @property
    def aspect(self) -> float:
        return float(self.width) / float(self.height)


@dataclass
class Screen:
    """The panel (spec/"Metixel screen").

    Two sizes are tracked:

    ``width_mm`` / ``height_mm`` — the **active area**, the lit region.  Every
        length in the model is expressed against this, and it is where pixels
        exist.  Nothing may be drawn outside it.

    ``housing_width_mm`` / ``housing_height_mm`` — the whole panel including
        the non-screen border.  A frame must be at least this large to hold the
        panel, so it defines the **minimum rebate** required to hide the panel
        edge when mounting without a mat.  It defaults to the active area.

    ``width_px`` / ``height_px`` describe the active area.
    """

    width_mm: float
    height_mm: float
    width_px: float = 0.0
    height_px: float = 0.0
    housing_width_mm: float | None = None
    housing_height_mm: float | None = None
    unit: str = "mm"

    def __post_init__(self) -> None:
        _require(
            float(self.width_mm) > 0 and float(self.height_mm) > 0,
            f"Screen width_mm/height_mm must be > 0, got {self.width_mm!r} x {self.height_mm!r}",
        )
        _require(
            float(self.width_px) >= 0 and float(self.height_px) >= 0,
            "Screen width_px/height_px must be >= 0",
        )
        if self.housing_width_mm is None:
            self.housing_width_mm = float(self.width_mm)
        if self.housing_height_mm is None:
            self.housing_height_mm = float(self.height_mm)
        _require(
            float(self.housing_width_mm) >= float(self.width_mm)
            and float(self.housing_height_mm) >= float(self.height_mm),
            "Screen housing must be at least the active area, got "
            f"{self.housing_width_mm!r} x {self.housing_height_mm!r} for an "
            f"active area of {self.width_mm!r} x {self.height_mm!r}",
        )

    # -- regions ---------------------------------------------------------

    @property
    def rect(self) -> Rect:
        """The **active area** (the lit region)."""
        return Rect(0.0, 0.0, float(self.width_mm), float(self.height_mm))

    @property
    def housing_rect(self) -> Rect:
        """The whole panel, centred on the active area (so it may be negative)."""
        w = float(self.housing_width_mm or self.width_mm)
        h = float(self.housing_height_mm or self.height_mm)
        return Rect(
            (float(self.width_mm) - w) / 2.0,
            (float(self.height_mm) - h) / 2.0,
            w,
            h,
        )

    @property
    def bezel(self) -> Insets:
        """The non-screen border, per side.

        This is also the **minimum rebate** needed to hide the panel edge when
        mounting without a mat: the frame must overlap the panel by at least
        this much on each side.
        """
        x = (float(self.housing_width_mm or self.width_mm) - self.width_mm) / 2.0
        y = (float(self.housing_height_mm or self.height_mm) - self.height_mm) / 2.0
        return Insets(x, x, y, y)

    @property
    def has_bezel(self) -> bool:
        b = self.bezel
        return b.horizontal > 0 or b.vertical > 0

    @property
    def panel_size(self) -> tuple[float, float]:
        """The overall panel size, including the non-screen border."""
        return (
            float(self.housing_width_mm or self.width_mm),
            float(self.housing_height_mm or self.height_mm),
        )

    @property
    def minimum_rebate(self) -> Insets:
        """The smallest rebate that hides the panel edge, per side.

        A **fit check, not a drawn element**.  The rebate is the overlap a real
        frame must provide to cover the non-screen border of the panel; this
        property reports the figure so a customer can be told the minimum a
        frame needs.  Nothing renders it — see :attr:`required_rebate`.
        """
        return self.bezel

    def required_rebate(self, opening: Rect) -> Insets:
        """Rebate needed for ``opening`` to cover the panel edge, per side.

        A **fit check, not a drawn element.**  Nothing in the render path draws
        or applies this value; it exists to answer "will the panel fit inside a
        real frame, and how much must that frame overlap the panel?" for the
        Remote UI / documentation.

        The physical reasoning: the panel sits *behind* the frame, so the frame
        must overlap it by ``(panel - opening) / 2`` per side.  Nothing is
        required once the opening is at least as large as the panel, hence the
        clamp at zero.

        On the PHYSICAL branch this is a genuine constraint and the engine
        enforces that the rebate cannot exceed the moulding width (a rebate wider
        than the frame is not buildable).  On the VIRTUAL branch — where the mat
        and artwork are drawn in software — there is no physical frame doing any
        hiding at all, so the value is guidance only and must never be painted.
        """
        dx = max(0.0, (self.panel_size[0] - opening.width) / 2.0)
        dy = max(0.0, (self.panel_size[1] - opening.height) / 2.0)
        return Insets(dx, dx, dy, dy)

    # -- metrics ---------------------------------------------------------

    @property
    def aspect(self) -> float:
        return float(self.width_mm) / float(self.height_mm)

    @property
    def area(self) -> float:
        return float(self.width_mm) * float(self.height_mm)

    @property
    def mm_per_px(self) -> float | None:
        """Millimetres per pixel, when pixel dimensions are known."""
        if self.width_px > 0 and self.height_px > 0:
            return float(self.width_mm) / float(self.width_px)
        return None

    @property
    def px_per_mm(self) -> tuple[float | None, float | None]:
        """Pixels per millimetre per axis, so non-square pixels stay correct."""
        wide = float(self.width_px) / float(self.width_mm) if self.width_px > 0 else None
        high = float(self.height_px) / float(self.height_mm) if self.height_px > 0 else None
        return wide, high

    def inset(self, edge_margin: float) -> Rect:
        """The default rect inset from the screen edge by ``edge_margin``."""
        m = float(edge_margin)
        _require(m >= 0, f"edge_margin must be >= 0, got {edge_margin!r}")
        _require(
            m * 2 < self.width_mm and m * 2 < self.height_mm,
            f"edge_margin {edge_margin!r} consumes the screen",
        )
        return Rect(m, m, self.width_mm - 2 * m, self.height_mm - 2 * m)

    def reference_dimension(self, edge_margin: float) -> float:
        """The style reference dimension (spec/"Mat Ring").

        The **shorter side** of ``screen - 2 x edge_margin``.  Taking the
        shorter side keeps a style percentage meaningful in both orientations:

            400 x 600   -> 400
            600 x 400   -> 400
            1000 x 1500 -> 1000
            514 x 320   -> 320
        """
        rect = self.inset(edge_margin)
        return min(rect.width, rect.height)


@dataclass
class WhitespaceSpec:
    """An intentional band around the artwork (spec/"Whitespace").

    A framing layer, not part of the print.  The band is uniform and its width
    is the shortest it can be, widening only if the Mat Window leaves room.
    """

    enabled: bool = False
    gap: float = 0.0
    colour: str = "#ffffff"

    def __post_init__(self) -> None:
        _require(float(self.gap) >= 0, f"whitespace gap must be >= 0, got {self.gap!r}")


@dataclass
class AmbientFillSpec:
    """Appearance of the derived ambient fill (spec/"Ambient fill").

    There is deliberately **no** ``enabled`` flag: whether ambient fill exists
    is derived from the geometry.  Only its look is configured here.
    """

    strategy: AmbientStrategy = "blur"
    colour: str = "#101014"
    blur_radius: float = 24.0
    darken: float = 0.35

    def __post_init__(self) -> None:
        _require(
            self.strategy in ("solid", "blur", "bars"),
            f"ambient strategy must be 'solid', 'blur' or 'bars', got {self.strategy!r}",
        )
        _require(float(self.darken) >= 0, "darken must be >= 0")
        _require(float(self.blur_radius) >= 0, "blur_radius must be >= 0")


@dataclass
class Effects:
    """Non-geometric renderer flags."""

    shadow: bool = False
    shadow_strength: float = 0.0
    border: bool = False
    border_width: float = 0.0


@dataclass
class FramingRequest:
    """Everything the engine needs, in explicit millimetres.

    Exactly one of the two branches is selected:

    ``mat_window is None``
        **Virtual** branch.  The Frame Opening is derived from the screen and
        the Mat Window is cut to the artwork's aspect.
    ``mat_window`` set
        **Physical** branch.  The Mat Window is given as an absolute rect in
        screen coordinates, and the Frame Opening grows around it.  Because the
        offset is achieved physically, ``mat_window_offset`` does not apply.
    """

    screen: Screen
    media: MediaDescriptor

    # Frame
    moulding_width: float = 40.0
    moulding: Insets | None = None

    # Branch selection
    mat_window: Rect | None = None
    ring: SideInput = 0.0
    edge_margin: float = 2.0

    # Placement within the ring
    mat_window_offset: tuple[float, float] = (0.5, 0.5)

    # Overlay
    whitespace: WhitespaceSpec = field(default_factory=WhitespaceSpec)
    ambient: AmbientFillSpec = field(default_factory=AmbientFillSpec)
    overflow: Overflow = "fill"

    # Focal behaviour
    focal_position: FocalPosition = "auto"
    manual_focal_point: FocalPoint | None = None

    effects: Effects = field(default_factory=Effects)
    mat_colour: str = "auto"

    def __post_init__(self) -> None:
        _require(
            float(self.moulding_width) >= 0,
            f"moulding_width must be >= 0, got {self.moulding_width!r}",
        )
        _require(
            self.overflow in ("crop", "fill"),
            f"overflow must be 'crop' or 'fill', got {self.overflow!r}",
        )
        _require(
            self.focal_position in ("auto", "center", "manual"),
            f"focal_position must be 'auto', 'center' or 'manual', got {self.focal_position!r}",
        )
        _require(
            len(self.mat_window_offset) == 2,
            "mat_window_offset must be a (x, y) pair",
        )
        ox, oy = (float(self.mat_window_offset[0]), float(self.mat_window_offset[1]))
        _require(
            0.0 <= ox <= 1.0 and 0.0 <= oy <= 1.0,
            f"mat_window_offset must be within [0, 1] per axis, got {self.mat_window_offset!r}",
        )
        if self.mat_window is not None and (ox, oy) != (0.5, 0.5):
            raise ValueError(
                "mat_window_offset does not apply to a physical mat "
                "(spec/'Mat window offset'): the offset is achieved by cutting "
                "the mat and mounting the screen, so it is expressed by the "
                "mat_window rect itself. Pass a centred offset, or move the "
                "mat_window rect instead."
            )
        # A whitespace band is a deliberate white frame around the artwork.
        # Combining it with ambient fill reads as a mistake: two competing
        # "borders" around the same image. Whitespace therefore implies crop.
        if self.whitespace.enabled and self.overflow == "fill":
            raise ValueError(
                "whitespace requires overflow='crop' "
                "(spec/'Whitespace'): a white border and ambient fill must "
                "never appear together. Either set overflow='crop', or disable "
                "whitespace to use fill."
            )

    @property
    def is_physical(self) -> bool:
        return self.mat_window is not None


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass
class FrameResult:
    """The moulding.  ``moulding`` is derived, never persisted upstream."""

    opening: Rect
    outer: Rect
    moulding: Insets
    orientation: Orientation
    required_rebate: Insets = field(default_factory=lambda: Insets(0.0, 0.0, 0.0, 0.0))
    """Frame overlap needed to hide the panel edge — a FIT CHECK, not geometry.

    Reported for the Remote UI and documentation so a customer can pick a real
    frame that will hold the panel.  It is deliberately NOT drawn: there is no
    render step for it, and it must not be added to the visualisation as though
    it were part of the composition.  See ``Screen.required_rebate``.
    """
    """How far the frame must overlap the panel to hide the non-screen area.

    Must not exceed the moulding width on any side, which would be physically
    impossible.
    """


@dataclass
class MatResult:
    """The mat.  ``ring`` is the band between window and opening."""

    type: Literal["physical", "virtual"]
    outer: Rect
    window: Rect
    ring: Insets
    offset: tuple[float, float]
    colour: str
    orientation: Orientation


@dataclass
class WhitespaceResult:
    enabled: bool
    authored_gap: float
    applied_gap: Insets
    """The white band actually drawn, per side: ``ws_outer`` inset to the
    artwork.  Equals ``authored_gap`` unless it had to compress."""

    outer: Rect
    residual: Insets = field(default_factory=lambda: Insets(0.0, 0.0, 0.0, 0.0))
    """The gap between the Mat Window and ``ws_outer``, per side.

    This is ambient fill, **not** whitespace.  It is non-zero only when the
    Mat Window is larger than the artwork plus its whitespace band.
    """

    colour: str = "#ffffff"


@dataclass
class AmbientFillResult:
    """Derived, never configured (spec/"Ambient fill")."""

    present: bool
    strategy: AmbientStrategy
    region: Rect
    bars: list[Rect]
    colour: str
    blur_radius: float
    darken: float


@dataclass
class ArtworkResult:
    bounds: Rect
    presentation: Rect
    fit: FitMode
    focal_point: FocalPoint | None
    orientation: Orientation


@dataclass
class Metrics:
    screen_utilisation: float
    overlap: Insets
    screen_aspect: float
    artwork_aspect: float
    opening_aspect: float
    window_aspect: float


@dataclass
class FramingResult:
    """Complete geometry.  Every group is reported in both branches."""

    screen: Screen
    branch: Literal["physical", "virtual"]
    overflow: Overflow
    frame: FrameResult
    mat: MatResult
    whitespace: WhitespaceResult
    ambient_fill: AmbientFillResult
    artwork: ArtworkResult
    metrics: Metrics
    effects: Effects

    # -- views -----------------------------------------------------------

    def display_relative(self) -> dict[str, dict[str, float]]:
        """Normalised to the **screen** (Metixel Software view)."""
        return self._normalised_views(self.screen.rect)

    def frame_relative(self) -> dict[str, dict[str, float]]:
        """Normalised to the **Frame Outer** (website mockup view)."""
        return self._normalised_views(self.frame.outer)

    def _normalised_views(self, reference: Rect) -> dict[str, dict[str, float]]:
        return {
            "frame_outer": self.frame.outer.normalised(reference),
            "frame_opening": self.frame.opening.normalised(reference),
            "mat_window": self.mat.window.normalised(reference),
            "whitespace_outer": self.whitespace.outer.normalised(reference),
            "artwork": self.artwork.bounds.normalised(reference),
            "ambient_fill": self.ambient_fill.region.normalised(reference),
        }

    def to_px(self) -> dict[str, dict[str, float]]:
        """Screen-relative millimetre geometry converted to pixels.

        Each rect is scaled by its own axis' factor, so a square artwork stays
        square even if the panel's pixels are not perfectly square.
        """
        sx, sy = self.screen.px_per_mm
        _require(
            sx is not None and sy is not None,
            "to_px() requires Screen pixel dimensions",
        )
        assert sx is not None and sy is not None

        def convert(rect: Rect) -> dict[str, float]:
            return {
                "x": rect.x * sx,
                "y": rect.y * sy,
                "width": rect.width * sx,
                "height": rect.height * sy,
            }

        return {
            "frame_outer": convert(self.frame.outer),
            "frame_opening": convert(self.frame.opening),
            "mat_window": convert(self.mat.window),
            "whitespace_outer": convert(self.whitespace.outer),
            "artwork": convert(self.artwork.bounds),
            "ambient_fill": convert(self.ambient_fill.region),
        }

    def to_spec(self, unit: Unit = "mm") -> dict[str, object]:
        """The framer cut list (spec/"Output views").

        Lengths are converted from the internal millimetres; ratios and flags
        are passed through unchanged.
        """
        divisors = {"mm": 1.0, "cm": 10.0, "in": 25.4}
        if unit == "px":
            sx, sy = self.screen.px_per_mm
            _require(sx is not None, "unit='px' requires Screen pixel dimensions")
            assert sx is not None and sy is not None

            def length(value_mm: float) -> float:
                return round(value_mm * sx, 2)

            def size(rect: Rect) -> list[float]:
                return [round(rect.width * sx, 2), round(rect.height * sy, 2)]
        else:
            _require(
                unit in divisors,
                f"unit must be one of mm, cm, in, px; got {unit!r}",
            )
            divisor = divisors[unit]

            def length(value_mm: float) -> float:
                return round(value_mm / divisor, 2)

            def size(rect: Rect) -> list[float]:
                return [length(rect.width), length(rect.height)]

        panel_w, panel_h = self.screen.panel_size
        return {
            "unit": unit,
            "screen": size(self.screen.rect),
            "panel": [length(panel_w), length(panel_h)],
            "frame_outer": size(self.frame.outer),
            "frame_opening": size(self.frame.opening),
            "moulding": {k: length(v) for k, v in self.frame.moulding.as_dict().items()},
            "required_rebate": {
                k: length(v) for k, v in self.frame.required_rebate.as_dict().items()
            },
            "mat_type": self.mat.type,
            "mat_window": size(self.mat.window),
            "mat_ring": {k: length(v) for k, v in self.mat.ring.as_dict().items()},
            "whitespace_gap": length(self.whitespace.authored_gap),
            "artwork": size(self.artwork.bounds),
            "artwork_fit": self.artwork.fit,
            "ambient_fill_present": self.ambient_fill.present,
            "ambient_strategy": self.ambient_fill.strategy,
            "screen_utilisation": round(self.metrics.screen_utilisation, 4),
            "overlap": {k: length(v) for k, v in self.metrics.overlap.as_dict().items()},
        }


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _contain(aspect: float, frame: Rect) -> Rect:
    """Fit a rectangle of ``aspect`` inside ``frame``, centred.

    In millimetres this is a direct comparison — no pixel-space compensation is
    needed, which is why the v1 ``display_aspect`` parameter is gone.
    """
    _require(frame.is_positive(), "available frame must have positive area")
    if aspect >= frame.aspect:
        width = frame.width
        height = frame.width / aspect
    else:
        height = frame.height
        width = frame.height * aspect
    cx, cy = frame.centre
    return Rect(cx - width / 2.0, cy - height / 2.0, width, height)


def _face_centroid(faces: list[Face]) -> FocalPoint:
    """Area-weighted centroid of detected faces."""
    total = 0.0
    cx = cy = 0.0
    for face in faces:
        area = max(face.width * face.height, 1e-9)
        cx += area * (face.x + face.width / 2.0)
        cy += area * (face.y + face.height / 2.0)
        total += area
    if total <= 0:
        return FocalPoint(0.5, 0.5)
    return FocalPoint(cx / total, cy / total)


def _gap_insets(box: Rect, gap: float) -> Insets:
    """The whitespace band to apply inside ``box``, compressed to fit.

    The band is normally ``gap`` on all four sides, but it is squeezed when the
    box is too small to hold it — which happens with a small physical Mat
    Window.
    """
    if gap <= 0.0:
        return Insets(0.0, 0.0, 0.0, 0.0)
    gx = min(gap, max(0.0, box.width / 2.0 - MIN_INNER_MM))
    gy = min(gap, max(0.0, box.height / 2.0 - MIN_INNER_MM))
    return Insets(gx, gx, gy, gy)


def _residual(inner: Rect, outer: Rect) -> Insets:
    """The gap between ``outer`` and ``inner``, per side, clamped at zero.

    Used for the ambient residue between the Mat Window and the whitespace
    presentation, and for the white band itself.
    """
    return Insets(
        max(0.0, inner.x - outer.x),
        max(0.0, outer.right - inner.right),
        max(0.0, inner.y - outer.y),
        max(0.0, outer.bottom - inner.bottom),
    )


def _resolve_focal_point(media: MediaDescriptor, request: FramingRequest) -> FocalPoint | None:
    """Resolve the effective focal point.

    Priority: manual -> media-provided -> face centroid -> geometric centre.
    ``focal_position == 'center'`` forces the centre.
    """
    if request.focal_position == "center":
        return FocalPoint(0.5, 0.5)
    if request.focal_position == "manual" and request.manual_focal_point is not None:
        return FocalPoint(request.manual_focal_point.x, request.manual_focal_point.y)
    if media.focal_point is not None:
        return FocalPoint(media.focal_point.x, media.focal_point.y)
    if media.faces:
        return _face_centroid(media.faces)
    return FocalPoint(0.5, 0.5)


def _cover_crop_fraction(aspect: float, frame: Rect) -> float:
    """Fraction of the scaled artwork that would be cropped to cover ``frame``."""
    oa = frame.aspect
    if aspect >= oa:
        return 1.0 - oa / aspect
    return 1.0 - aspect / oa


# ---------------------------------------------------------------------------
# Style resolution (the engine consumes mm; it never invents a style)
# ---------------------------------------------------------------------------


def ring_target_mm(
    screen: Screen,
    fraction: float,
    edge_margin: float = 2.0,
    bottom_fraction: float | None = None,
) -> Insets:
    """Convert a style fraction into a Mat Ring width in millimetres.

    ``ring = fraction x reference_dimension`` (spec/"Mat Ring"), where the
    reference dimension is the **shorter side** of ``screen - 2 x edge_margin``.

    ``bottom_fraction`` optionally overrides the bottom ring, which is how a
    deliberately bottom-heavy style such as polaroid is expressed.
    """
    _require(float(fraction) >= 0, f"ring fraction must be >= 0, got {fraction!r}")
    reference = screen.reference_dimension(edge_margin)
    base = float(fraction) * reference
    if bottom_fraction is None:
        return Insets.uniform(base)
    _require(
        float(bottom_fraction) >= 0,
        f"bottom ring fraction must be >= 0, got {bottom_fraction!r}",
    )
    return Insets(base, base, base, float(bottom_fraction) * reference)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def calculate_framing(request: FramingRequest) -> FramingResult:
    """Compute the complete framing geometry for ``request``.

    Pure and deterministic: identical requests produce identical results.  No
    randomness, no time dependence, no I/O.
    """
    screen = request.screen
    media = request.media
    screen_rect = screen.rect
    moulding = (
        request.moulding
        if request.moulding is not None
        else Insets.uniform(float(request.moulding_width))
    )
    for value in moulding:
        _require(value >= 0, "moulding widths must be >= 0")

    ring = _resolve_insets(request.ring)
    for value in ring:
        _require(value >= 0, "Mat Ring widths must be >= 0")

    has_mat = ring.horizontal > 0.0 or ring.vertical > 0.0

    # -- 1. Resolve the Mat Window ---------------------------------------
    # The branches differ ONLY here: who is fixed, and who is derived.
    if request.is_physical:
        window = request.mat_window
        assert window is not None
        _require(
            window.is_positive(),
            f"Mat Window must have positive size, got {window.width!r} x {window.height!r} mm",
        )
        _require(
            screen_rect.contains(window),
            "Mat Window must lie within the screen "
            f"(spec/'Constraints'): {window} vs screen {screen_rect}",
        )
        # The board is cut once, so the ring is uniform and the Frame Opening
        # grows around the fixed window.
        opening = window.expand(ring.left, ring.right, ring.top, ring.bottom)
        branch: Literal["physical", "virtual"] = "physical"
    else:
        # Virtual: the Frame Opening is anchored to the screen and the window
        # is cut to the artwork's aspect.
        opening = screen.inset(request.edge_margin)

        inner = opening.inset(ring.left, ring.right, ring.top, ring.bottom)
        _require(
            inner.width > MIN_INNER_MM and inner.height > MIN_INNER_MM,
            "Mat Ring consumes the frame opening; reduce the ring or enlarge "
            f"the screen (inner rect {inner.width!r} x {inner.height!r} mm)",
        )

        gap = float(request.whitespace.gap) if request.whitespace.enabled else 0.0
        branch = "virtual"

        if not has_mat:
            # Borderless: no mat at all, so the Mat Window is the Frame Opening
            # and the overflow behaviour resolves any aspect mismatch.
            window = opening
        else:
            # Design C (spec/"The ring absorbs the mismatch"): the window is cut
            # to the artwork's aspect so the SHORTEST ring sits exactly on the
            # style target, and the opposite axis absorbs the mismatch.  No
            # ambient fill is ever required.
            band = _gap_insets(inner, gap)
            slot = inner.inset(band.left, band.right, band.top, band.bottom)
            _require(slot.is_positive(), "Mat Ring leaves no room for the artwork")
            window = _contain(media.aspect, slot).expand(
                band.left, band.right, band.top, band.bottom
            )

            # Position within ``inner`` (not the opening) so the style's per-side
            # ring targets survive — this is what expresses a deliberately
            # bottom-heavy style such as polaroid.
            ox, oy = request.mat_window_offset
            window = Rect(
                inner.x + (inner.width - window.width) * ox,
                inner.y + (inner.height - window.height) * oy,
                window.width,
                window.height,
            )

    _require(
        opening.contains(window),
        f"Mat Window must lie within the Frame Opening: {window} vs {opening}",
    )
    if branch == "virtual":
        _require(
            screen_rect.contains(opening),
            f"Frame Opening must lie within the screen: {opening}",
        )

    # -- 2. Frame ---------------------------------------------------------
    frame_outer = opening.expand(moulding.left, moulding.right, moulding.top, moulding.bottom)
    _require(frame_outer.is_positive(), "Frame Outer must have positive size")

    # The frame must overlap the whole panel so the non-screen area is never
    # visible.  Nothing is needed once the opening already covers the panel
    # (the ring grows it past the edge), hence the clamp at zero.
    #
    # The rebate is a real part of the moulding, so it cannot be wider than the
    # moulding itself — that would be physically impossible.  When it is, the
    # frame is too small to house the panel and a wider moulding is required.
    required_rebate = screen.required_rebate(opening)
    for side, need, have in (
        ("left", required_rebate.left, moulding.left),
        ("right", required_rebate.right, moulding.right),
        ("top", required_rebate.top, moulding.top),
        ("bottom", required_rebate.bottom, moulding.bottom),
    ):
        _require(
            need <= have + 1e-6,
            "The rebate needed to hide the panel edge is wider than the frame:"
            f" {side} needs {need:.1f} mm but the moulding is {have:.1f} mm. "
            "Widen the moulding, enlarge the Mat Window, or increase the Mat "
            "Ring so the opening covers the panel "
            f"({screen.panel_size[0]:.1f} x {screen.panel_size[1]:.1f} mm).",
        )

    # -- 3. Artwork + whitespace -----------------------------------------
    focal = _resolve_focal_point(media, request)
    gap = float(request.whitespace.gap) if request.whitespace.enabled else 0.0

    # The artwork is placed inside the Mat Window, reserving the whitespace
    # band symmetrically so it stays uniform around the artwork.
    band = _gap_insets(window, gap)
    slot = window.inset(band.left, band.right, band.top, band.bottom)
    _require(slot.is_positive(), "the Mat Window leaves no room for the artwork")

    if request.overflow == "crop":
        # Cover: the artwork fills the slot, cropping whatever overflows.  The
        # whitespace band still surrounds the artwork, so a white border and a
        # cropped image are combined — which is exactly why the two must not be
        # paired with ambient fill as well.
        fit: FitMode = "cover"
        artwork_rect = slot
    else:
        fit = "contain"
        artwork_rect = _contain(media.aspect, slot)
        # Focal shift within the slot's slack, bounded.
        slack_x = slot.width - artwork_rect.width
        slack_y = slot.height - artwork_rect.height
        if focal is not None and (slack_x > 0 or slack_y > 0):
            shift_x = min(slack_x / 2.0, MAX_FOCAL_SHIFT_X * slot.width)
            shift_y = min(slack_y / 2.0, MAX_FOCAL_SHIFT_Y * slot.height)
            artwork_rect = artwork_rect.shifted(
                (focal.x - 0.5) * 2.0 * shift_x,
                (focal.y - 0.5) * 2.0 * shift_y,
            )

    ws_outer = artwork_rect.expand(band.left, band.right, band.top, band.bottom)
    # The white band is measured from the presentation to the artwork, so it is
    # always the authored width.  Measuring it from the Mat Window would wrongly
    # absorb the ambient residue, which is a separate band.
    applied_gap = _residual(artwork_rect, ws_outer)

    # Ambient residue: the gap between the Mat Window and the whitespace
    # presentation.  Non-zero only when the window exceeds the artwork plus its
    # band, which is the physical branch's normal case.
    residual = _residual(ws_outer, window)

    _require(
        window.contains(ws_outer),
        f"whitespace/artwork must lie within the Mat Window: {ws_outer} vs {window}",
    )

    # -- 4. Ambient fill (derived) ---------------------------------------
    # Present exactly when the Mat Window is fixed and the artwork does not
    # fill it.  Never mat space, and never part of the mat geometry.
    ambient_present = request.overflow == "fill" and (
        residual.horizontal > 1e-6 or residual.vertical > 1e-6
    )
    bars: list[Rect] = []
    ambient_region = window
    if ambient_present:
        if residual.left > 1e-6:
            bars.append(Rect(window.x, window.y, residual.left, window.height))
        if residual.right > 1e-6:
            bars.append(Rect(ws_outer.right, window.y, residual.right, window.height))
        if residual.top > 1e-6:
            bars.append(Rect(window.x, window.y, window.width, residual.top))
        if residual.bottom > 1e-6:
            bars.append(Rect(window.x, ws_outer.bottom, window.width, residual.bottom))

    # -- 5. Assemble ------------------------------------------------------
    frame_result = FrameResult(
        opening=opening,
        outer=frame_outer,
        moulding=moulding,
        orientation=classify_aspect(opening.aspect),
        required_rebate=required_rebate,
    )
    mat_result = MatResult(
        type=branch,
        outer=opening,
        window=window,
        ring=Insets(
            window.x - opening.x,
            opening.right - window.right,
            window.y - opening.y,
            opening.bottom - window.bottom,
        ),
        offset=tuple(request.mat_window_offset),  # type: ignore[arg-type]
        colour=request.mat_colour,
        orientation=classify_aspect(window.aspect),
    )
    whitespace_result = WhitespaceResult(
        enabled=bool(request.whitespace.enabled),
        authored_gap=gap,
        applied_gap=applied_gap,
        outer=ws_outer,
        residual=residual,
        colour=request.whitespace.colour,
    )
    ambient_result = AmbientFillResult(
        present=ambient_present,
        strategy=request.ambient.strategy,
        region=ambient_region,
        bars=bars,
        colour=request.ambient.colour,
        blur_radius=float(request.ambient.blur_radius),
        darken=float(request.ambient.darken),
    )
    artwork_result = ArtworkResult(
        bounds=artwork_rect,
        presentation=ws_outer,
        fit=fit,
        focal_point=focal,
        orientation=classify_aspect(media.aspect),
    )
    metrics = Metrics(
        screen_utilisation=(window.area / screen.area) if screen.area else 0.0,
        overlap=mat_result.ring,
        screen_aspect=screen.aspect,
        artwork_aspect=media.aspect,
        opening_aspect=opening.aspect,
        window_aspect=window.aspect,
    )

    return FramingResult(
        screen=screen,
        branch=branch,
        overflow=request.overflow,
        frame=frame_result,
        mat=mat_result,
        whitespace=whitespace_result,
        ambient_fill=ambient_result,
        artwork=artwork_result,
        metrics=metrics,
        effects=request.effects,
    )


# ---------------------------------------------------------------------------
# Invariant checker — used by the test suite
# ---------------------------------------------------------------------------


def check_invariants(result: FramingResult) -> list[str]:
    """Return a list of violated invariants (spec/"Invariants").

    Empty list means the geometry is consistent: nesting holds, no band is
    negative, and the per-axis band sum accounts for the whole distance
    between the Frame Opening and the artwork.
    """
    problems: list[str] = []
    tol = 1e-6

    # 2. Nesting
    if not result.mat.window.contains(result.artwork.presentation, tol):
        problems.append("presentation is not contained by the Mat Window")
    if not result.artwork.presentation.contains(result.artwork.bounds, tol):
        problems.append("artwork bounds are not contained by the presentation")
    if not result.frame.opening.contains(result.mat.window, tol):
        problems.append("Mat Window is not contained by the Frame Opening")
    if not result.frame.outer.contains(result.frame.opening, tol):
        problems.append("Frame Opening is not contained by the Frame Outer")

    # 5. Rings are never negative and sum to the slack
    if result.mat.ring.minimum < -tol:
        problems.append(f"negative ring: {result.mat.ring}")
    if (
        abs(result.mat.ring.horizontal - (result.frame.opening.width - result.mat.window.width))
        > tol
    ):
        problems.append("horizontal rings do not sum to the slack")
    if (
        abs(result.mat.ring.vertical - (result.frame.opening.height - result.mat.window.height))
        > tol
    ):
        problems.append("vertical rings do not sum to the slack")

    # 3. Band partition: every band between the opening and the artwork is
    #    non-negative, so nothing overlaps and no space is unclaimed.
    for axis, ring_pair in (
        ("x", (result.mat.ring.left, result.mat.ring.right)),
        ("y", (result.mat.ring.top, result.mat.ring.bottom)),
    ):
        if min(ring_pair) < -tol:
            problems.append(f"negative Mat Ring on axis {axis}")
    for axis, band in (
        (
            "x",
            (result.whitespace.applied_gap.left, result.whitespace.applied_gap.right),
        ),
        (
            "y",
            (result.whitespace.applied_gap.top, result.whitespace.applied_gap.bottom),
        ),
    ):
        if min(band) < -tol:
            problems.append(f"negative whitespace band on axis {axis}")

    # 9. Physical Mat Window is independent of the style
    if result.branch == "physical" and not result.screen.rect.contains(result.mat.window, tol):
        problems.append("physical Mat Window is not on the screen")

    # 6/7. Ambient fill is derived from a residual only. It appears in the
    # physical branch always, and in the virtual branch only at ring 0, where
    # the fixed window is the Frame Opening itself.
    has_ring = result.mat.ring.horizontal > tol or result.mat.ring.vertical > tol
    if result.ambient_fill.present and result.branch == "virtual" and has_ring:
        problems.append(
            "ambient fill must be absent in the virtual branch when a ring "
            "is applied (the Mat Window is cut to the artwork)"
        )
    if not result.ambient_fill.present and not result.mat.window.contains(
        result.artwork.presentation, tol
    ):
        problems.append("presentation must always lie within the Mat Window")

    # 8. A virtual mat holds the style proportion on its shortest ring.  The
    #    slack axis is expected to differ; the shortest side must still match.
    if result.branch == "virtual" and has_ring:
        slack = (
            result.frame.opening.width - result.mat.window.width,
            result.frame.opening.height - result.mat.window.height,
        )
        if min(slack) < -tol:
            problems.append("virtual Mat Window exceeds the Frame Opening")

    return problems


__all__ = [
    # constants
    "PORTRAIT_THRESHOLD",
    "SQUARE_MAX",
    "PANORAMA_THRESHOLD",
    "MAX_FOCAL_SHIFT_X",
    "MAX_FOCAL_SHIFT_Y",
    # helpers
    "classify_aspect",
    "ring_target_mm",
    "check_invariants",
    # value types
    "Rect",
    "Insets",
    "FocalPoint",
    "Face",
    # inputs
    "MediaDescriptor",
    "Screen",
    "WhitespaceSpec",
    "AmbientFillSpec",
    "Effects",
    "FramingRequest",
    # outputs
    "FrameResult",
    "MatResult",
    "WhitespaceResult",
    "AmbientFillResult",
    "ArtworkResult",
    "Metrics",
    "FramingResult",
    # entry point
    "calculate_framing",
]
