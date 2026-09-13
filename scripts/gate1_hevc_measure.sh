#!/usr/bin/env bash
# GATE-1 (part 3): settle two questions with a trustworthy measurement.
#
# Part 2 raised a contradiction worth resolving rather than trusting:
#   - H.264 via v4l2m2m fell back to SOFTWARE, yet repo notes record H.264/v4l2m2m
#     as the verified path on Pi 5. Either the notes are about a different config,
#     or `vo=null` breaks v4l2m2m.
#   - Only `drm-copy` gave hardware HEVC; zero-copy `drm` fell back.
#
# The CPU numbers from part 2 were 0.0% across the board, i.e. the measurement was
# broken. This version reads utime+stime correctly and separates the variables:
# vo=null (decoder in isolation) vs the real GL path (what the app uses).
set -u

WORK=/tmp/gate1
mkdir -p "$WORK"

[ -f "$WORK/test_hevc.mp4" ] || ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i testsrc2=size=1920x1080:rate=24:duration=3 \
    -c:v libx265 -preset ultrafast -x265-params log-level=none -pix_fmt yuv420p \
    "$WORK/test_hevc.mp4"
[ -f "$WORK/test_h264.mp4" ] || ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i testsrc2=size=1920x1080:rate=24:duration=3 \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p "$WORK/test_h264.mp4"

CORE_COUNT=$(nproc)

# Measure by diffing the process's own CPU ticks. The earlier version read the
# wrong /proc field; this one sums utime (14) + stime (15), which is what
# `top`/`ps` report, and divides by real elapsed wall time.
measure_cpu() {
    local label="$1"; shift
    local log="$WORK/cpu_${label//\//_}.log"
    local seconds=6

    timeout $(( seconds + 8 )) mpv "$1" --ao=null --no-config --loop=yes \
        --msg-level=all=v --frames=100000 "${@:2}" >"$log" 2>&1 &
    local pid=$!
    sleep 2
    local t0
    t0=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null || echo "")

    # Wall clock from the kernel, not from sleep, so a slow start cannot skew it.
    local w0 w1
    w0=$(date +%s%N)
    sleep "$seconds"
    w1=$(date +%s%N)

    local t1
    t1=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null || echo "")
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null

    local verdict
    verdict=$(grep -oE 'Using hardware decoding \([a-z0-9_-]+\)|Using software decoding' \
        "$log" | tail -1)
    [ -z "$verdict" ] && verdict="(no verdict)"

    if [ -z "$t0" ] || [ -z "$t1" ]; then
        printf '  %-34s %-34s cpu=?? (process vanished)\n' "$label" "$verdict"
        return
    fi

    local pct
    pct=$(awk -v a="$t0" -v b="$t1" -v n="$w0" -v m="$w1" \
        'BEGIN{ d=b-a; w=(m-n)/1e9; if (w>0) printf "%.1f", (d/100.0)/w*100; else print "?" }')

    printf '  %-34s %-34s cpu=%s%% of one core (of %s)\n' \
        "$label" "$verdict" "$pct" "$CORE_COUNT"
}

echo "############ vo=null (decoder in isolation) ############"
printf '  %-34s %-34s %s\n' "case" "verdict" "cost"
measure_cpu "hevc hwdec=no   vo=null" "$WORK/test_hevc.mp4" --vo=null --hwdec=no
measure_cpu "hevc hwdec=auto vo=null" "$WORK/test_hevc.mp4" --vo=null --hwdec=auto
measure_cpu "hevc hwdec=drm-copy vo=null" "$WORK/test_hevc.mp4" --vo=null --hwdec=drm-copy
measure_cpu "h264 hwdec=no   vo=null" "$WORK/test_h264.mp4" --vo=null --hwdec=no
measure_cpu "h264 hwdec=auto vo=null" "$WORK/test_h264.mp4" --vo=null --hwdec=auto
measure_cpu "h264 hwdec=v4l2m2m vo=null" "$WORK/test_h264.mp4" --vo=null --hwdec=v4l2m2m

echo
echo "############ real GL path, headless via EGL surfaceless ############"
# The configuration closest to production. Not the app's cage path -- that is
# GATE-2 -- but it exercises the GL interop that vo=null skips entirely.
if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    echo "  (a display is present; using it)"
    measure_cpu "hevc hwdec=auto gl" "$WORK/test_hevc.mp4" --vo=gpu --gpu-api=opengl --hwdec=auto
    measure_cpu "h264 hwdec=auto gl" "$WORK/test_h264.mp4" --vo=gpu --gpu-api=opengl --hwdec=auto
else
    echo "  (no DISPLAY/WAYLAND_DISPLAY -- the real GL path must be tested in GATE-2"
    echo "   under cage, which is where the production stack actually runs)"
fi

echo
echo "############ why v4l2m2m fails: libavcodec log ############"
mpv "$WORK/test_h264.mp4" --vo=null --ao=null --no-config --frames=24 \
    --hwdec=v4l2m2m --msg-level=all=v --msg-level=ffmpeg/video=v \
    >"$WORK/why.log" 2>&1
grep -iE 'v4l2m2m|device|/dev/video|Failed|error|request' "$WORK/why.log" | head -20

echo
echo "############ are the decoder nodes permissioned for the pi user? ############"
id
ls -l /dev/video19 2>/dev/null
ls -l /dev/dri/renderD128 2>/dev/null
groups
