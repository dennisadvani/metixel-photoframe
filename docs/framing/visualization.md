# Visualization

`visualize_framing.py` renders framing scenarios as matplotlib figures. It is a
**viewer only**: it imports the engine and draws its geometry, and never
recomputes or modifies framing logic.

Geometry is computed in millimetres and drawn onto a millimetre plane, so a
uniform mm band appears uniform on screen at any panel aspect.

## Requirements

The local venv already contains `matplotlib` (plus transitive `numpy`,
`pillow`). If you recreate the environment:

```powershell
.venv\Scripts\python -m pip install matplotlib
```

## Usage

```powershell
.venv\Scripts\python -m metixel.visualize_framing --no-show               # all figures
.venv\Scripts\python -m metixel.visualize_framing --family physical --no-show
.venv\Scripts\python -m metixel.visualize_framing --orientation portrait --no-show
.venv\Scripts\python -m metixel.visualize_framing --figure P-B-L --no-show
.venv\Scripts\python -m metixel.visualize_framing --no-show --dpi 300
.venv\Scripts\python -m metixel.visualize_framing --no-show --spec        # + cut list
```

Options:

- `--family {physical,virtual,all}` — render one mat family (default `all`).
- `--orientation {landscape,portrait,all}` — render one mounting (default `all`).
- `--figure KEY` — render a single figure, e.g. `P-B-L` or `V-A-P`.
- `--no-show` — write the PNGs and exit without opening a window.
- `--dpi N` — output resolution (default `200`).
- `--spec` — also print the screen/style tables and a sample framer cut list.

Output is written to **`visualisation_output/`**, so generated images stay out of
the repository root. That folder is not committed.

## Figures

Figures are split by mat family, because the two branches behave differently: a
physical mat keeps the Mat Window fixed and grows the frame, while a virtual mat
keeps the Frame Opening fixed and shrinks the visible area.

**Every figure is rendered in both mountings**, so the keys carry an orientation
suffix: `-L` landscape, `-P` portrait.

### Physical mat

| Figure | Contents |
| --- | --- |
| **P-A** | Ring sweep — one Mat Window with each style's ring. The frame grows; the window and active-area use never change. |
| **P-B** | Mat Window shapes — landscape, portrait and square windows, showing how much of the active area each one uses. |
| **P-C** | Crop, and whitespace + ambient — the same window cropped, then with a white band and the ambient residue it leaves. |
| **P-D** | Crop vs fill, side by side on identical geometry. |

### Virtual mat

| Figure | Contents |
| --- | --- |
| **V-A** | Style sweep — the visible area shrinks as the ring grows. |
| **V-B** | Artwork aspects — the Mat Window is cut to the artwork, so the ring is uneven and the shortest ring holds the style target. |
| **V-C** | Crop vs fill, borderless. |

## What each panel draws

Exactly five things, and nothing else:

1. **Frame** — black, the band between Frame Outer and Frame Opening.
2. **Mat** — beige, the band between the Frame Opening and the Mat Window
   (a physical board, or the virtual ring). Absent when there is no mat.
3. **Ambient fill** — light green, inside the Mat Window wherever the artwork
   does not reach. Present only when a residue exists.
4. **Whitespace** — white, the band between the Mat Window and the artwork.
5. **Artwork** — dark green.

The lit panel is never drawn, and neither is the panel behind the mat: there is
no pixel-less screen area to show. The plane is clamped to the Frame Outer plus
a small margin, so nothing outside the frame can appear.

Markers overlay the artwork: a gold `+` at the focal point and red boxes for
detected faces.

Panel titles carry the numbers that matter — Frame Opening, Mat Window, ring
widths, screen utilisation, and caption flags for ambient and whitespace — all
wrapped to fit the panel. On the virtual branch a fit-check line is also shown,
stating the rebate a real frame must provide.

The rebate is **reported as text, never drawn**: it is an advisory fit check for
choosing a physical frame, not a component of the composition. Nothing in the
figure is a rebate, and it must not become one — there is no layer or geometry to
paint. (On the physical branch the engine already enforces
`required_rebate ≤ moulding_width`, so the caption would only restate a
constraint.)

## Customising scenarios

Scenarios are data, not a cross-product grid. Add entries to the factory
functions, or define your own:

```python
from metixel.visualize_framing import Scenario, media_for, ASPECTS

scenarios = [
    Scenario(label="gallery / 3:2", media=media_for(3 / 2), style="gallery"),
    Scenario(label="museum / square", media=media_for(1.0), style="museum"),
    Scenario(label="crop / 9:16", media=media_for(9 / 16), preset="immersive"),
]
```

`Scenario` carries the style (or overflow preset), media, mounting, branch, Mat
Window, ring override, moulding, whitespace and ambient overrides, so most
variations need no new code. For anything more involved, use
`metixel.framing_templates.build_request()` directly and draw the resulting
`FramingResult`.

A scenario the engine legitimately rejects (for example a frame too narrow to
house the required rebate) is annotated on its panel rather than failing the
whole figure. Again, the annotation is text: the rebate is never drawn.

## Keeping it in sync

Regenerate the figures in `visualisation_output/` whenever geometry semantics
change, so the images stay representative. The palette and legend are shared
constants at the top of the file.
