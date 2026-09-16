#!/bin/bash
# Benchmark blur options at display resolution, on this hardware.
# This is the number that decides the implementation: it runs in the render
# thread, once per slide, and must not stall a frame noticeably.
python3 - <<'PY'
import time
from PIL import Image, ImageFilter

W, H = 1920, 1200
# A textured source, so the timings are not optimised away on a flat image.
src = Image.new("RGB", (W, H))
px = src.load()
for y in range(0, H, 4):
    for x in range(0, W, 4):
        v = (x * y) % 256
        for dy in range(4):
            for dx in range(4):
                if x + dx < W and y + dy < H:
                    px[x + dx, y + dy] = (v, (v * 3) % 256, (v * 7) % 256)

def timeit(label, fn, repeat=3):
    best = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        dt = (time.perf_counter() - t0) * 1000
        best = dt if best is None else min(best, dt)
    print(f"  {label:42s} {best:7.1f} ms")
    return best

print(f"=== blur timings at {W}x{H} (best of 3) ===")
print()

# Current implementation: downscale then upscale.
def current(radius=24):
    sw, sh = max(1, W // radius), max(1, H // radius)
    small = src.resize((sw, sh), Image.BILINEAR)
    return small.resize((W, H), Image.BILINEAR)

timeit("current: downscale->upscale (radius 24)", current)

# Full-resolution box blur — separable running sum.
timeit("PIL BoxBlur(radius=24)", lambda: src.filter(ImageFilter.BoxBlur(24)))
timeit("PIL GaussianBlur(radius=24)", lambda: src.filter(ImageFilter.GaussianBlur(24)))

# Three box passes approximate a Gaussian closely.
def triple_box(radius=24):
    out = src
    for _ in range(3):
        out = out.filter(ImageFilter.BoxBlur(radius))
    return out

timeit("3 x BoxBlur(radius=8)", lambda: triple_box(8))

print()
print("=== quality check: how many distinct colours survive? ===")
print("  (a blocky blur produces large flat plateaus; a good one keeps gradients)")
for label, fn in (
    ("current downscale/upscale", current),
    ("BoxBlur(24)", lambda: src.filter(ImageFilter.BoxBlur(24))),
    ("GaussianBlur(24)", lambda: src.filter(ImageFilter.GaussianBlur(24))),
):
    out = fn().convert("RGB")
    colours = len(out.getcolors(maxcolors=2_000_000) or [])
    print(f"  {label:30s} {colours:>9,} distinct colours")

print()
print("=== downscale/upscale blocking: sample a horizontal run ===")
out = current(24).convert("RGB")
row = [out.getpixel((x, 600))[0] for x in range(0, W, 8)]
# The block size is W/radius = 80 px, so every 10th sample of an 8-px step is a
# block boundary.  Report how often a step repeats identically (plateaus).
deltas = [row[i+1] - row[i] for i in range(len(row) - 1)]
flat = sum(1 for d in deltas if d == 0)
print(f"  identical adjacent steps: {flat}/{len(deltas)} ({100*flat//max(1,len(deltas))}%)")
g = src.filter(ImageFilter.GaussianBlur(24)).convert("RGB")
rowg = [g.getpixel((x, 600))[0] for x in range(0, W, 8)]
deltas_g = [rowg[i+1] - rowg[i] for i in range(len(rowg) - 1)]
flatg = sum(1 for d in deltas_g if d == 0)
print(f"  Gaussian, identical steps: {flatg}/{len(deltas_g)} ({100*flatg//max(1,len(deltas_g))}%)")
PY
