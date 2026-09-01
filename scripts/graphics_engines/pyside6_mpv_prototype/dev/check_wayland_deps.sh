#!/bin/bash
echo "=== deps of libqwayland.so ==="
ldd /home/pi/.local/lib/python3.13/site-packages/PySide6/Qt/plugins/platforms/libqwayland.so 2>&1 | grep -iE 'not found|Wayland|wayland'
echo "=== deps of libQt6WaylandClient ==="
ldd /home/pi/.local/lib/python3.13/site-packages/PySide6/Qt/lib/libQt6WaylandClient.so.6 2>&1 | grep -iE 'not found|wayland'
echo "=== check libwayland-egl ==="
find / -name 'libwayland-egl*' 2>/dev/null
echo "=== check libQt6WaylandEglClientHwIntegration ==="
find /home/pi/.local/lib/python3.13/site-packages/PySide6 -name '*WaylandEgl*' 2>/dev/null