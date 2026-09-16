#!/bin/bash
# Measure ACTUAL writes over 60 s of idle, split by flash vs tmpfs.
PID=$(pgrep -f "mode frontend" | head -1)
echo "frontend pid = $PID"

# Block-device write sectors before/after for the rootfs (mmcblk0p2).
read_sectors() {
    awk '$3 == "mmcblk0p2" {print $7}' /proc/diskstats
}
BEFORE=$(read_sectors)
echo "  mmcblk0p2 write sectors before: $BEFORE"

echo
echo "=== tracing writes for 60 s (idle, no config change) ==="
timeout 60 sudo strace -f -p $PID -e trace=openat,write,rename,utimes -o /tmp/idle.strace 2>/dev/null

AFTER=$(read_sectors)
echo "  mmcblk0p2 write sectors after:  $AFTER"
echo "  sectors written during 60 s:    $((AFTER - BEFORE))  (512 B each => $(( (AFTER - BEFORE) / 2 )) KiB)"

echo
echo "=== paths opened for writing during the window ==="
grep -E 'O_WRONLY|O_CREAT|O_RDWR' /tmp/idle.strace 2>/dev/null \
    | grep -oE '"[^"]+"' | sort | uniq -c | sort -rn | head -20

echo
echo "=== were any of those on flash (not /run, /tmp, /dev)? ==="
grep -E 'O_WRONLY|O_CREAT|O_RDWR' /tmp/idle.strace 2>/dev/null \
    | grep -oE '"/[^"]+"' \
    | grep -vE '"/(run|tmp|dev|proc|sys)' \
    | sort -u | head -20
echo "  (empty above = zero flash writes while idle)"
