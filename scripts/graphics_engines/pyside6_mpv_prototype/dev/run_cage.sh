#!/bin/bash
# Run the PySide6+mpv slideshow under cage (Wayland compositor)
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland
# Run cage with the app. cage needs a tty/DRM; use setsid nohup to detach.
setsid nohup cage -- python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 30 --out /tmp/metixel-pyside-mpv/benchmark.json > /tmp/metixel-pyside-mpv/cage.log 2>&1 < /dev/null &
echo "launched cage pid $!"
sleep 15
echo "=== cage.log ==="
cat /tmp/metixel-pyside-mpv/cage.log
echo "=== running? ==="
ps aux | grep -E 'cage|slideshow' | grep -v grep | wc -l