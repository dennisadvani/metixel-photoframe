#!/bin/bash
# Check the running Xorg and DISPLAY
echo "=== Xorg process ==="
ps aux | grep -i xorg | grep -v grep
echo "=== DISPLAY env ==="
echo "DISPLAY=$DISPLAY"
echo "=== X sockets ==="
ls -la /tmp/.X11-unix/ 2>/dev/null
echo "=== Xorg log ==="
ls -la /var/log/Xorg* 2>/dev/null | head