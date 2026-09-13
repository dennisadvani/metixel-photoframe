#!/bin/bash
# GATE-1 (HEVC on Pi 5): which decoder ACTUALLY runs in hardware?
#
# Decodes the generated HEVC clip with each candidate hwdec and reports:
#   - system-wide CPU over the run (from /proc/stat deltas, not `top`)
#   - mpv's own verdict: hardware or software, and which method
#
# vo=null deliberately: this measures the DECODER. The GL display path is a
# separate question answered on real hardware with the full stack.
#
# Requires: /tmp/gate1/test_hevc_1080p.mp4 (run pi_gate1_make_hevc.sh first),
# and Metixel services stopped so nothing competes.

set -u

HEVC=/tmp/gate1/test_hevc_1080p.mp4
H264=/opt/metixel/data/media/sample_media/landscape/13131508_1920_1080_24fps.mp4
DURATION=12
LOGDIR=/tmp/gate1
mkdir -p "$LOGDIR"

if [ ! -f "$HEVC" ]; then
    echo "ERROR: $HEVC missing — run pi_gate1_make_hevc.sh first"; exit 1
fi

# Refuse to measure on a busy system: Metixel running in the background was
# exactly what invalidated the first attempt.
busy=$(ps -eo comm | grep -cE '^(cage|metixel|mpv|Xwayland)$' || true)
if [ "$busy" -gt 0 ]; then
    echo "WARNING: Metixel processes are running — numbers will be inflated:"
    ps -eo comm | grep -E '^(cage|metixel|mpv|Xwayland)$' | sed 's/^/    /'
    echo
fi

busy_ticks() { echo "$1" | awk '{print $1 + $2 + $3 + $6 + $7}'; }

_measure() {
    # $1 = label, $2 = file, rest = extra mpv flags
    local label="$1" file="$2"; shift 2
    # Sanitise: the label uses "/" as a separator, which would otherwise make
    # the log path a directory that does not exist and silently swallow output.
    local log="$LOGDIR/$(echo "$label" | tr '/' '_').log"
    local before busy_before after busy_after hz delta pct method

    sync

    before=$(awk '/^cpu / {print $2, $3, $4, $5, $6, $7, $8; exit}' /proc/stat)
    busy_before=$(busy_ticks "$before")

    timeout $((DURATION + 30)) mpv \
        --vo=null --no-audio --no-osc --no-config \
        --length="$DURATION" "$@" "$file" \
        >"$log" 2>&1
    local rc=$?

    after=$(awk '/^cpu / {print $2, $3, $4, $5, $6, $7, $8; exit}' /proc/stat)
    busy_after=$(busy_ticks "$after")
    hz=$(getconf CLK_TCK)
    delta=$((busy_after - busy_before))
    pct=$(awk -v d="$delta" -v h="$hz" -v s="$DURATION" \
        'BEGIN {printf "%.1f", (d / h) / s * 100}')

    method=$(grep -oiE 'Using hardware decoding \([a-z0-9_-]+\)' "$log" | head -1)
    [ -z "$method" ] && method="(software)"

    printf '  %-16s %8s%%   %s\n' "$label" "$pct" "$method"
    # Surface WHY a hwdec attempt failed, rather than just "software".
    grep -oiE '(Could not find a valid device|Failed to initialize a hardware decoder|no decoder found|Not a valid DRM device)' \
        "$log" | head -1 | sed 's/^/                     ^ /'
    if [ "$rc" -ne 0 ]; then
        printf '  %-16s %8s    (mpv exit %s)\n' "" "" "$rc"
    fi
}

echo "=== HEVC decode: $(basename "$HEVC") ==="
echo
printf '  %-16s %8s   %s\n' "hwdec" "CPU" "mpv verdict"
printf '  %-16s %8s   %s\n' "----------------" "--------" "-----------"
_measure "hevc/no"        "$HEVC" --hwdec=no
_measure "hevc/auto"      "$HEVC" --hwdec=auto
_measure "hevc/v4l2m2m"   "$HEVC" --hwdec=v4l2m2m
_measure "hevc/hevc_v4l2" "$HEVC" --hwdec=hevc_v4l2m2m
_measure "hevc/drm"       "$HEVC" --hwdec=drm
_measure "hevc/drm-copy"  "$HEVC" --hwdec=drm-copy
_measure "hevc/auto-safe" "$HEVC" --hwdec=auto-safe

echo
echo "=== H.264 control: $(basename "$H264") ==="
echo
printf '  %-16s %8s   %s\n' "hwdec" "CPU" "mpv verdict"
printf '  %-16s %8s   %s\n' "----------------" "--------" "-----------"
_measure "h264/no"        "$H264" --hwdec=no
_measure "h264/auto"      "$H264" --hwdec=auto
_measure "h264/v4l2m2m"   "$H264" --hwdec=v4l2m2m
_measure "h264/drm"       "$H264" --hwdec=drm
_measure "h264/drm-copy"  "$H264" --hwdec=drm-copy

echo
echo "logs: $LOGDIR"

