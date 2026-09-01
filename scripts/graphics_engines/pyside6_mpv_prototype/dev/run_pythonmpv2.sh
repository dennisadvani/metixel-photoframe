#!/bin/bash
# Test python-mpv inside QApplication under cage, full output
cd /tmp/metixel-pyside-mpv
timeout 15 cage -- python3 test_pythonmpv.py 2>&1 | tail -20