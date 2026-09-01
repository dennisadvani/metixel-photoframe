#!/usr/bin/env bash
# Check the environment of the running cage process.
set -u
# Find the cage process
for pid in $(pgrep -f 'cage -d -- /tmp/metixel-wayland-cursor/client'); do
    echo "=== PID $pid ==="
    tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -iE 'XCURSOR|WLR' || echo "  (no XCURSOR/WLR env)"
done
