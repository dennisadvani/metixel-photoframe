#!/usr/bin/env bash
# GATE-1 (decisive): does mpv hardware-decode HEVC on this Pi 5?
#
# The presence of `rpi-hevc-dec` in /dev and `hevc_v4l2m2m` in mpv's --hwdec list
# is necessary but NOT sufficient: if libavcodec cannot actually open the v4l2m2m
# decoder, mpv silently falls back to software and only says so in the log. That
# silent fallback is the failure this test exists to catch.
set -u

WORK=/tmp/gate1
mkdir -p "$WORK"

echo "=== source codecs available ==="
for f in /opt/metixel/data/media/sample_media/*/*.mp4; do
    [ -f "$f" ] || continue
    codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
        -of default=nw=1:nk=1 "$f" 2>/dev/null)
    printf '  %-60s %s\n' "$(basename "$f")" "$codec"
done

# Build an HEVC test clip if we do not have one, so the test is self-contained.
HEVC="$WORK/test_hevc.mp4"
if [ ! -f "$HEVC" ]; then
    echo
    echo "=== generating a 1080p HEVC test clip (libx265, 3s) ==="
    ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i testsrc2=size=1920x1080:rate=24:duration=3 \
        -c:v libx265 -preset ultrafast -x265-params log-level=none \
        -pix_fmt yuv420p "$HEVC" 2>&1 | tail -3
    ls -la "$HEVC"
fi

echo
echo "=== codec of the test clip ==="
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height \
    -of default=nw=1 "$HEVC"

run_mpv() {
    local label="$1"
    local hwdec="$2"
    shift 2
    local log="$WORK/${label}.log"
    echo
    echo "############ $label (--hwdec=$hwdec) ############"
    # vo=null + no audio: isolate the DECODER. Rendering to the console would
    # confound the result with the GL path, which is a separate question.
    timeout 40 mpv "$HEVC" \
        --vo=null --ao=null --frames=48 --no-config \
        --hwdec="$hwdec" \
        --msg-level=all=v \
        "$@" >"$log" 2>&1
    echo "--- decoder lines ---"
    grep -iE 'Using hardware decoding|Using software decoding|hwdec|v4l2m2m|Failed to|error|drm' \
        "$log" | head -25
    echo "--- chosen decoder (vd) ---"
    grep -iE '^\[vd\]|avcodec|VT|decoder' "$log" | head -12
}

run_mpv "hwdec_v4l2m2m" "v4l2m2m"
run_mpv "hwdec_auto" "auto"

echo
echo "=== decoded frame count proof (v4l2m2m) ==="
timeout 40 mpv "$HEVC" --vo=null --ao=null --no-config --hwdec=v4l2m2m \
    --msg-level=all=info --frames=48 2>&1 | grep -iE 'video|frame|fps' | head -10

echo
echo "=== CPU during a 3s hwdec run (should be low if truly hardware) ==="
( timeout 20 mpv "$HEVC" --vo=null --ao=null --no-config --hwdec=v4l2m2m --loop=yes \
    >/dev/null 2>&1 ) &
MPV_PID=$!
sleep 4
top -b -n 1 -p "$MPV_PID" 2>/dev/null | tail -3
sleep 1
wait "$MPV_PID" 2>/dev/null
echo "(done)"
