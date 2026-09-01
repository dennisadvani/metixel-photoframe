#!/bin/bash
# Run the mpv_create-in-Qt test with LC_NUMERIC=C under cage
cd /tmp/metixel-pyside-mpv
timeout 15 cage -- python3 test_mpv_qt3.py 2>&1 | grep -iE 'LC_NUMERIC|QApplication|mpv_create|initialize|destroyed|NULL|error' | head