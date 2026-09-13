# Testing

The engine ships with a pytest suite in `tests/test_framing_engine.py`, grounded in
`docs/geometry-model.md`, which is the authority on expected behaviour.

## Running the tests

`pytest` may not be installed in the local venv. Install and run:

```powershell
.venv\Scripts\python -m pip install pytest

# The suite (pytest.ini scopes collection to it)
.venv\Scripts\python -m pytest -q

# With a failure summary
.venv\Scripts\python -m pytest -q --tb=short
```

`pytest.ini` scopes collection to `tests/`. Without it a bare `pytest` recurses
into `scripts/`, whose standalone probe scripts are named `test_*.py` and fail at
collection.

## What the suite covers

The core matrix is **styles × branch × artwork aspect × whitespace × overflow**,
asserting `check_invariants(result) == []` in every combination. That single
assertion is the executable form of the specification's invariant list.

## Reference geometry

`templates.METIXEL_16_10_1920x1200` — 1920 × 1200 px, 518 × 324 mm active area
in a 528 × 337 mm panel.

```
reference rect      = screen − 2 × edge_margin   = 514 × 320 mm
reference dimension = min(514, 320)              = 320 mm
```

so a style fraction of `f` yields a ring of `f × 320` mm. `gallery` (17.5 %) is
therefore a 56 mm ring.

## Test areas

| Class | Focus |
| --- | --- |
| `TestReferenceDimension` | The reference is the **shorter side**, in either orientation (400 × 600 → 400). Style fractions resolve to mm. |
| `TestScreen` | Panel geometry, per-axis `px_per_mm`, inset by `edge_margin`, rejection of degenerate sizes. |
| `TestClassification` | `classify_aspect` threshold boundaries. |
| `TestVirtualBranch` | Frame Opening fixed to the screen; Mat Window cut to the artwork; the **shortest ring equals the style target**; no ambient fill when `ring > 0`. |
| `TestPhysicalBranch` | Mat Window fixed and style-independent; the **frame grows**; the ring is uniform; ambient fill absorbs the mismatch; crop vs fill. |
| `TestWhitespace` | The band runs **outside** the artwork, is uniform, lies inside the Mat Window, and forces the crop presentation. Casing/presentation defaults and user overrides. |
| `TestStyles` | Every documented style exists, sits inside its percentage range, and ordering is monotonic. |
| `TestFocalPoints` | Priority `manual → provided → faces → centre`; bounded shifts; **no effect when there is no slack**. |
| `TestConstraints` | Hard `ValueError`s: window off-screen, oversized ring, negative ring, degenerate window, invalid offset, invalid ambient strategy. |
| `TestPanelCoverage` | The rebate rule: the panel must be covered, the rebate must fit inside the moulding, and both mountings hold the panel. |
| `TestPortraitMounting` | The panel rotated 90°: axis swap, unchanged reference dimension, matching ring width, invariants in both mountings. |
| `TestRingProportions` | Virtual and physical mats read the same: the **ring-to-artwork ratio** matches, via the `1 / (1 − 2f)` multiplier. |
| `TestOutputs` | All groups reported in both branches; `to_spec` units; `to_px`; `frame_relative` / `display_relative`. |
| `TestInvariantMatrix` | The full cross-product sweep plus nesting and determinism. |
| `TestAspectPreservation` | Artwork keeps its aspect exactly — in mm the ratio compares directly, so there is no pixel-space compensation. |

## Model consequences the suite asserts explicitly

These are easy to mistake for bugs:

1. **Focal placement only has slack where the Mat Window is larger than the
   artwork.** With `ring > 0` the window is cut to the artwork, so the fit is
   exact and nothing can shift. It bites at `ring = 0` and in the physical
   branch.
2. **Whitespace sits inside the Mat Window**, between the window edge and the
   artwork — not between the artwork and the ring. The band is measured from
   `ws_outer` to the artwork, so it is always exactly `gap`; the leftover is
   ambient fill.
3. **Whitespace and ambient fill never coexist.** A white border alongside
   ambient fill reads as a mistake, so whitespace forces `overflow="crop"`.
4. **The physical ring is scaled** so a uniform mat matches the virtual mat's
   ring-to-artwork ratio. Without it the physical mat reads far thinner.

## `check_invariants()`

`metixel.framing_engine.check_invariants(result)` returns a list of violated invariants.
It is the canonical assertion for new tests:

- nesting: `Frame Outer ⊇ Frame Opening ⊇ Mat Window ⊇ presentation ⊇ artwork`
- rings are never negative and sum to the slack on each axis
- every band is non-negative
- a physical Mat Window lies on the screen
- ambient fill is absent in the virtual branch whenever a ring is applied
- the presentation always lies within the Mat Window

Extend it when you add an invariant to the spec, then assert it in the matrix.

## Adding tests

- Reuse the helpers at the top of the file: `media(aspect)`, `request(...)`,
  `close(a, b)`. They exist to keep new cases short.
- Parametrise over `STYLE_NAMES`, `ASPECTS`, and the `physical` / `whitespace` /
  `overflow` flags for cross-product checks, as `TestInvariantMatrix` does.
- New assertions must be grounded in `docs/geometry-model.md` or a named module
  constant — never hard-coded against incidental values.
- Keep determinism guarantees: identical requests must produce identical
  results, with no randomness, ordering or external state.
