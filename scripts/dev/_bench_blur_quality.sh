#!/bin/bash
# Settle the quality question properly: measure BLOCK EDGES, not flatness.
#
# A blocky (low-res) blur shows periodic discontinuities at the downscale grid
# pitch (target/radius px).  A true blur's second derivative is smooth.  So:
# compute the second difference along a row and look for spikes at the grid
# pitch specifically, versus the median.
set -e
python3 - <<'PY'
from PIL import Image, ImageFilter

W, H = 1920, 1200
src = Image.new("RGB", (W, H))
px = src.load()
# A smooth gradient, like a sky — the case where blocking is most visible.
for y in range(H):
    for x in range(W):
        px[x, y] = (int(255 * x / W), int(255 * y / H), 128)

def current(radius=24):
    sw, sh = max(1, W // radius), max(1, H // radius)
    return src.resize((sw, sh), Image.BILINEAR).resize((W, H), Image.BILINEAR)

def analyse(label, im):
    im = im.convert("L")
    row = [im.getpixel((x, 600)) for x in range(W)]
    # second difference
    d2 = [abs(row[i + 1] - 2 * row[i] + row[i - 1]) for i in range(1, W - 1)]
    median = sorted(d2)[len(d2) // 2]
    peak = max(d2)
    # Where are the biggest spikes?
    worst = sorted(range(len(d2)), key=lambda i: -d2[i])[:8]
    print(f"  {label:32s} median 2nd-diff={median:3d}  peak={peak:3d}  "
          f"peak/median={peak / max(median, 1):6.1f}x")
    return peak / max(median, 1)

print("=== block-edge test on a smooth gradient (lower ratio = smoother) ===")
r1 = analyse("current downscale/upscale", current(24))
r2 = analyse("PIL BoxBlur(24)", src.filter(ImageFilter.BoxBlur(24)))
r3 = analyse("PIL GaussianBlur(24)", src.filter(ImageFilter.GaussianBlur(24)))
r4 = analyse("double downscale (2 passes)", current(24).filter(ImageFilter.GaussianBlur(8)))

print()
print("=== same test on a photo-like source ===")
import math
photo = Image.new("RGB", (W, H))
pp = photo.load()
for y in range(H):
    for x in range(W):
        v = 128 + 60 * math.sin(x / 40.0) + 40 * math.sin(y / 25.0)
        pp[x, y] = (int(v), int(v * 0.8), int(v * 0.6))
g1 = analyse("current", photo.resize((W, H)).resize((W // 24, H // 24), Image.BILINEAR).resize((W, H), Image.BILINEAR))
g2 = analyse("PIL BoxBlur(24)", photo.filter(ImageFilter.BoxBlur(24)))
PY
