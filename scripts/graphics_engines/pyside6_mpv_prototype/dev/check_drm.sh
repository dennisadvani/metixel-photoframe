#!/bin/bash
# Identify which DRM card is the active display
echo "=== card0 uevent ==="
grep -i 'DRIVER\|OF_NAME' /sys/class/drm/card0/device/uevent 2>/dev/null
echo "=== card1 uevent ==="
grep -i 'DRIVER\|OF_NAME' /sys/class/drm/card1/device/uevent 2>/dev/null
echo "=== connector statuses ==="
for f in /sys/class/drm/card*-*/status; do
  echo "$f: $(cat "$f")"
done
echo "=== card0 connectors ==="
ls -d /sys/class/drm/card0-* 2>/dev/null
echo "=== card1 connectors ==="
ls -d /sys/class/drm/card1-* 2>/dev/null