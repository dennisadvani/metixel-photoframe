#!/bin/bash
# Run the slideshow under cage, capturing app output separately.
cd /tmp/metixel-pyside-mpv
chmod +x app_wrapper.sh
setsid nohup cage -- bash /tmp/metixel-pyside-mpv/app_wrapper.sh > /tmp/metixel-pyside-mpv/cage.log 2>&1 < /dev/null &
echo "launched cage pid $!"
sleep 35
echo "=== app.log ==="
cat /tmp/metixel-pyside-mpv/app.log 2>/dev/null
echo "=== running? ==="
ps aux | grep -E 'cage|slideshow' | grep -v grep | wc -l
echo "=== benchmark.json ==="
cat /tmp/metixel-pyside-mpv/benchmark.json 2>/dev/null