#!/bin/bash
# Test python-mpv inside QApplication under cage with faulthandler
cd /tmp/metixel-pyside-mpv
timeout 15 cage -- python3 test_pythonmpv3.py 2>&1 | tail -25