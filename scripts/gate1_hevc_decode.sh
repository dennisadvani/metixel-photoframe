#!/usr/bin/env bash
# GATE-1: does mpv DECODE HEVC via hardware on this Pi, or fall back to software?
#
# The distinction that matters is decode, not encode: `hevc_v4l2m2m` being present
# as an *encoder* says nothing about whether the decoder path is usable. If HEVC
# silently software-decodes, the `pi5`/`pi4` transcode profiles must move to H.264
# or the device will peg its CPU on every video.
set -u

echo "=== v4l2m2m DECODER devices exposed to mpv ==="
# mpv's v4l2m2m hwdec enumerates /dev/video* for the mem2mem decoder node.
for d in /dev/video*; do
    name=$(cat "/sys/class/video4linux/$(basename "$d")/name" 2>/dev/null)
    case "$name" in
        *dec*|*Dec*|*m2m*|*bm2835*|*rpivid*|*H265*|*HEVC*|*hevc*)
            echo "  $d -> $name"
            ;;
    esac
done

echo
echo "=== all v4l2 device names (for reference) ==="
for d in /dev/video*; do
    printf '  %s -> %s\n' "$d" "$(cat "/sys/class/video4linux/$(basename "$d")/name" 2>/dev/null)"
done

echo
echo "=== hwdec values containing v4l2 or drm ==="
mpv --hwdec=help 2>&1 | grep -iE 'v4l2|drm|rpi' || echo "  (none listed)"

echo
echo "=== kernel codec support in the report (rpi-hevc-dec etc.) ==="
dmesg 2>/dev/null | grep -iE 'hevc|h265|v4l2.*m2m|rpivid' | head -10 || echo "  (dmesg unavailable)"

echo
echo "=== v4l2 codec capability of each decoder node ==="
for d in /dev/video*; do
    name=$(cat "/sys/class/video4linux/$(basename "$d")/name" 2>/dev/null)
    case "$name" in
        *dec*|*Dec*|*m2m*|*M2M*)
            echo "--- $d ($name) ---"
            v4l2-ctl -d "$d" --list-formats-out 2>/dev/null | head -8
            v4l2-ctl -d "$d" --list-formats-ext 2>/dev/null | head -6
            ;;
    esac
done
