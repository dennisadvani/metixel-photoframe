#!/bin/bash
# Run Qt import test
cd /tmp/metixel-pyside-mpv
timeout 15 python3 test_qt_import.py 2>&1 | tail -15