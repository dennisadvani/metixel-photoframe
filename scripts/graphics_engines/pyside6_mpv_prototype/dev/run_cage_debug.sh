#!/bin/bash
# Run slideshow under cage with mpv debug logging
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland
timeout 15 cage -- python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 12 --out /tmp/metixel-pyside-mpv/benchmark.json 2>&1 | grep -iE 'mpv|render|video|VO|hwdec|decode|error|bench|playlist|frame' | head -40