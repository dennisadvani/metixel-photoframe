#!/bin/bash
# Run the mpv_create-in-Qt test under cage, full output
cd /tmp/metixel-pyside-mpv
timeout 15 cage -- python3 test_mpv_qt2.py 2>&1 | head -30