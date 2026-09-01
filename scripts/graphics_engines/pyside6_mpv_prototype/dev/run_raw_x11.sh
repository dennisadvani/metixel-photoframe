#!/bin/bash
# Run raw mpv_create test under X11
cd /tmp/metixel-pyside-mpv
export DISPLAY=:0
export QT_QPA_PLATFORM=xcb
timeout 15 python3 test_raw_x11.py 2>&1 | tail -15