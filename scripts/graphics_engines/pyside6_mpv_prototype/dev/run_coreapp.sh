#!/bin/bash
# Run QCoreApplication test under X11
cd /tmp/metixel-pyside-mpv
export DISPLAY=:0
export QT_QPA_PLATFORM=xcb
timeout 15 python3 test_coreapp.py 2>&1 | tail -15