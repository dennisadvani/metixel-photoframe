#!/bin/bash
# GATE-1 (HEVC on Pi 5): device check. Re-runnable, read-only.

echo "=== video devices ==="
ls -la /dev/video* 2>/dev/null | head -20

echo
echo "=== device identities ==="
for d in 10 11 12 18 19 20 21 22 23 24 25 26 27 28 29 30 31; do
    n=$(cat /sys/class/video4linux/video${d}/name 2>/dev/null)
    [ -n "$n" ] && echo "  video${d}: $n"
done

echo
echo "=== open file handles on the HEVC decoder ==="
sudo fuser -v /dev/video19 2>&1 | head -10 || echo "  (fuser unavailable)"

echo
echo "=== kernel HEVC registration ==="
sudo dmesg 2>/dev/null | grep -iE "hevc|rpi-hevc|v4l2" | tail -10

echo
echo "=== load average ==="
cat /proc/loadavg

echo
echo "=== metixel services ==="
systemctl is-active metixel-cage metixel-backend metixel-cursor-hider 2>/dev/null
