#!/bin/bash
# Test python-mpv inside QApplication under cage
cd /tmp/metixel-pyside-mpv
timeout 15 cage -- python3 test_pythonmpv.py 2>&1 | grep -iE 'QApplication|MPV|terminated|failed|error|NULL' | head