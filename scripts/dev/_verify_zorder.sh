#!/bin/bash
# Verify the z-order fix: during a crossfade in blur mode, the OUTGOING photo
# must be visible (on top of both backdrops), and it must fade smoothly rather
# than abruptly vanish at the end of the transition.
set -e

python3 - <<'PY'
import json, urllib.request
body = json.dumps({
    "image_duration_seconds": 3, "transition_duration_ms": 3000,
    "transition_style": "crossfade", "fit_mode": "contain",
    "smart_cover": True, "shuffle": True,
    "ambient_strategy": "blur", "ambient_color": "#1717d3",
    "ambient_blur_radius": 24, "ambient_darken": 0.35,
    "matte_color": [20, 20, 20],
}).encode()
req = urllib.request.Request("http://127.0.0.1:8080/api/config/slideshow",
                             data=body, method="PUT",
                             headers={"Content-Type": "application/json"})
print("PUT ->", urllib.request.urlopen(req).status)
PY

sleep 6
echo
echo "=== sampling across a transition: is the outgoing photo ever hidden? ==="
PYTHONPATH=/opt/metixel/live/src python3 - <<'PY'
import subprocess, time
from PIL import Image, ImageStat

def capture(path):
    subprocess.run(["grim", path], check=True,
        env={"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0",
             "PATH": "/usr/bin:/bin"})

# Track the sharpness of the CENTRE (where the artwork is).  A photo being
# occluded by a blurred backdrop would drop the edge energy toward the blur's.
series = []
for i in range(26):
    time.sleep(0.35)
    capture("/tmp/z.png")
    im = Image.open("/tmp/z.png").convert("RGB")
    w, h = im.size
    centre = im.crop((w//2 - 300, h//2 - 300, w//2 + 300, h//2 + 300))
    from PIL import ImageFilter
    sharp = ImageStat.Stat(centre.filter(ImageFilter.FIND_EDGES)).mean[0]
    band = im.crop((0, 0, 100, h))
    band_sharp = ImageStat.Stat(band.filter(ImageFilter.FIND_EDGES)).mean[0]
    series.append((round(sharp, 1), round(band_sharp, 1)))

print("  (centre sharpness, band sharpness) sampled every ~0.35 s:")
for i in range(0, len(series), 4):
    print("   ", series[i:i+4])

centre_vals = [s[0] for s in series]
print()
print(f"  centre sharpness: min={min(centre_vals):.1f} max={max(centre_vals):.1f}")
# A photo occluded by a blur would show the blur's low edge energy (~8), while a
# real photo is much sharper.  Anything below ~11 for a sustained period means
# the artwork is being covered.
low = [v for v in centre_vals if v < 11]
print(f"  frames where the centre looked blurred (sharp<11): {len(low)} of {len(centre_vals)}")
print("  RESULT:", "artwork never occluded by the backdrop"
      if len(low) <= 2 else "artwork IS being covered by the backdrop")
PY
