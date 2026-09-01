#!/bin/bash
# Start Xorg and run the wid-embedding test under X11
cd /tmp/metixel-pyside-mpv

# Start Xorg on :0 as root (needed for DRM access)
sudo Xorg :0 -config /tmp/metixel-pyside-mpv/xorg.conf > /tmp/metixel-pyside-mpv/xorg.log 2>&1 &
sleep 3
echo "=== Xorg running? ==="
ps aux | grep -i '[X]org' | wc -l
echo "=== X socket ==="
ls -la /tmp/.X11-unix/ 2>/dev/null
echo "=== xorg.log ==="
tail -10 /tmp/metixel-pyside-mpv/xorg.log 2>/dev/null