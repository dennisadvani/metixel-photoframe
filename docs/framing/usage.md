# Usage & API

How to describe a screen and artwork, and call the engine. Two modules in the
`metixel` package:

| Module | Role |
| --- | --- |
| `metixel.framing_engine` | **Dimensions only.** Explicit mm in, rectangles out. No styles. |
| `metixel.framing_templates` | Styles, screen presets, and `build_request()` to resolve a style into mm. |

For the model itself — branches, units, ring proportions, invariants — see
[Geometry Model](./geometry-model.md), which is the authority.

## Install

The package uses a **src layout** and should be installed in editable mode:

```powershell
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m pip install -e ".[dev,viz]"   # + pytest, ruff, mypy, matplotlib
```

The engine and templates are **stdlib-only**; `matplotlib` is needed only by the
viewer, and `pytest`/`ruff`/`mypy` only for development.

## Quick start

```python
from metixel.framing_engine import MediaDescriptor, calculate_framing
from metixel import framing_templates as templates

screen = templates.METIXEL_16_10_1920x1200          # 518 x 324 mm
media = MediaDescriptor(width=3, height=2)          # 3:2 artwork

request = templates.build_request(screen, media, "gallery")
result = calculate_framing(request)

print(result.mat.window)             # Rect(x=77.0, y=42.0, width=360.0, height=240.0)
print(result.mat.ring)               # Insets(left=77.0, right=77.0, top=40.0, bottom=40.0)
print(result.metrics.screen_utilisation)   # 0.515
```

## The screen

```python
Screen(width_mm=518.0, height_mm=324.0, width_px=1920.0, height_px=1200.0)
```

Millimetre dimensions are **required** — every length in the model is mm. Pixel
dimensions are optional and used only by `to_px()` and `to_spec("px")`.

```python
screen.reference_dimension(edge_margin=2.0)   # 320.0 — the shorter side of
                                              # screen - 2 x edge_margin
screen.inset(2.0)                             # Rect(2, 2, 514, 320)
```

Named presets live in `templates.SCREENS`; the Metixel panel is
`METIXEL_16_10_1920x1200`.

## The two branches

A request is physical or virtual depending on one field:

| `mat_window` | Branch | Behaviour |
| --- | --- | --- |
| `None` | **virtual** | Frame Opening is anchored to the screen; the Mat Window is cut to the artwork's aspect |
| a `Rect` | **physical** | The Mat Window is fixed; the Frame Opening grows around it |

```python
# Virtual: the Frame Opening is fixed at 514 x 320; the window shrinks.
virtual = templates.build_request(screen, media, "gallery")

# Physical: the window is fixed at 514 x 320; the frame grows.
physical = templates.build_request(screen, media, "gallery", physical=True)

# Physical with a deliberate square window (uses 61% of a 16:10 panel).
from metixel.framing_engine import Rect
square = templates.build_request(
    screen, media, "classic", physical=True,
    mat_window=Rect(x=99.0, y=2.0, width=320.0, height=320.0),
)
```

## Styles

`templates.STYLES` maps a name to a `Style`. The ring is a fraction of the
**reference dimension**, and the engine receives the resolved millimetres.

| Style | Ring % | Ring on Metixel | Mental model |
| --- | --- | --- | --- |
| `borderless` | 0 % | 0 mm | No mat / full bleed |
| `modern` | 4.5 % | 14.4 mm | Just enough separation |
| `classic` | 8 % | 25.6 mm | Traditional photographic mount |
| `gallery` | 12.5 % | 40.0 mm | Generous fine-art presentation |
| `museum` | 18 % | 57.6 mm | Formal, substantial presentation |
| `floating` | 11 % | 35.2 mm | Like gallery, with artwork separation |
| `polaroid` | 10 % / 20 % bottom | 32 / 64 mm | Deliberately bottom-heavy |
| `custom` | — | explicit mm | Supply `ring` yourself |

```python
templates.style_table()   # the above, with mm equivalents, for a settings UI
```

### Overflow presets

Borderless styles paired with an overflow behaviour. These are **not** framing
styles:

```python
templates.build_request_for_preset(screen, media, "immersive")       # crop
templates.build_request_for_preset(screen, media, "immersive_fill")  # contain + blur
```

### Overriding the ring

```python
# Uniform scalar, in mm
templates.build_request(screen, media, "custom", ring=55.0)

# Per side: (left, right, top, bottom), as a framer would state it
templates.build_request(screen, media, "custom", ring=(77.0, 77.0, 40.0, 40.0))
```

The latter is how you cut a Mat Window to the artwork's aspect exactly, giving a
deliberately uneven mount.

## Whitespace

```python
templates.build_request(
    screen, media, "museum",
    whitespace=True, whitespace_gap=8.0,     # mm, uniform
)
```

Whitespace sits **inside** the Mat Window, between the window edge and the
artwork, and stays uniform around it. It compresses only if there is not enough
room.

## Ambient fill

Ambient fill is **derived** — there is no `enabled` flag. It appears when the
Mat Window is fixed and the artwork does not fill it: in the physical branch
always potentially, and virtually at `ring = 0`.

Only its appearance is configured:

```python
templates.build_request(screen, media, "gallery", physical=True,
                        ambient_strategy="blur")     # "solid" | "blur" | "bars"
```

The two ways to resolve a mismatch:

| `overflow` | Fit | Result |
| --- | --- | --- |
| `"fill"` | contain | Artwork fits inside; ambient fill absorbs the residue |
| `"crop"` | cover | Artwork fills the window; edges are lost |

## Mat window offset

A fraction of the slack (a reveal weight), **virtual only**:

```python
templates.build_request(screen, media, "gallery",
                        mat_window_offset=(0.5, 0.75))   # bottom-weighted
```

`(0.5, 0.5)` concentric (default); `0` / `1` flush. Passing a non-centred offset
with a physical `mat_window` raises `ValueError` — a physical offset is achieved
by cutting the mat, so it is expressed by the window rect itself.

## Output

```python
result.frame       # FrameResult  — opening, outer, moulding, orientation
result.mat         # MatResult    — type, outer, window, ring, colour, orientation
result.whitespace  # WhitespaceResult — enabled, authored_gap, applied_gap, outer
result.ambient_fill# AmbientFillResult — present, strategy, region, bars, darken
result.artwork     # ArtworkResult — bounds, presentation, fit, focal_point
result.metrics     # screen_utilisation, overlap (per side), aspects
result.effects     # shadow / border flags
result.branch      # "physical" | "virtual"
result.overflow    # "crop" | "fill"
```

Every group is present in both branches, so a renderer never branches.

### Views

```python
result.to_spec("mm")        # framer cut list; also "cm", "in", "px"
result.display_relative()   # normalised to the screen  (Metixel Software)
result.frame_relative()     # normalised to Frame Outer (website mockup)
result.to_px()              # screen pixels              (renderer)
```

`to_spec()` example:

```python
{
  "unit": "mm",
  "frame_outer": [674.0, 480.0],
  "frame_opening": [594.0, 400.0],
  "moulding": {"left": 40.0, "right": 40.0, "top": 40.0, "bottom": 40.0},
  "mat_type": "physical",
  "mat_window": [514.0, 320.0],
  "mat_ring": {"left": 40.0, "right": 40.0, "top": 40.0, "bottom": 40.0},
  "screen_utilisation": 0.98,
  ...
}
```

## Focal points

Priority: `manual → provided → faces → geometric centre`;
`focal_position="center"` forces the centre.

```python
from metixel.framing_engine import FocalPoint, Face

media = MediaDescriptor(
    width=2, height=3,
    faces=[Face(x=0.4, y=0.2, width=0.2, height=0.3)],
)
templates.build_request(screen, media, "gallery", focal_position="manual",
                        manual_focal_point=FocalPoint(0.4, 0.15))
```

Focal placement only has slack where the Mat Window is larger than the artwork,
so it is inert when a ring is applied and the fit is exact.

## Validation

Invalid input raises `ValueError`:

- `Screen` with non-positive millimetre dimensions, or an `edge_margin` that
  consumes the panel.
- `MediaDescriptor` with non-positive dimensions or an unknown `type`.
- A physical Mat Window outside the screen, or with no positive area.
- A ring that consumes the Frame Opening.
- Returns from raw input, passing a non-centred `mat_window_offset` with a
  physical `mat_window`.

## Using the engine directly

`framing_engine` needs no styles — build a `FramingRequest` in millimetres:

```python
from metixel.framing_engine import (
    FramingRequest, MediaDescriptor, Rect, Screen, calculate_framing,
)

request = FramingRequest(
    screen=Screen(width_mm=518.0, height_mm=324.0),
    media=MediaDescriptor(width=3, height=2),
    mat_window=Rect(2.0, 2.0, 514.0, 320.0),   # physical
    ring=40.0,                                  # uniform 40 mm
    moulding_width=40.0,
)
result = calculate_framing(request)
assert check_invariants(result) == []           # always worth asserting
```

## Determinism

`calculate_framing` is a pure function of its request: no randomness, no time
dependence, no I/O. Identical inputs always produce identical results, and the
test suite enforces it.

---

## Superseded concepts

Earlier revisions modelled a mat as margins from the display edge, in
normalised coordinates. Those concepts — `PhysicalMat`, `Aperture`,
`FramingConfig`, `FRAMING_MODES`, combined-mat limits — **no longer exist** and
must not be used as a model to extend. [Geometry Model](./geometry-model.md) is
the only specification.
