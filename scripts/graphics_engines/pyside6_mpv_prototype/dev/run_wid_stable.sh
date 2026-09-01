#!/bin/bash
# Run wid-embedding test with stable native window + vo=gpu
cd /tmp/metixel-pyside-mpv
export DISPLAY=:0
export QT_QPA_PLATFORM=xcb
timeout 20 python3 test_wid_stable.py 2>&1 | tail -30