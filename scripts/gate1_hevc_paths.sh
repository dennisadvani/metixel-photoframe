#!/usr/bin/env bash
# GATE-1 (part 2): which HEVC hardware path actually works, and at what CPU cost?
#
# Part 1 established:
#   v4l2m2m + HEVC  -> FAILS ("Could not find a valid device"), software fallback
#   auto    + HEVC  -> "Using hardware decoding (drm-copy)"
#
# drm-copy is decode-on-GPU then COPY back to system memory. The zero-copy
# variant (`drm`) is preferable if it works. This script tests each candidate
# explicitly and measures CPU, because "it says hardware" is not the same as
# "it is cheaper than software" -- a copy path can cost more than it saves.
set -u

WORK=/tmp/gate1
HEVC="$WORK/test_hevc.mp4"
mkdir -p "$WORK"

if [ ! -f "$HEVC" ]; then
    ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i testsrc2=size=1920x1080:rate=24:duration=3 \
        -c:v libx265 -preset ultrafast -x265-params log-level=none \
        -pix_fmt yuv420p "$HEVC"
fi

# A software-decoded baseline is essential: without it, "15% CPU" is meaningless.
measure() {
    local label="$1"; shift
    local log="$WORK/m_${label}.log"

    # One run for the verdict, one short run for CPU.
    timeout 60 mpv "$HEVC" --vo=null --ao=null --no-config \
        --msg-level=all=v --frames=48 "$@" >"$log" 2>&1
    local verdict
    verdict=$(grep -oE 'Using hardware decoding \([a-z0-9_-]+\)|Using software decoding' "$log" | tail -1)
    [ -z "$verdict" ] && verdict="(no verdict line)"

    # CPU: 8 seconds of looping playback, sampled by the kernel via /proc.
    # Using the child's own utime+stime avoids top's sampling noise.
    timeout 20 mpv "$HEVC" --vo=null --ao=null --no-config --loop=yes "$@" \
        >/dev/null 2>&1 &
    local pid=$!
    sleep 2
    local start
    start=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null || echo 0)
    sleep 6
    local end
    end=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null || echo 0)
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null

    local ticks=$(( end - start ))
    # 100 ticks/sec, 6s wall, 4 cores -> percent of one core.
    local cpu
    cpu=$(awk -v t="$ticks" 'BEGIN{printf "%.1f", t/100/6*100}')

    printf '  %-26s %-38s cpu=%s%% of one core\n' "$label" "$verdict" "$cpu"
}

echo "=== HEVC decode paths (1080p, 24fps) ==="
printf '  %-26s %-38s %s\n' "option" "mpv verdict" "cost"
measure "software(baseline)" --hwdec=no
measure "v4l2m2m" --hwdec=v4l2m2m
measure "drm" --hwdec=drm
measure "drm-copy" --hwdec=drm-copy
measure "auto" --hwdec=auto

echo
echo "=== for comparison: H.264 via v4l2m2m (the assumed-good path) ==="
H264="$WORK/test_h264.mp4"
if [ ! -f "$H264" ]; then
    ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i testsrc2=size=1920x1080:rate=24:duration=3 \
        -c:v libx264 -preset ultrafast -pix_fmt yuv420p "$H264"
fi
HEVC_SAVE="$HEVC"; HEVC="$H264"
measure "h264 v4l2m2m" --hwdec=v4l2m2m
measure "h264 software" --hwdec=no
HEVC="$HEVC_SAVE"

echo
echo "=== does drm work under the REAL gl render path (not vo=null)? ==="
# This is the configuration the app actually uses, so it is the one that counts.
timeout 60 mpv "$HEVC" --vo=gpu --gpu-api=opengl --no-config \
    --msg-level=all=v --frames=24 --hwdec=drm-copy \
    >"$WORK/gl_drm.log" 2>&1
grep -oE 'Using hardware decoding \([a-z0-9_-]+\)|Using software decoding' \
    "$WORK/gl_drm.log" | tail -1
grep -iE 'drmprime|dmabuf|interop|EGL' "$WORK/gl_drm.log" | head -8
