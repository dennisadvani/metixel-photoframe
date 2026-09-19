#!/usr/bin/env bash
# Run the QOpenGLWidget paint probe in both modes and capture each with grim.
#
# Takes over the display briefly: metixel-cage must stop so this probe's own cage
# can hold the DRM master.  metixel-cage is restarted at the end, including on
# failure, so a stuck probe cannot leave the frame dark.
#
# Run:  ssh pi@host bash -s < scripts/dev/_run_paint_probe_pi.sh
set -uo pipefail

export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0

restore() {
	echo "--- restoring metixel-cage ---"
	sudo -n systemctl start metixel-cage
	sleep 6
	systemctl is-active metixel-cage
}
trap restore EXIT

sudo -n systemctl stop metixel-cage
sleep 3
echo "cage stopped; metixel-cage=$(systemctl is-active metixel-cage)"

for mode in gl qpainter stacked; do
	echo
	echo "######## mode=$mode ########"
	sudo -n systemctl reset-failed "metixel-paintprobe-$mode" 2>/dev/null
	sudo -n systemd-run --quiet --unit="metixel-paintprobe-$mode" --collect \
		--uid=pi --gid=pi \
		--property=SupplementaryGroups='video render input tty' \
		--property=Environment=XDG_RUNTIME_DIR=/run/user/1000 \
		--property=WorkingDirectory=/tmp \
		/usr/bin/cage -d -- /usr/bin/python3 /tmp/_probe_qpainter_on_glwidget.py \
		--mode "$mode" --seconds 12
	sleep 6
	grim "/tmp/paint_$mode.png" >/dev/null 2>&1 && echo "captured /tmp/paint_$mode.png" || echo "grim FAILED"
	sleep 8

	echo "--- captured content ---"
	sudo -n python3 - "/tmp/paint_$mode.png" <<'PY' 2>/dev/null || echo "(no analysis)"
import sys
from collections import Counter

from PIL import Image

img = Image.open(sys.argv[1]).convert("RGB")
# Downsample: we only care which colours are present, not pixel-exact counts.
small = img.resize((160, 100))
counts = Counter(small.getdata())
total = sum(counts.values())
for colour, n in counts.most_common(4):
    print(f"  {colour}: {n * 100 // total}%")
PY
done

echo
echo "PAINT_PROBE_DONE"
