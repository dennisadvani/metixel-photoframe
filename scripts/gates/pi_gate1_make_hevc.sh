#!/bin/bash
# GATE-1 prerequisite: build a real HEVC test clip.
#
# Both shipped sample videos are H.264, so any "HEVC" hwdec comparison run
# against them was actually decoding H.264 and proves nothing. This transcodes
# an H.264 source to H.265 with the same parameters PROFILES["pi5"] uses
# (libx265, CRF 23), so the test clip matches what the optimiser would emit.

set -eu

SRC=/opt/metixel/data/media/sample_media/landscape/13131508_1920_1080_24fps.mp4
OUT=/tmp/gate1/test_hevc_1080p.mp4
mkdir -p /tmp/gate1

if [ ! -f "$SRC" ]; then
    echo "ERROR: source not found: $SRC"; exit 1
fi

echo "=== source ==="
ffprobe -v error -show_entries stream=codec_name,width,height -of default=noprint_wrappers=1 "$SRC"

echo
echo "=== transcoding to HEVC (libx265, crf 23, no audio) ==="
echo "    this is what PROFILES['pi5'] produces"
ffmpeg -y -v warning -stats \
    -i "$SRC" \
    -c:v libx265 -crf 23 -preset veryfast \
    -pix_fmt yuv420p \
    -tag:v hvc1 \
    -an \
    "$OUT"

echo
echo "=== result ==="
ffprobe -v error \
    -show_entries stream=codec_name,profile,width,height,pix_fmt \
    -show_entries format=duration,size,bit_rate \
    -of default=noprint_wrappers=1 "$OUT" | sed 's/^/  /'

echo
echo "clip: $OUT"
