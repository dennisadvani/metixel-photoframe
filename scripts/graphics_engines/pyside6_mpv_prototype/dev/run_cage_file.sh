#!/bin/bash
# Run slideshow under cage, full output to file
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland
timeout 25 cage -- python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 20 --out /tmp/metixel-pyside-mpv/benchmark.json > /tmp/metixel-pyside-mpv/full.log 2>&1
echo "=== exit code: $? ==="
echo "=== full.log ==="
cat /tmp/metixel-pyside-mpv/full.log
echo "=== benchmark.json ==="
cat /tmp/metixel-pyside-mpv/benchmark.json 2>/dev/null