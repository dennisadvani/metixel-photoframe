#!/bin/bash
# Run the mpv_create-in-Qt test under cage
cd /tmp/metixel-pyside-mpv
timeout 15 cage -- python3 test_mpv_qt.py 2>&1 | grep -iE 'QApplication|mpv_create|initialize|destroyed|NULL|error' | head