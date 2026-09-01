#!/bin/bash
# Wrapper run by cage: launches the slideshow and captures its output.
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland
exec python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 30 --out /tmp/metixel-pyside-mpv/benchmark.json > /tmp/metixel-pyside-mpv/app.log 2>&1