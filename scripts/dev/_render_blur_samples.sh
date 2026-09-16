#!/bin/bash
# Write the actual blurred outputs to files so they can be LOOKED AT, and
# magnify a crop 4x so any blocking is unmistakable.
set -e
PYTHONPATH=/opt/metixel/live/src python3 - <<'PY'
import glob
from PIL import Image, ImageFilter

candidates = sorted(glob.glob("/opt/metixel/data/cache/images/*"))
if not candidates:
    raise SystemExit("no cached image")
src = Image.open(candidates[0]).convert("RGB")
W, H = 1920, 1200
src = src.resize((W, H), Image.LANCZOS)

def current(radius=24):
    sw, sh = max(1, W // radius), max(1, H // radius)
    return src.resize((sw, sh), Image.BILINEAR).resize((W, H), Image.BILINEAR)

outputs = {
    "current": current(24),
    "box": src.filter(ImageFilter.BoxBlur(24)),
    "gauss": src.filter(ImageFilter.GaussianBlur(24)),
}
# Magnify a 300x200 crop 4x — blocking is obvious at this zoom.
box = (700, 500, 1000, 700)
for name, im in outputs.items():
    im.crop(box).resize((1200, 800), Image.NEAREST).save(f"/tmp/blur_{name}_zoom.png")
    im.save(f"/tmp/blur_{name}.png")
    print(f"  wrote /tmp/blur_{name}_zoom.png and /tmp/blur_{name}.png")

# Also build a side-by-side strip of the same crop, magnified.
strip = Image.new("RGB", (1200, 800 * 3 + 40), (0, 0, 0))
for i, (name, im) in enumerate(outputs.items()):
    strip.paste(im.crop(box).resize((1200, 800), Image.NEAREST), (0, i * (800 + 20)))
strip.save("/tmp/blur_compare.png")
print("  wrote /tmp/blur_compare.png (top=current, middle=box, bottom=gaussian)")
PY
