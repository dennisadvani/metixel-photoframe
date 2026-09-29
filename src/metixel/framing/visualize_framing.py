# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Visualise Metixel framing scenarios (engine v2) as matplotlib figures.

This script is a **viewer only**: it imports the engine and its templates and
draws their geometry.  It never recomputes or modifies framing logic.

Specification: ``docs/geometry-model.md``.

Geometry is computed in millimetres and drawn onto a millimetre plane, so a
uniform mm band appears uniform on screen at any panel aspect.

Figures
-------

Physical mat (``--family physical``, the default):

``P-A``  Ring sweep — the same Mat Window with each style's ring, showing the
         frame growing around a fixed window.
``P-B``  Mat Window shapes — landscape, portrait and square windows in a square
         frame, showing how much of the 16:10 active area each one uses.
``P-C``  Artwork aspects — how the artwork and its ambient fill vary inside one
         fixed Mat Window.

Virtual mat (``--family virtual``):

``V-A``  Style sweep — every style, showing the visible area shrinking as the
         ring grows.
``V-B``  Artwork aspects — the Mat Window is cut to the artwork, so the ring
         becomes uneven and the shortest ring holds the style target.
``V-C``  Overflow behaviours — ``immersive`` (crop) against ``immersive_fill``
         (contain plus ambient fill).

Every panel draws exactly five things:

* the **frame** in black,
* the **mat** in beige (when a mat is visible),
* **whitespace** in white (when enabled),
* **ambient fill** in light green (when present),
* the **artwork** in dark green.

Nothing else is drawn.  In particular the non-screen area of the panel is never
shown: a frame rebate smaller than the panel would expose it, so
:func:`~metixel.framing_engine.calculate_framing` rejects that configuration
instead.

The **rebate is never drawn.**  ``required_rebate`` is a fit check — "what
overlap must a real frame provide to hide the panel edge?" — reported as text for
the customer, not as a component of the composition.  It appears in a panel's
caption only on the virtual branch, where it is pure guidance because the mat and
artwork are software-drawn and no physical frame is doing any hiding.  On the
physical branch the engine already enforces rebate-vs-moulding, so repeating it
would read as though it were geometry.

Run::

    .venv\\Scripts\\python visualize_framing.py --no-show
    .venv\\Scripts\\python visualize_framing.py --family virtual --no-show
    .venv\\Scripts\\python visualize_framing.py --figure P-B-L --no-show
    .venv\\Scripts\\python visualize_framing.py --no-show --dpi 300 --spec
"""

from __future__ import annotations

import argparse
import os
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from . import framing_templates as templates
from .framing_engine import (
    FramingRequest,
    Insets,
    MediaDescriptor,
    Rect,
    calculate_framing,
)

# matplotlib is an OPTIONAL extra (``pip install metixel-photoframe[viz]``).
# It is imported lazily, inside the functions that actually draw — never at
# module level — so that ``import metixel.framing``, and therefore the whole
# application, works on a device that has never installed it.


def _require_matplotlib() -> None:
    """Fail with an actionable message when the optional extra is missing.

    The viewer is a dev tool, so a bare ImportError from deep inside a plot is
    unhelpful — it looks like the framing engine is broken rather than like a
    missing optional dependency.
    """
    try:
        import matplotlib  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SystemExit(
            "The framing visualiser needs matplotlib, which is an optional extra.\n"
            "Install it with:  pip install 'metixel-photoframe[viz]'"
        ) from exc


# ---------------------------------------------------------------------------
# Palette — only four things are drawn
# ---------------------------------------------------------------------------

FRAME_COLOUR = "#000000"  # the frame moulding
MAT_COLOUR = "#e8dcc0"  # the mat (physical board or virtual ring)
WHITESPACE_COLOUR = "#ffffff"  # the whitespace band
AMBIENT_COLOUR = "#b7e4a8"  # ambient fill
ARTWORK_COLOUR = "#1e5c2e"  # the artwork

FOCAL_MARK = "#ffd166"
FACE_MARK = "#ef476f"

SCREEN = templates.METIXEL_16_10_1920x1200
PLANE_PAD = 12.0  # mm of padding around the Frame Outer

#: Images are written here, keeping generated output out of the source tree.
#: The viewer lives in ``src/metixel/``, so the folder is at the repo root.
OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "visualisation_output",
)

#: The two mountings of the Metixel panel.  Rotating the assembly swaps the
#: active area, the panel and the pixel dimensions, but not the reference
#: dimension — so a style's ring is the same width either way up.
ORIENTATIONS = ("landscape", "portrait")

#: Figure registry, populated at the bottom of this module from ``_FAMILIES``.
#: Keyed by ``<family letter>-<figure>-<orientation>``, e.g. ``P-B-L``.
FIGURES: dict[str, tuple[str, Any, str, str]] = {}


def screen_for(orientation: str) -> templates.Screen:
    """The Metixel panel mounted ``orientation``."""
    return templates.METIXEL_SCREENS[orientation]


def media_for_orientation(orientation: str, portrait_first: bool = True) -> float:
    """A representative artwork aspect for a mounting.

    A 3:2 landscape artwork inside a portrait panel wastes most of the screen,
    which is a legitimate configuration but not a useful default for a sweep.
    """
    return 2 / 3 if orientation == "portrait" else 3 / 2


# Panel titles are wrapped to this many characters so they fit the panel width
# instead of running off the figure.
TITLE_CHARS = 26
TITLE_FONTSIZE = 7.0


def _wrap(text: str, width: int = TITLE_CHARS) -> list[str]:
    """Wrap ``text`` onto lines of at most ``width`` characters."""
    return textwrap.wrap(text, width=width) or [""]


# ---------------------------------------------------------------------------
# Scenario model
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """One panel: a style or preset, plus any overrides."""

    label: str
    media: MediaDescriptor
    style: str = "gallery"
    preset: str | None = None
    screen: templates.Screen | None = None
    """The mounting to use.  ``None`` falls back to the landscape panel."""

    physical: bool = False
    mat_window: Rect | None = None
    ring: object | None = None
    moulding: Insets | None = None
    whitespace: bool | None = None
    """``None`` (default) uses the style's own setting; ``True``/``False``
    overrides it.  Whitespace is a virtual element drawn by the screen, so it
    is available on a physical mat just as it is on a virtual one."""

    whitespace_gap: float = 8.0
    overflow: str | None = None
    """User choice: ``crop`` or ``fill``.  Independent of the style.

    ``crop`` covers the window (edges lost); ``fill`` contains the artwork and
    lets ambient fill absorb the rest.  Whitespace forces ``crop``.
    """

    ambient_strategy: str | None = None
    moulding_width: float = 40.0
    note: str = ""

    def build(self) -> FramingRequest:
        screen = self.screen or SCREEN
        kwargs: dict[str, Any] = {
            "physical": self.physical,
            "moulding_width": self.moulding_width,
        }
        if self.whitespace is not None:
            kwargs["whitespace"] = self.whitespace
            kwargs["whitespace_gap"] = self.whitespace_gap
        elif self.whitespace_gap != 8.0:
            kwargs["whitespace_gap"] = self.whitespace_gap
        if self.moulding is not None:
            kwargs["moulding"] = self.moulding
            kwargs.pop("moulding_width")
        if self.mat_window is not None:
            kwargs["mat_window"] = self.mat_window
        if self.ring is not None:
            kwargs["ring"] = self.ring
        if self.overflow is not None:
            kwargs["overflow"] = self.overflow
        if self.ambient_strategy is not None:
            kwargs["ambient_strategy"] = self.ambient_strategy
        if self.preset is not None:
            return templates.build_request_for_preset(screen, self.media, self.preset, **kwargs)
        return templates.build_request(screen, self.media, self.style, **kwargs)


ASPECTS = {
    "21:9": 21 / 9,
    "16:9": 16 / 9,
    "3:2": 3 / 2,
    "1:1": 1.0,
    "2:3": 2 / 3,
    "9:16": 9 / 16,
}

STYLE_NAMES = ["modern", "classic", "gallery", "museum", "floating", "polaroid"]


def media_for(aspect: float) -> MediaDescriptor:
    """A media descriptor with an exact aspect ratio."""
    if aspect >= 1.0:
        return MediaDescriptor(width=float(aspect), height=1.0)
    return MediaDescriptor(width=1.0, height=1.0 / float(aspect))


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _plot_bounds(result) -> tuple[float, float, float, float]:
    """The Frame Outer plus padding — nothing outside this is ever drawn."""
    outer = result.frame.outer
    return (
        outer.x - PLANE_PAD,
        outer.right + PLANE_PAD,
        outer.y - PLANE_PAD,
        outer.bottom + PLANE_PAD,
    )


def _fill(ax, rect: Rect, colour: str, ec: str = "none", lw: float = 0.0, z: int = 2) -> None:
    if rect.width <= 0 or rect.height <= 0:
        return
    from matplotlib.patches import Rectangle  # lazy: optional extra

    ax.add_patch(
        Rectangle(
            (rect.x, rect.y),
            rect.width,
            rect.height,
            facecolor=colour,
            edgecolor=ec,
            linewidth=lw,
            zorder=z,
        )
    )


def _ring_patches(outer: Rect, inner: Rect) -> list[tuple[float, float, float, float]]:
    """The four rectangles forming the band between two rects."""
    if inner.width <= 0 or inner.height <= 0:
        return []
    return [
        (outer.x, outer.y, outer.width, inner.y - outer.y),  # top
        (outer.x, inner.bottom, outer.width, outer.bottom - inner.bottom),  # bottom
        (outer.x, inner.y, inner.x - outer.x, inner.height),  # left
        (inner.right, inner.y, outer.right - inner.right, inner.height),  # right
    ]


def draw_scenario(ax, scenario: Scenario) -> list[str]:
    """Draw one panel and return its title lines (unpadded)."""
    from matplotlib.patches import Rectangle  # lazy: optional extra

    result = calculate_framing(scenario.build())

    lo_x, hi_x, lo_y, hi_y = _plot_bounds(result)
    ax.set_xlim(lo_x, hi_x)
    ax.set_ylim(hi_y, lo_y)  # y increases downwards, as in the engine
    ax.set_aspect("equal")
    ax.axis("off")

    opening = result.frame.opening
    window = result.mat.window

    # 1) The frame: the band between Frame Outer and Frame Opening, in black.
    for patch in _ring_patches(result.frame.outer, opening):
        _fill(ax, Rect(*patch), FRAME_COLOUR, z=1)

    # 2) The mat: the band between the Frame Opening and the Mat Window.
    #    Absent (zero width) when there is no mat, so nothing is drawn.
    for patch in _ring_patches(opening, window):
        _fill(ax, Rect(*patch), MAT_COLOUR, z=2)

    # 3) Ambient fill — the background of the Mat Window interior, wherever the
    #    artwork plus its whitespace band does not reach.  Painted first so the
    #    layers above sit on top of it.
    if result.ambient_fill.present:
        _fill(ax, window, AMBIENT_COLOUR, z=3)

    # 4) Whitespace, between the Mat Window and the artwork.  The band lies
    #    OUTSIDE the artwork rect — expanding inwards from ``ws_outer`` would
    #    cover the artwork, so the ring is cut from ws_outer to the artwork and
    #    the leftover (the ambient residue) is left showing.
    art = result.artwork
    if result.whitespace.enabled and result.whitespace.authored_gap > 0:
        for patch in _ring_patches(result.whitespace.outer, art.bounds):
            _fill(ax, Rect(*patch), WHITESPACE_COLOUR, z=4)

    # 5) The artwork: the only content, in dark green.
    _fill(ax, art.bounds, ARTWORK_COLOUR, z=5)

    # 6) Markers.
    if art.focal_point is not None and art.bounds.width > 0:
        fx = art.bounds.x + art.focal_point.x * art.bounds.width
        fy = art.bounds.y + art.focal_point.y * art.bounds.height
        ax.plot([fx], [fy], "+", color=FOCAL_MARK, ms=8, mew=2, zorder=7)
    if art.fit == "contain":
        for face in scenario.media.faces or []:
            ax.add_patch(
                Rectangle(
                    (
                        art.bounds.x + face.x * art.bounds.width,
                        art.bounds.y + face.y * art.bounds.height,
                    ),
                    face.width * art.bounds.width,
                    face.height * art.bounds.height,
                    fill=False,
                    edgecolor=FACE_MARK,
                    linewidth=1.0,
                    zorder=7,
                )
            )

    # 7) Title.  Kept to short wrapped lines so nothing overflows the panel.
    #    Height is equalised by the caller.
    ring = result.mat.ring
    ring_text = f"{ring.minimum:.0f}"
    if not _uniform(ring):
        widest = max(ring.left, ring.right, ring.top, ring.bottom)
        ring_text += f"-{widest:.0f}"

    lines = _wrap(scenario.label)
    lines.append(f"open {opening.width:.0f}x{opening.height:.0f}")
    lines.append(f"win {window.width:.0f}x{window.height:.0f}")
    lines.append(f"ring {ring_text}mm  used {result.metrics.screen_utilisation:.0%}")
    flags: list[str] = []
    # The rebate is a FIT CHECK, never a drawn element.  It answers "will this
    # panel fit inside a real frame, and how much overlap does the frame need to
    # hide the non-screen border?" — so it is reported as advice, not rendered.
    #
    # Only shown on the VIRTUAL branch.  There the mat and artwork are drawn in
    # software, so there is no physical rebate doing any hiding; the number is
    # guidance for someone choosing a real frame to mount the panel in.  On the
    # PHYSICAL branch the rebate is a genuine constraint that the engine already
    # enforces (see the rebate-vs-moulding check in framing_engine), so repeating
    # it here as a flag would read as though it were part of the geometry.
    rebate = result.frame.required_rebate
    if result.branch == "virtual" and rebate.minimum > 0:
        flags.append(f"fits frame rebate >= {rebate.minimum:.0f}mm")
    if result.ambient_fill.present:
        flags.append(f"ambient {result.ambient_fill.strategy}")
    if result.whitespace.enabled and result.whitespace.authored_gap > 0:
        flags.append(f"ws {result.whitespace.authored_gap:.0f}mm")
    if scenario.note:
        flags.append(scenario.note)
    if flags:
        lines.extend(_wrap("  ".join(flags)))
    return lines


def _uniform(insets: Insets, tol: float = 1e-6) -> bool:
    return max(insets) - min(insets) < tol


# ---------------------------------------------------------------------------
# Physical family
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Figure factories
#
# Figures A, B and C are orientation-agnostic in structure: each is generated
# once per mounting, so the physical and virtual families are exercised
# landscape *and* portrait.
# ---------------------------------------------------------------------------


def physical_ring_sweep(orientation: str) -> tuple[list[Scenario], int, int]:
    """The same Mat Window with each style's ring: the frame grows."""
    screen = screen_for(orientation)
    window = screen.inset(2.0)
    ref = screen.reference_dimension(2.0)
    scenarios = [
        Scenario(
            label=name,
            media=media_for(media_for_orientation(orientation)),
            style=name,
            screen=screen,
            physical=True,
            mat_window=window,
            note=f"frame {window.width + 2 * templates.STYLES[name].ring * ref:.0f} wide",
        )
        for name in STYLE_NAMES
    ]
    return scenarios, 3, 2


def _max_window(screen: templates.Screen, aspect: float, edge_margin: float = 2.0) -> Rect:
    """The largest centred Mat Window of ``aspect`` that fits the active area.

    Maximising the window is what a real framer would do: it keeps the lit
    screen as large as possible and leaves only the margin needed to hide the
    panel edge.  The window's **short** side therefore ends up as large as the
    mounting allows — 320 mm for the Metixel panel, the same as the reference
    dimension.
    """
    available = screen.inset(edge_margin)
    if aspect >= available.aspect:
        width = available.width
        height = available.width / aspect
    else:
        height = available.height
        width = available.height * aspect
    return Rect(
        (screen.width_mm - width) / 2.0,
        (screen.height_mm - height) / 2.0,
        width,
        height,
    )


def physical_window_shapes(orientation: str) -> tuple[list[Scenario], int, int]:
    """Mat Window shapes, each maximised inside the active area.

    A window's **short side is as large as the panel allows** (~320 mm), so the
    only variable is its shape.  The three rows therefore show what a window's
    proportions cost in screen area:

    * **panel shape** — fills the inset active area; ~98 % used
    * **square** — 61 % used, because a square cannot fill a 16:10 panel
    * **rotated** — 41 % used, the worst case

    A smaller window needs a wider moulding: the frame must still reach the
    panel edge, and the rebate cannot exceed the moulding width.
    """
    screen = screen_for(orientation)
    available = screen.inset(2.0)
    panel_w, panel_h = screen.panel_size

    # Window aspects, expressed relative to the mounting so the same three
    # cases appear whichever way up the panel is.
    cases = (
        ("panel-shape", available.aspect),
        ("square", 1.0),
        ("rotated", 1.0 / available.aspect),
    )

    scenarios: list[Scenario] = []
    for shape, aspect in cases:
        window = _max_window(screen, aspect)
        # Ring enough that the opening reaches the panel on both axes.
        ring = max(
            (panel_w - window.width) / 2.0,
            (panel_h - window.height) / 2.0,
        )
        moulding = ring + 2.0
        for aspect_name, art_aspect in (
            ("16:9", 16 / 9),
            ("1:1", 1.0),
            ("9:16", 9 / 16),
        ):
            scenarios.append(
                Scenario(
                    label=f"{shape} window / {aspect_name} art",
                    media=media_for(art_aspect),
                    style="custom",
                    screen=screen,
                    physical=True,
                    mat_window=window,
                    ring=ring,
                    moulding_width=moulding,
                    note=f"window uses {window.width * window.height / screen.area:.0%} of area",
                )
            )
    return scenarios, 3, 3


def physical_artwork_aspects(orientation: str) -> tuple[list[Scenario], int, int]:
    """Ambient fill and whitespace inside one fixed Mat Window.

    Row 1 crops, so the artwork fills the window.  Row 2 adds whitespace and
    shows the ambient residue that remains inside the window.
    """
    screen = screen_for(orientation)
    window = screen.inset(2.0)
    picks = [("21:9", 21 / 9), ("3:2", 3 / 2), ("1:1", 1.0), ("9:16", 9 / 16)]
    scenarios: list[Scenario] = [
        Scenario(
            label=f"gallery crop / {name}",
            media=media_for(aspect),
            style="gallery",
            screen=screen,
            physical=True,
            mat_window=window,
            whitespace=False,
            overflow="crop",
            ambient_strategy="bars",
        )
        for name, aspect in picks
    ]
    scenarios += [
        Scenario(
            label=f"gallery ws / {name}",
            media=media_for(aspect),
            style="gallery",
            screen=screen,
            physical=True,
            mat_window=window,
            whitespace=True,
            whitespace_gap=10.0,
            note="whitespace + ambient",
        )
        for name, aspect in picks
    ]
    return scenarios, len(picks), 2


def virtual_style_sweep(orientation: str) -> tuple[list[Scenario], int, int]:
    """Every style: the Frame Opening is fixed and the visible area shrinks."""
    screen = screen_for(orientation)
    shape_aspect = media_for_orientation(orientation)
    scenarios = [
        Scenario(label=name, media=media_for(shape_aspect), style=name, screen=screen)
        for name in STYLE_NAMES
    ]
    scenarios.append(
        Scenario(
            label="borderless",
            media=media_for(shape_aspect),
            style="borderless",
            screen=screen,
            overflow="fill",
        )
    )
    scenarios.append(
        Scenario(
            label="museum + ws",
            media=media_for(shape_aspect),
            style="museum",
            screen=screen,
            whitespace=True,
            whitespace_gap=10.0,
        )
    )
    return scenarios, 4, 2


def virtual_artwork_aspects(orientation: str) -> tuple[list[Scenario], int, int]:
    """The ring absorbs the mismatch, so the window is cut to the artwork."""
    screen = screen_for(orientation)
    scenarios = [
        Scenario(
            label=f"gallery / {name}",
            media=media_for(aspect),
            style="gallery",
            screen=screen,
        )
        for name, aspect in ASPECTS.items()
    ]
    return scenarios, 3, 2


def virtual_overflows(orientation: str) -> tuple[list[Scenario], int, int]:
    """Crop against fill, as a user choice rather than a style."""
    screen = screen_for(orientation)
    scenarios: list[Scenario] = []
    for name, aspect in ASPECTS.items():
        for overflow in ("crop", "fill"):
            scenarios.append(
                Scenario(
                    label=f"borderless + {overflow} / {name}",
                    media=media_for(aspect),
                    style="borderless",
                    screen=screen,
                    overflow=overflow,
                    ambient_strategy="bars" if overflow == "crop" else "blur",
                )
            )
    return scenarios, 4, 3


def physical_overflows(orientation: str) -> tuple[list[Scenario], int, int]:
    """Crop against fill on a **physical** mat, where the window is pinned.

    This is the case ambient fill exists for, so it is the most useful place to
    compare the two overflow behaviours side by side.
    """
    screen = screen_for(orientation)
    window = screen.inset(2.0)
    scenarios: list[Scenario] = []
    for name, aspect in [("21:9", 21 / 9), ("3:2", 3 / 2), ("9:16", 9 / 16)]:
        for overflow in ("crop", "fill"):
            scenarios.append(
                Scenario(
                    label=f"gallery + {overflow} / {name}",
                    media=media_for(aspect),
                    style="gallery",
                    screen=screen,
                    physical=True,
                    mat_window=window,
                    overflow=overflow,
                    whitespace=False,
                    ambient_strategy="bars" if overflow == "crop" else "solid",
                )
            )
    return scenarios, 2, 3


# Figure key -> (title, factory).  Keys carry the orientation suffix so each
# mounting gets its own image: P-A-L / P-A-P, and so on.
_FAMILIES = (
    (
        "P",
        "physical",
        [
            ("A", "Physical — ring sweep", physical_ring_sweep),
            ("B", "Physical — Mat Window shapes", physical_window_shapes),
            (
                "C",
                "Physical — crop, and whitespace + ambient",
                physical_artwork_aspects,
            ),
            ("D", "Physical — crop vs fill", physical_overflows),
        ],
    ),
    (
        "V",
        "virtual",
        [
            ("A", "Virtual — style sweep", virtual_style_sweep),
            ("B", "Virtual — artwork aspects", virtual_artwork_aspects),
            ("C", "Virtual — crop vs fill", virtual_overflows),
        ],
    ),
)

FIGURES.clear()
for _prefix, _family, _entries in _FAMILIES:
    for _letter, _title, _factory in _entries:
        for _orientation in ORIENTATIONS:
            _key = f"{_prefix}-{_letter}-{'L' if _orientation == 'landscape' else 'P'}"
            _label = f"{_prefix}-{_letter}"
            FIGURES[_key] = (
                f"{_label} — {_title} ({_orientation})",
                (lambda f, o: lambda: f(o))(_factory, _orientation),
                _family,
                _orientation,
            )


# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------


def _legend_handles() -> list[Any]:
    """Build the legend handles on demand.

    Deliberately a function rather than a module-level list: ``Patch`` and
    ``Line2D`` are matplotlib objects, and constructing them at import time
    would make matplotlib a hard requirement of importing this module — which
    in turn would break ``import metixel.framing`` on a device without the
    optional ``viz`` extra.
    """
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    return [
        Patch(facecolor=FRAME_COLOUR, edgecolor="#666666", label="Frame"),
        Patch(facecolor=MAT_COLOUR, edgecolor="#b8ab8c", label="Mat"),
        Patch(facecolor=WHITESPACE_COLOUR, edgecolor="#b8b8b8", label="Whitespace"),
        Patch(facecolor=AMBIENT_COLOUR, edgecolor="#7fae72", label="Ambient fill"),
        Patch(facecolor=ARTWORK_COLOUR, label="Artwork"),
        Line2D(
            [0],
            [0],
            marker="+",
            color=FOCAL_MARK,
            ls="none",
            ms=10,
            mew=2,
            label="Focal point",
        ),
    ]


#: Backwards-compatible alias — the list is built lazily on first access.
LEGEND_HANDLES: list[Any] = []

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(
    scenarios: Sequence[Scenario],
    ncols: int,
    nrows: int,
    title: str,
    out_path: str,
    dpi: int,
    show: bool,
) -> None:
    import matplotlib.pyplot as plt  # lazy: optional extra

    panel = 2.5
    fig, axes = plt.subplots(nrows, ncols, figsize=(ncols * panel, nrows * panel * 0.8 + 2.4))
    axes = axes.reshape(nrows, ncols) if nrows * ncols > 1 else [[axes]]

    # Draw every panel first, collecting titles, so they can all be padded to
    # the same height.  Without that, a panel with fewer title lines plots into
    # a taller area and the grid looks ragged.
    titles: dict[tuple[int, int], list[str]] = {}
    for index, scenario in enumerate(scenarios):
        if index >= nrows * ncols:
            break
        row, col = divmod(index, ncols)
        try:
            titles[(row, col)] = draw_scenario(axes[row][col], scenario)
        except ValueError as exc:
            # A configuration the engine legitimately rejects: say so on the
            # panel rather than failing the whole figure.
            ax = axes[row][col]
            ax.set_xlim(0.0, 1.0)
            ax.set_ylim(1.0, 0.0)
            ax.set_aspect("equal")
            ax.axis("off")
            titles[(row, col)] = _wrap(scenario.label) + _wrap(f"rejected: {exc}", width=34)

    max_lines = max((len(v) for v in titles.values()), default=0)
    for (row, col), lines in titles.items():
        padded = lines + [""] * (max_lines - len(lines))
        axes[row][col].set_title(
            "\n".join(padded),
            fontsize=TITLE_FONTSIZE,
            linespacing=1.4,
        )

    for index in range(len(scenarios), nrows * ncols):
        row, col = divmod(index, ncols)
        axes[row][col].axis("off")

    # The figure title is wrapped to the figure width, so long details can never
    # run off the page.
    suptitle_chars = max(60, ncols * 30)
    fig.suptitle(
        "\n".join(_wrap(title, width=suptitle_chars)),
        fontsize=12,
        y=0.995,
        linespacing=1.4,
    )
    fig.legend(
        handles=LEGEND_HANDLES if LEGEND_HANDLES else _legend_handles(),
        loc="upper center",
        bbox_to_anchor=(0.5, 0.965),
        ncol=6,
        fontsize=9,
        frameon=False,
        handlelength=1.6,
        columnspacing=1.6,
        handletextpad=0.5,
    )
    # Reserve room for the suptitle (two lines) and the legend.
    fig.tight_layout(rect=(0.01, 0.01, 0.99, 0.90))
    fig.savefig(out_path, dpi=dpi)

    size = fig.get_size_inches()
    print(f"wrote {out_path}  ({int(size[0] * dpi)}x{int(size[1] * dpi)} px @ {dpi} dpi)")
    if show:
        plt.show()
    plt.close(fig)


def print_spec_table() -> None:
    """Print the screen presets, style table and a sample cut list."""
    print("\nScreen presets")
    for name, row in templates.screen_table().items():
        print(f"  {name}")
        for key, value in row.items():
            print(f"    {key:<16} {value}")
    print(
        "\n  Fit check: a real frame needs a rebate of at least the panel size to "
        "hide the non-screen area."
    )
    print("  The rebate is never drawn — it is guidance for choosing a frame.")

    print("\nStyle table (Metixel, edge_margin 2 mm, reference dimension 320 mm)")
    print(f"{'style':<12} {'ring %':>8} {'ring mm':>9}  mental model")
    for name, row in templates.style_table().items():
        fraction = f"{float(row['fraction']) * 100:.1f}%"
        ring_mm = f"{float(row['ring_mm']):.1f}"
        print(f"{name:<12} {fraction:>8} {ring_mm:>9}  {row['mental_model']}")

    print("\nCut list — gallery, physical, 40 mm moulding")
    result = calculate_framing(
        templates.build_request(SCREEN, media_for(3 / 2), "gallery", physical=True)
    )
    for key, value in result.to_spec("mm").items():
        print(f"  {key:<22} {value}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render Metixel framing scenarios (engine v2) to images."
    )
    parser.add_argument(
        "--family",
        default="all",
        choices=["all", "physical", "virtual"],
        help="render only one mat family (default: all)",
    )
    parser.add_argument(
        "--orientation",
        default="all",
        choices=["all", "landscape", "portrait"],
        help="render only one mounting orientation (default: all)",
    )
    parser.add_argument(
        "--figure",
        default=None,
        choices=list(FIGURES),
        help="render a single figure, e.g. P-B-L or V-A-P",
    )
    parser.add_argument(
        "--no-show",
        action="store_true",
        help="write the PNGs and exit without a window",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=200,
        help="output resolution in dots per inch (default: 200)",
    )
    parser.add_argument(
        "--spec",
        action="store_true",
        help="also print the screen/style tables and a cut list",
    )
    args = parser.parse_args()

    # Fail early and helpfully if the optional viewer dependency is missing,
    # rather than a bare ImportError from deep inside a plot.
    _require_matplotlib()

    out_dir = OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    if args.figure:
        selected = [args.figure]
    elif args.family == "all" and args.orientation == "all":
        selected = list(FIGURES)
    else:
        selected = [
            k
            for k, v in FIGURES.items()
            if (args.family in ("all", v[2])) and (args.orientation in ("all", v[3]))
        ]

    for key in selected:
        title, factory, family, orientation = FIGURES[key]
        scenarios, ncols, nrows = factory()
        screen = screen_for(orientation)
        out = os.path.join(out_dir, f"framing_scenarios_{key}.png")
        render(
            scenarios,
            ncols,
            nrows,
            f"{key} — {title}  ·  Metixel {family} mat  ·  "
            f"active area {screen.width_mm:.0f}x{screen.height_mm:.0f} mm "
            f"in a {screen.housing_width_mm:.0f}x{screen.housing_height_mm:.0f} mm panel",
            out,
            args.dpi,
            show=not args.no_show,
        )

    if args.spec:
        print_spec_table()


if __name__ == "__main__":
    main()
