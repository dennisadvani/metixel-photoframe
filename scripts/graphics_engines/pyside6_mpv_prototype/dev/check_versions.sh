#!/bin/bash
echo "=== mpv.py line 875 context ==="
sed -n '860,890p' /home/pi/.local/lib/python3.13/site-packages/mpv.py
echo "=== mpv version ==="
mpv --version 2>/dev/null | head -1
echo "=== libmpv version ==="
python3 -c "import ctypes,ctypes.util; lib=ctypes.CDLL(ctypes.util.find_library('mpv') or 'libmpv.so.2'); lib.mpv_client_api_version.restype=ctypes.c_ulong; v=lib.mpv_client_api_version(); print('client api version:', hex(v))"
echo "=== python-mpv version ==="
pip show python-mpv 2>/dev/null | grep -iE 'version|location'
echo "=== PySide6 version ==="
python3 -c "import PySide6; print(PySide6.__version__)"