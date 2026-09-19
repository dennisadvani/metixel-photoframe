#!/usr/bin/env bash
# Cost-reducer sweep #3: does the output format (write bandwidth) matter?
#
# The earlier sweeps showed resolution and fps both help, but the per-call cost
# has a large fixed term, which suggests the time is NOT simply proportional to
# bytes written.  This run tests that directly at production resolution by
# changing only the buffer format:
#
#   rgb0   -> 4 bytes/pixel  (baseline: 9.0 MB per frame at 1904x1184)
#   rgb24  -> 3 bytes/pixel
#   rgb565 -> 2 bytes/pixel  (and matches the GL_RGB565 texture the production
#                             backend is meant to use, per rule 5)
#
# If rgb565 is not meaningfully cheaper than rgb0, then the cost is mpv's
# scale+convert work, not the memory write, and the resolution cap is the only
# lever that matters.
#
# The app services must already be stopped (they compete for the Pi's CPU); this
# script only reports whether they are.
#
# Usage: bash scripts/dev/_run_sw_format_sweep.sh
set -uo pipefail

PI="${PI:-pi@192.168.222.122}"
SIZE="${SIZE:-1904x1184}"
SECS="${SECS:-16}"
FPS="${FPS:-30}"
OUT="${OUT:-/tmp/sw_format_sweep.txt}"

: >"$OUT"

echo "########## 1/2 confirm the Pi is quiet ##########" | tee -a "$OUT"
timeout 30 ssh -o BatchMode=yes -o ConnectTimeout=10 "pi@${PI#*@}" \
	"echo -n 'services: '; systemctl is-active metixel-backend metixel-cage metixel-cursor-hider | tr '\n' ' '; echo; echo -n 'loadavg: '; cat /proc/loadavg" \
	>>"$OUT" 2>&1

for fmt in rgb0 rgb24 rgb565; do
	echo "" | tee -a "$OUT"
	echo "########## 2/2 FORMAT=$fmt at $SIZE ##########" | tee -a "$OUT"
	FORMAT="$fmt" SIZE="$SIZE" SECS="$SECS" FPS="$FPS" \
		bash scripts/dev/_run_sw_spike.sh >>"$OUT" 2>&1
done

echo ""
grep -nE '^####|services:|loadavg|bytes_per_frame|hwdec_current|sync_file growth|frames verified|distinct frames|cpu |cpu per frame|VERDICT|hwdec-current|pixelformat' "$OUT" | cut -c1-150
