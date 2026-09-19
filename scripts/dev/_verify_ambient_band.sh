#!/usr/bin/env bash
# Did the ambient fill go black as the next item transitioned in?
#
# Samples the AMBIENT margins only — the bands outside the artwork rectangle —
# because that is exactly where the defect appears and it is far more sensitive
# than the whole frame: video content sits in the middle and would swamp a
# whole-frame black fraction.
#
# The trigger is a video ending, so this samples across several slides rather
# than trying to time one: a crossfade is 2s, which at 0.3s sampling is 6-7
# samples, so an intermittent black band cannot hide between samples.
#
# Run:  ssh pi@host bash -s < scripts/dev/_verify_ambient_band.sh
set -uo pipefail

export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0

SECONDS_TO_RUN="${SECONDS_TO_RUN:-90}"
INTERVAL="${INTERVAL:-0.3}"
OUT=/tmp/ambientband
mkdir -p "$OUT"
rm -f "$OUT"/*.png

echo "sampling the ambient margins for ${SECONDS_TO_RUN}s..."
start=$(date +%s)
i=0
worst=0
worst_frame=""
while [ $(( $(date +%s) - start )) -lt "$SECONDS_TO_RUN" ]; do
	i=$((i + 1))
	file="$OUT/f$i.png"
	grim "$file" >/dev/null 2>&1 || continue
	frac=$(sudo -n python3 - "$file" <<'PY' 2>/dev/null || echo ""
import sys

from PIL import Image

img = Image.open(sys.argv[1]).convert("RGB")
w, h = img.size
px = img.load()


def lum(x, y):
    p = px[x, y]
    return (p[0] + p[1] + p[2]) / 3


# The four margins, inside the panel edge (the outermost rows/columns are
# compositor furniture on some setups).
bands = [
    (20, 20, 180, h - 20),      # left
    (w - 180, 20, w - 20, h - 20),  # right
    (20, 20, w - 20, 120),      # top
    (20, h - 120, w - 20, h - 20),  # bottom
]
dark = 0
total = 0
for x0, y0, x1, y1 in bands:
    for y in range(y0, y1, 12):
        for x in range(x0, x1, 12):
            total += 1
            if lum(x, y) < 12:
                dark += 1
print(f"{dark / max(1, total):.3f}")
PY
)
	[ -z "$frac" ] && continue
	if sudo -n python3 -c "import sys; sys.exit(0 if float('$frac') > float('$worst') else 1)"; then
		worst="$frac"
		worst_frame="$file"
	fi
	printf 'sample %3d  ambient_black=%s\n' "$i" "$frac"
	sleep "$INTERVAL"
done

echo
echo "samples taken    : $i"
echo "worst ambient    : $worst  (frame $worst_frame)"
echo
echo "--- video events in the log ---"
sudo -n grep -aE "Video playback (started|ended)" /opt/metixel/data/logs/metixel-frontend.log |
	tail -8 | cut -c1-110

if sudo -n python3 -c "import sys; sys.exit(0 if float('$worst') > 0.5 else 1)"; then
	echo "VERDICT: ambient went predominantly BLACK at least once"
	cp "$worst_frame" /tmp/ambient_worst.png 2>/dev/null || true
	echo "worst frame copied to /tmp/ambient_worst.png"
else
	echo "VERDICT: ambient stayed filled (worst $worst)"
fi
echo "AMBIENT_BAND_DONE"
