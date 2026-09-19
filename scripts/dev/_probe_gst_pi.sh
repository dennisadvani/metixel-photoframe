#!/usr/bin/env bash
# Probe what the Pi can actually do with GStreamer.
#
# Run as:  ssh pi@host bash -s < scripts/dev/_probe_gst_pi.sh
#
# NOTE: deliberately piped over stdin rather than passed as an inline ssh
# argument.  A long inline double-quoted ssh command corrupts its own escaping
# and silently produced a bogus "everything is MISSING" result once already.
set -uo pipefail

G=/usr/bin/gst-inspect-1.0

echo "=== version ==="
"$G" --version | head -2

echo
echo "=== ALL v4l2-ish elements ==="
"$G" 2>/dev/null | grep -iE '^v4l2|^.*v4l2' | sort

echo
echo "=== ALL decoders (element, klass) ==="
"$G" 2>/dev/null | awk -F: '/^[a-z0-9_]+:/ {print $1}' | while read -r e; do
	case "$e" in
	*dec | *decode) printf '%s\n' "$e" ;;
	esac
done | sort -u

echo
echo "=== plugins loaded ==="
"$G" 2>/dev/null | sort | tr '\n' ' '
echo

echo
echo "=== /dev/video* and codec devices ==="
ls -1 /dev/video* 2>/dev/null || echo "no /dev/video*"
ls -1 /dev/dri 2>/dev/null || echo "no /dev/dri"
echo "v4l2-ctl presence:"; command -v v4l2-ctl || echo "  v4l2-ctl not installed"

echo
echo "=== relevant packages ==="
dpkg -l 2>/dev/null | awk '/^ii/ {print $2}' |
	grep -E 'gstreamer|gst-|python3-gi|gir1.2-gst|libav|v4l|libcamera' | sort

echo
echo "=== python bindings ==="
printf 'python3-gi         : '
dpkg -l python3-gi >/dev/null 2>&1 && echo INSTALLED || echo MISSING
printf 'gir1.2-gstreamer   : '
dpkg -l gir1.2-gstreamer-1.0 >/dev/null 2>&1 && echo INSTALLED || echo MISSING
printf 'gi import          : '
/usr/bin/python3 -c 'import gi; print("OK", gi.__file__)' 2>&1 | tail -1

echo
echo "=== can mpv/GStreamer see the H.264 V4L2 device? ==="
/usr/bin/python3 - <<'PY'
import glob
for p in sorted(glob.glob("/sys/class/video4linux/video*/name")):
    try:
        with open(p) as fh:
            print(" ", p.split("/")[4], "->", fh.read().strip())
    except OSError as exc:
        print(" ", p, "unreadable:", exc)
PY
