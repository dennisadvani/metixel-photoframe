#!/bin/bash
# 1. Is /run a real tmpfs (RAM) or a flash-backed mount?
echo "=== mount for /run and /run/metixel ==="
findmnt -no TARGET,SOURCE,FSTYPE /run
echo "  /run/metixel is inside the above unless listed separately"

echo
echo "=== confirm tmpfs by device id vs the rootfs ==="
python3 - <<'PY'
import os
for p in ("/", "/run", "/run/metixel", "/opt/metixel/data"):
    try:
        st = os.statvfs(p)
        s = os.stat(p)
        print(f"  {p:22s} dev={s.st_dev:<12} fstype-lookup-below")
    except OSError as e:
        print(f"  {p:22s} ERROR {e}")
PY
findmnt -no SOURCE,FSTYPE /                 | sed 's/^/  rootfs: /'
findmnt -no SOURCE,FSTYPE /run              | sed 's/^/  /run:   /'
findmnt -no SOURCE,FSTYPE /opt/metixel/data | sed 's/^/  data:   /'

echo
echo "=== who writes config.updated? ==="
grep -rn "config.updated" /opt/metixel/live/src --include=*.py | head

echo
echo "=== is it written on RELOAD (frontend) or only on SAVE (backend)? ==="
grep -rn -B3 -A6 "config.updated" /opt/metixel/live/src --include=*.py | head -40
