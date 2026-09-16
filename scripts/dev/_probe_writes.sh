#!/bin/bash
# Does the config-change check write anything? Measure it directly: read the
# process's write syscalls and the block-device write counter while idle.
PID=$(pgrep -f "mode frontend" | head -1)
echo "frontend pid = $PID"

echo
echo "=== what files does the frontend hold open for WRITING? ==="
sudo ls -l /proc/$PID/fd 2>/dev/null | grep -E 'w|a' | head -20

echo
echo "=== any writable handle on the SD card (mmcblk / rootfs)? ==="
sudo ls -l /proc/$PID/fd 2>/dev/null | grep -E 'mmcblk|/opt/metixel|/data' | head -20
echo "   (empty = nothing on the data tree held open)"

echo
echo "=== does strace show a write during a reload? (10s window) ==="
if command -v strace >/dev/null 2>&1; then
    timeout 10 sudo strace -f -p $PID -e trace=write,openat -o /tmp/probe.strace 2>/dev/null
    echo "   openat with O_WRONLY/O_CREAT:"
    grep -E 'O_WRONLY|O_RDWR|O_CREAT' /tmp/probe.strace 2>/dev/null | grep -vE 'ENOENT' | head -20
    echo "   (empty = no file opened for writing during the window)"
else
    echo "   strace not installed"
fi

echo
echo "=== config mtime now (should NOT change while idle) ==="
sudo stat -c '  %y' /opt/metixel/data/config.json

echo
echo "=== run_dir writes only (tmpfs, costs RAM not flash) ==="
ls -la /run/metixel/ 2>/dev/null | head
echo "  is /run/metixel on tmpfs?"
findmnt -no FSTYPE /run/metixel 2>/dev/null || echo "  (not a separate mount)"
