#!/bin/bash
echo "=== PySide6 wayland platform plugin ==="
ls /home/pi/.local/lib/python3.13/site-packages/PySide6/Qt/plugins/platforms/ | grep -i wayland
echo "=== PySide6 wayland client libs ==="
find /home/pi/.local/lib/python3.13/site-packages/PySide6 -name '*Wayland*' 2>/dev/null
echo "=== system qt6-wayland ==="
apt-cache policy qt6-wayland 2>/dev/null | head -5
echo "=== cage installed? ==="
which cage
echo "=== cage version ==="
cage --version 2>&1 | head -2
echo "=== wayland libs ==="
find / -name 'libwayland-client.so*' 2>/dev/null | head