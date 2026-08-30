#!/bin/bash
# Install missing xcb plugin deps
sudo apt-get install -y libxcb-xkb1 libxkbcommon-x11-0 2>&1 | tail -5
echo "=== verify ==="
ldd /home/pi/.local/lib/python3.13/site-packages/PySide6/Qt/plugins/platforms/libqxcb.so 2>&1 | grep -iE 'not found' || echo "ALL DEPS SATISFIED"