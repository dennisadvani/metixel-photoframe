#!/bin/bash
# Run slideshow under cage, full output
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland
timeout 25 cage -- python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 20 --out /tmp/metixel-pyside-mpv/benchmark.json 2>&1 | grep -vE 'xdg_surface|eglQueryDeviceStringEXT' | tail -40