#!/bin/bash
echo "=== find libQt6EglFsKmsGbmSupport ==="
find / -name 'libQt6EglFsKmsGbmSupport*' 2>/dev/null
echo "=== PySide6 Qt lib dir (EglFs/Kms/Gbm) ==="
ls /home/pi/.local/lib/python3.13/site-packages/PySide6/Qt/lib/ 2>/dev/null | grep -iE 'EglFs|Kms|Gbm'
echo "=== LD_LIBRARY_PATH ==="
echo "$LD_LIBRARY_PATH"
echo "=== PySide6 Qt lib dir listing ==="
ls /home/pi/.local/lib/python3.13/site-packages/PySide6/Qt/lib/ 2>/dev/null | head -40