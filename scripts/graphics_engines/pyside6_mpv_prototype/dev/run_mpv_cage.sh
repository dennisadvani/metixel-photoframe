#!/bin/bash
# Run the mpv_create test under cage
cd /tmp/metixel-pyside-mpv
timeout 15 cage -- python3 test_mpv_cage.py 2>&1 | grep -iE 'lib:|mpv_create|initialize|destroyed|error' | head