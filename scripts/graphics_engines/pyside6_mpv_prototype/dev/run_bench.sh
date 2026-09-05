#!/bin/bash
# Run a proper 30s benchmark under cage
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland
timeout 40 cage -- python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 30 --out /tmp/metixel-pyside-mpv/benchmark.json > /tmp/metixel-pyside-mpv/bench.log 2>&1
echo "=== exit: $? ==="
echo "=== bench lines ==="
grep -iE '\[bench\]|playlist|render context|first video frame|VO:' /tmp/metixel-pyside-mpv/bench.log
echo "=== benchmark.json ==="
cat /tmp/metixel-pyside-mpv/benchmark.json 2>/dev/null