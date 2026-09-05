#!/bin/bash
echo "=== full cage.log ==="
cat /tmp/metixel-pyside-mpv/cage.log
echo "=== ls benchmark ==="
ls -la /tmp/metixel-pyside-mpv/benchmark.json 2>/dev/null
echo "=== grep slideshow output ==="
grep -iE 'bench|playlist|slideshow|error|traceback' /tmp/metixel-pyside-mpv/cage.log