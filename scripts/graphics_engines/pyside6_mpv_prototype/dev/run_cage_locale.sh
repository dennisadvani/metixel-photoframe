#!/bin/bash
# Stop Xorg if running, then run slideshow under cage
echo "=== stopping Xorg ==="
sudo pkill -f 'Xorg :0' 2>/dev/null
sleep 2
echo "=== Xorg running? ==="
ps aux | grep -i '[X]org' | wc -l
echo "=== running slideshow under cage ==="
cd /tmp/metixel-pyside-mpv
export QT_QPA_PLATFORM=wayland
timeout 25 cage -- python3 slideshow.py --media /opt/metixel/data/media/sample_media --duration 20 --out /tmp/metixel-pyside-mpv/benchmark.json 2>&1 | grep -iE 'mpv|render|failed|initialize|create|bench|playlist|segfault|error|video|VO' | head -30