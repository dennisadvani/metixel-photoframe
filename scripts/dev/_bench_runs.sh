#!/bin/bash
# Render the SAME real photo through each blur and report the visual defect
# directly: downscale-based blur quantises to a coarse grid, so adjacent pixels
# become IDENTICAL in runs of the grid pitch.  Count run lengths.
set -e
PYTHONPATH=/opt/metixel/live/src python3 - <<'PY'
import glob
from PIL import Image, ImageFilter

# Use a real cached photo so the texture is representative.
candidates = sorted(glob.glob("/opt/metixel/data/cache/images/*"))
if not candidates:
    candidates = sorted(glob.glob("/opt/metixel/data/media/**/*.jpg", recursive=True))
if not candidates:
    raise SystemExit("no sample image found")
path = candidates[0]
print(f"source: {path}")

W, H = 1920, 1200
src = Image.open(path).convert("RGB").resize((W, H), Image.LANCZOS)

def current(radius=24):
    sw, sh = max(1, W // radius), max(1, H // radius)
    return src.resize((sw, sh), Image.BILINEAR).resize((W, H), Image.BILINEAR)

def run_lengths(im):
    """Longest run of identical adjacent pixels along many rows.

    A real blur varies every pixel.  A blur built by downscaling produces runs
    as long as the grid pitch — that is the "blocky" look.
    """
    g = im.convert("L")
    longest = 0
    total_runs = 0
    for y in range(0, H, 40):
        row = [g.getpixel((x, y)) for x in range(W)]
        run = 1
        for i in range(1, len(row)):
            if row[i] == row[i - 1]:
                run += 1
            else:
                longest = max(longest, run)
                total_runs += 1
                run = 1
        longest = max(longest, run)
    return longest, total_runs

print()
print("=== longest identical-pixel run along a row (grid pitch = W/radius = 80) ===")
for label, im in (
    ("current downscale/upscale", current(24)),
    ("PIL BoxBlur(24)", src.filter(ImageFilter.BoxBlur(24))),
    ("PIL GaussianBlur(24)", src.filter(ImageFilter.GaussianBlur(24))),
    ("PIL GaussianBlur(8)", src.filter(ImageFilter.GaussianBlur(8))),
):
    longest, runs = run_lengths(im)
    print(f"  {label:30s} longest run={longest:4d} px across {runs} runs")
PY
