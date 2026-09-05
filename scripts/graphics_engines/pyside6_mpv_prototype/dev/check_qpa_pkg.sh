#!/bin/bash
echo "=== qt6-qpa-plugins details ==="
apt-cache show qt6-qpa-plugins 2>/dev/null | grep -iE 'Package|Version|Description|Depends' | head -20
echo "=== files in qt6-qpa-plugins (if installed) ==="
dpkg -L qt6-qpa-plugins 2>/dev/null | grep -iE 'eglfs|kms|gbm' || echo "not installed"
echo "=== search all packages for EglFsKmsGbm ==="
apt-cache search qt6 2>/dev/null | grep -iE 'qpa|egl|kms|gbm|platform'