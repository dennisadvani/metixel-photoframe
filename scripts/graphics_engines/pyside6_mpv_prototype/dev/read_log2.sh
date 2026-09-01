#!/bin/bash
echo "=== app.log ==="
cat /tmp/metixel-pyside-mpv/app.log 2>/dev/null
echo "=== cage.log ==="
cat /tmp/metixel-pyside-mpv/cage.log 2>/dev/null
echo "=== running? ==="
ps aux | grep -E 'cage|slideshow' | grep -v grep | wc -l