#!/bin/bash
# Run cage in the foreground to capture the crash directly.
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland
timeout 20 cage -- python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 15 --out /tmp/metixel-pyside-mpv/benchmark.json 2>&1 | tail -40