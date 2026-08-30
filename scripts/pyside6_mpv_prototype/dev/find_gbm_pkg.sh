#!/bin/bash
echo "=== which package provides libQt6EglFsKmsGbmSupport ==="
apt-file search libQt6EglFsKmsGbmSupport 2>/dev/null || echo "apt-file not installed"
echo "=== try apt-cache search ==="
apt-cache search qt6-qpa 2>/dev/null
apt-cache search qt6-egl 2>/dev/null
echo "=== check libqt6gui6 contents ==="
apt-cache show libqt6gui6 2>/dev/null | grep -iE 'Package|Version|Description' | head
echo "=== list qt6 qpa plugin packages ==="
apt-cache search 'qt6.*plugin' 2>/dev/null | head
echo "=== check if libqt6gui6 provides eglfs kms ==="
apt-cache depends libqt6gui6 2>/dev/null | head -30