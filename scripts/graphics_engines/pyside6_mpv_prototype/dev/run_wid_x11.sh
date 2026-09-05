#!/bin/bash
# Run the wid-embedding test under X11
cd /tmp/metixel-pyside-mpv
export DISPLAY=:0
export QT_QPA_PLATFORM=xcb
timeout 20 python3 test_wid_embed.py 2>&1 | tail -30