#!/usr/bin/env bash
# Capture real compositor frames while a video is on screen, so the result can be
# LOOKED at rather than inferred.
#
# A clean descriptor count says nothing about whether the picture is visible: the
# canvas leaves the artwork rectangle unpainted so the mpv widget shows through,
# and if that fails the symptom is the poster or a black rectangle — with every
# other signal (FPS, logs, fd counts) looking perfectly healthy.
#
# Waits for `media_type == video` in /run/metixel/current_media.json, then grabs
# N frames at an interval, and reports per-frame image statistics so a black or
# blank capture is obvious before anyone views the PNGs.
#
# Run:  ssh pi@host bash -s < scripts/dev/_capture_frames_pi.sh
set -uo pipefail

FRAMES="${FRAMES:-4}"
INTERVAL="${INTERVAL:-2}"
OUTDIR="${OUTDIR:-/tmp/vidframes}"

export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0
mkdir -p "$OUTDIR"
rm -f "$OUTDIR"/*.png

echo "--- waiting for a video to be the current item (up to 90s) ---"
found=no
for _ in $(seq 1 90); do
	kind=$(sudo -n python3 -c "
import json
try:
    print(json.load(open('/run/metixel/current_media.json')).get('media_type',''))
except Exception:
    print('')
" 2>/dev/null)
	if [ "$kind" = "video" ]; then
		found=yes
		break
	fi
	sleep 1
done
echo "video on screen: $found"
if [ "$found" != yes ]; then
	echo "VERDICT: never caught a video -- cannot judge visibility this way"
	exit 1
fi

echo
echo "--- capturing $FRAMES frames, ${INTERVAL}s apart ---"
for i in $(seq 1 "$FRAMES"); do
	file="$OUTDIR/frame_$i.png"
	grim "$file" 2>/dev/null || echo "  grim failed on frame $i"
	printf 'frame %s: ' "$i"
	sudo -n python3 - "$file" <<'PY' 2>/dev/null || echo "(no stats)"
import sys
from PIL import Image, ImageStat

try:
    img = Image.open(sys.argv[1]).convert("RGB")
except Exception as exc:
    print(f"unreadable ({exc})")
    raise SystemExit(0)

# The artwork region only: the full frame includes the ambient surround and the
# overlay, which are painted either way and would mask a dead video rectangle.
w, h = img.size
box = (int(w * 0.12), int(h * 0.12), int(w * 0.88), int(h * 0.88))
crop = img.crop(box)
stat = ImageStat.Stat(crop)
mean = [round(v, 1) for v in stat.mean]
extrema = crop.convert("L").getextrema()
print(f"{w}x{h} artwork-mean={mean} lum-range={extrema} (mean near [0,0,0] = black rectangle)")
PY
	sleep "$INTERVAL"
done

echo
echo "--- files ---"
ls -la "$OUTDIR"/*.png 2>/dev/null
echo "CAPTURE_DONE"
