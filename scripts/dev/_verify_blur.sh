#!/bin/bash
# Verify the blur backdrop: does it render, is it blurred, and is it built once?
set -e

echo "=== switch to blur mode via the API (no restart) ==="
python3 - <<'PY'
import json, urllib.request
body = json.dumps({
    "image_duration_seconds": 3, "transition_duration_ms": 2500,
    "transition_style": "crossfade", "fit_mode": "contain",
    "smart_cover": True, "shuffle": True,
    "ambient_strategy": "blur", "ambient_color": "#1717d3",
    "ambient_blur_radius": 24, "ambient_darken": 0.35,
    "matte_color": [20, 20, 20],
}).encode()
req = urllib.request.Request("http://127.0.0.1:8080/api/config/slideshow",
                             data=body, method="PUT",
                             headers={"Content-Type": "application/json"})
print("   PUT ->", urllib.request.urlopen(req).status)
PY

sleep 8
echo
echo "=== capture and characterise the backdrop ==="
PYTHONPATH=/opt/metixel/live/src python3 - <<'PY'
import subprocess, time
from PIL import Image, ImageFilter, ImageStat

def capture(path):
    subprocess.run(["grim", path], check=True,
        env={"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0",
             "PATH": "/usr/bin:/bin"})

capture("/tmp/blur.png")
im = Image.open("/tmp/blur.png").convert("RGB")
w, h = im.size
print(f"  screen {w}x{h}")

# The ambient BAND is where the effect is visible: outside the artwork.
# A blurred backdrop there should be soft (low local variance) and coloured
# like the photo, not a flat single colour.
band = im.crop((0, 0, 120, h))
stat = ImageStat.Stat(band)
print(f"  left band mean RGB : {[round(v) for v in stat.mean]}")
print(f"  left band stddev   : {[round(v) for v in stat.stddev]}")
unique = len(band.getcolors(maxcolors=1_000_000) or [])
print(f"  distinct colours in band: {unique}")

# A flat solid fill would be ONE colour (stddev ~0).  A blur has many.
if unique < 5:
    print("  RESULT: band is FLAT — blur did not render")
else:
    print("  RESULT: band has structure — the blurred backdrop is rendering")

# Blur check: sharp edges have high local variance.  Compare the band against
# the artwork region, which should be much sharper.
art = im.crop((w // 2 - 200, h // 2 - 200, w // 2 + 200, h // 2 + 200))
band_edge = band.filter(ImageFilter.FIND_EDGES)
art_edge = art.filter(ImageFilter.FIND_EDGES)
print(f"  band edge energy (soft if low): {round(ImageStat.Stat(band_edge).mean[0], 2)}")
print(f"  artwork edge energy (sharp)   : {round(ImageStat.Stat(art_edge).mean[0], 2)}")
PY
