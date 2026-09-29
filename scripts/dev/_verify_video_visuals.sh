#!/usr/bin/env bash
# Verify the two video visual fixes on the Pi.
#
#   1. the hairline seam: capture a portrait video and look for a 1px black column
#      at the artwork edge (the defect measured at x=1297, pure black);
#   2. the black flash at video start: detect a NEW "Video playback started" in
#      the log, then capture immediately and check that the artwork region shows
#      the POSTER rather than black before the video appears.
#
# The flash lasted only a frame or two, so it cannot be caught by blind sampling:
# the capture has to be triggered by the log line.  Both checks report numbers
# rather than relying on someone looking at the screen.
#
# Run:  ssh pi@host bash -s < scripts/dev/_verify_video_visuals.sh
set -uo pipefail

export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0

LOG=/opt/metixel/data/logs/metixel-frontend.log
OUT=/tmp/vidvisuals
mkdir -p "$OUT"
rm -f "$OUT"/*.png

analyse() {
	sudo -n python3 - "$1" <<'PY'
import sys

from PIL import Image

img = Image.open(sys.argv[1]).convert("RGB")
w, h = img.size
px = img.load()


def lum(p):
    return (p[0] + p[1] + p[2]) / 3


# (a) seam: a column that is almost entirely black while BOTH neighbours are not.
#     That is the signature of a one-pixel letterbox bar, and it is what a plain
#     "count black pixels" test cannot distinguish from dark video content.
seam_cols = []
for x in range(1, w - 1):
    dark = sum(1 for y in range(150, h - 150, 25) if lum(px[x, y]) < 12)
    total = len(range(150, h - 150, 25))
    if dark < total * 0.9:
        continue
    left = sum(1 for y in range(150, h - 150, 25) if lum(px[x - 1, y]) < 12)
    right = sum(1 for y in range(150, h - 150, 25) if lum(px[x + 1, y]) < 12)
    if left < total * 0.2 and right < total * 0.2:
        seam_cols.append(x)

# (b) black flash: the fraction of the frame that is pure black.  A revealed but
#     unpainted video rectangle is a large solid black block, far beyond anything
#     ordinary content produces across the whole screen.
black = sum(1 for y in range(0, h, 4) for x in range(0, w, 4) if lum(px[x, y]) < 12)
sampled = len(range(0, h, 4)) * len(range(0, w, 4))
print(f"black_frac={black / sampled:.3f} seam_cols={seam_cols[:6]}")
PY
}

echo "########## 1. wait for a NEW video to start, then capture the transition ##########"
before=$(sudo -n grep -ac "Video playback started" "$LOG" 2>/dev/null || echo 0)
echo "existing 'Video playback started' count: $before"

for _ in $(seq 1 240); do
	now=$(sudo -n grep -ac "Video playback started" "$LOG" 2>/dev/null || echo 0)
	[ "$now" -gt "$before" ] && break
	sleep 1
done
echo "detected (count now $now) — capturing immediately"

for i in $(seq 1 12); do
	grim "$OUT/start_$i.png" >/dev/null 2>&1 || true
	printf 'start %2d: ' "$i"
	analyse "$OUT/start_$i.png" 2>/dev/null || echo "(unreadable)"
done

echo
echo "########## 2. capture steady playback for the seam check ##########"
sleep 4
for i in $(seq 1 6); do
	grim "$OUT/steady_$i.png" >/dev/null 2>&1 || true
	printf 'steady %2d: ' "$i"
	analyse "$OUT/steady_$i.png" 2>/dev/null || echo "(unreadable)"
	sleep 1
done

echo
echo "########## reveal timing ##########"
sudo -n grep -aE "Video playback started|Video surface revealed" "$LOG" |
	tail -8 | cut -c1-120
echo "VIDEO_VISUALS_DONE"
