#!/bin/bash
# Run slideshow under cage, full output to file
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland
timeout 15 cage -- python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 12 --out /tmp/metixel-pyside-mpv/benchmark.json > /tmp/metixel-pyside-mpv/debug.log 2>&1
echo "=== exit: $? ==="
echo "=== mpv/bench lines ==="
grep -iE 'mpv|render|video|VO|hwdec|decode|error|bench|playlist|frame|no video|finished' /tmp/metixel-pyside-mpv/debug.log | head -50