#!/bin/bash
echo "=== xcb plugin deps ==="
ldd /home/pi/.local/lib/python3.13/site-packages/PySide6/Qt/plugins/platforms/libqxcb.so 2>&1 | grep -iE 'not found'
echo "=== all not-found deps ==="
ldd /home/pi/.local/lib/python3.13/site-packages/PySide6/Qt/plugins/platforms/libqxcb.so 2>&1 | grep 'not found'