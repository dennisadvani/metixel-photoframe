#!/bin/bash
echo "=== PySide6 version ==="
python3 -c "import PySide6; print(PySide6.__version__)"
echo "=== all EglFs/Kms/Gbm libs in PySide6 ==="
find /home/pi/.local/lib/python3.13/site-packages/PySide6 -name '*EglFs*' -o -name '*Kms*' -o -name '*Gbm*' 2>/dev/null
echo "=== apt-cache policy qt6 ==="
apt-cache policy libqt6gui6 2>/dev/null
echo "=== apt list available qt6 egl ==="
apt-cache search qt6 2>/dev/null | grep -iE 'egl|kms|gbm|gui' | head
echo "=== check pip PySide6-Essentials vs Addons ==="
pip list 2>/dev/null | grep -i pyside