#!/bin/bash
# Run slideshow and capture full output to find the segfault location
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland
timeout 40 cage -- python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 30 --out /tmp/metixel-pyside-mpv/benchmark.json > /tmp/metixel-pyside-mpv/crash.log 2>&1
echo "=== exit: $? ==="
echo "=== tail ==="
tail -30 /tmp/metixel-pyside-mpv/crash.log