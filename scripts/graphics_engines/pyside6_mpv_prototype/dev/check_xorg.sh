#!/bin/bash
# Check if Xorg is available and can be started
echo "=== Xorg binary ==="
which Xorg
echo "=== Xorg version ==="
Xorg -version 2>&1 | head -2
echo "=== unclutter ==="
which unclutter
echo "=== is Xorg running? ==="
ps aux | grep -i xorg | grep -v grep | wc -l
echo "=== Xwrapper config ==="
cat /etc/X11/Xwrapper.config 2>/dev/null