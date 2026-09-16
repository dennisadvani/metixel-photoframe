#!/bin/bash
# Does selecting "black bars" actually blacken the band? No restart.
PID=$(pgrep -f "mode frontend" | head -1)

sample() {
    OUT=/tmp/bars_probe.png
    rm -f "$OUT"
    XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 grim "$OUT" 2>/dev/null
    PYTHONPATH=/opt/metixel/live/src python3 - "$OUT" <<'PY'
import sys
from collections import Counter
from PIL import Image
im = Image.open(sys.argv[1]).convert("RGB")
w, h = im.size
cols = [im.getpixel((x, y)) for x in (2, 4, 6) for y in range(0, h, max(1, h // 60))]
c, n = Counter(cols).most_common(1)[0]
print(f"   band colour: {c}  (x{n})")
PY
}

echo "process start: $(ps -o lstart= -p $PID)"

echo
echo "=== strategy = solid, colour #1717d3 ==="
python3 - <<'PY'
import json, urllib.request
body = json.dumps({"fit_mode": "contain", "smart_cover": True, "shuffle": True,
                   "image_duration_seconds": 5, "transition_duration_ms": 1500,
                   "transition_style": "crossfade", "ambient_strategy": "solid",
                   "ambient_color": "#1717d3", "matte_color": [20, 20, 20]}).encode()
req = urllib.request.Request("http://127.0.0.1:8080/api/config/slideshow", data=body,
                             method="PUT", headers={"Content-Type": "application/json"})
print("   PUT ->", urllib.request.urlopen(req).status)
PY
sleep 24
sample

echo
echo "=== strategy = bars (colour left at #1717d3 on purpose) ==="
python3 - <<'PY'
import json, urllib.request
body = json.dumps({"fit_mode": "contain", "smart_cover": True, "shuffle": True,
                   "image_duration_seconds": 5, "transition_duration_ms": 1500,
                   "transition_style": "crossfade", "ambient_strategy": "bars",
                   "ambient_color": "#1717d3", "matte_color": [20, 20, 20]}).encode()
req = urllib.request.Request("http://127.0.0.1:8080/api/config/slideshow", data=body,
                             method="PUT", headers={"Content-Type": "application/json"})
print("   PUT ->", urllib.request.urlopen(req).status)
PY
sleep 24
sample

echo
echo "  process start (must be unchanged): $(ps -o lstart= -p $PID)"
