#!/bin/bash
echo "=== apt search for Qt6 EglFs Kms Gbm ==="
apt-cache search Qt6EglFs 2>/dev/null
apt-cache search eglfs 2>/dev/null
echo "=== system Qt6 libs ==="
find /usr/lib -name 'libQt6EglFs*' 2>/dev/null
echo "=== dpkg -l qt6 ==="
dpkg -l 2>/dev/null | grep -i 'qt6.*egl\|qt6.*kms\|qt6.*gbm' | head
echo "=== is libqeglfs-kms-integration in system? ==="
find /usr -name 'libqeglfs-kms-integration*' 2>/dev/null
echo "=== system Qt6 version ==="
dpkg -l 2>/dev/null | grep -i 'libqt6gui6' | head