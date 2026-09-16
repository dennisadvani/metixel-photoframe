#!/bin/bash
# Verify the BoxBlur backdrop: no blocking, and reasonable CPU.
set -e

python3 - <<'PY'
import json, urllib.request
body = json.dumps({
    "image_duration_seconds": 4, "transition_duration_ms": 2000,
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

sleep 8
echo
echo "=== capture and magnify the band, then test for blocking ==="
PYTHONPATH=/opt/metixel/live/src python3 - <<'PY'
import subprocess
from PIL import Image

subprocess.run(["grim", "/tmp/nb.png"], check=True,
    env={"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0",
         "PATH": "/usr/bin:/bin"})

im = Image.open("/tmp/nb.png").convert("RGB")
w, h = im.size
# The left band is where the backdrop lives.
band = im.crop((0, 400, 160, 800))
# Magnify 4x with NEAREST so any block structure is visible.
band.resize((640, 1600), Image.NEAREST).save("/tmp/nb_zoom.png")

# Blocking detector: a low-res upsample has long runs of IDENTICAL pixels
# horizontally.  A real blur varies nearly every pixel.
g = band.convert("L")
longest = 0
runs = 0
identical = 0
total = 0
for y in range(g.height):
    row = [g.getpixel((x, y)) for x in range(g.width)]
    run = 1
    for i in range(1, len(row)):
        total += 1
        if row[i] == row[i - 1]:
            run += 1
            identical += 1
        else:
            longest = max(longest, run)
            runs += 1
            run = 1
    longest = max(longest, run)

print(f"  band {band.size}, 4x zoom written to /tmp/nb_zoom.png")
print(f"  longest identical run: {longest} px  (old downscale block pitch was ~13 px here)")
print(f"  identical adjacent pixels: {100*identical//max(1,total)}%")
print()
if longest <= 6:
    print("  RESULT: no blocking — the backdrop is a real filter")
else:
    print(f"  RESULT: possible blocking (run of {longest} px)")
PY
