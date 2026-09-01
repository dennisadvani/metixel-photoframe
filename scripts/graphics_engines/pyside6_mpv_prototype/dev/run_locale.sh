#!/bin/bash
# Run locale test
cd /tmp/metixel-pyside-mpv
timeout 15 python3 test_locale.py 2>&1 | tail -15