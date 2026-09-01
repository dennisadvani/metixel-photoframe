#!/bin/bash
# Test mpv_create order relative to QApplication under cage
cd /tmp/metixel-pyside-mpv
timeout 15 cage -- python3 test_mpv_order.py 2>&1 | grep -iE 'mpv_create|initialize|QApplication|destroyed|NULL|error' | head