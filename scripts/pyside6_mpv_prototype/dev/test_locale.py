#!/usr/bin/env python3
"""Check locale state after QCoreApplication and test mpv_create."""
import ctypes
import ctypes.util
import locale
import sys

name = ctypes.util.find_library("mpv") or "libmpv.so.2"
lib = ctypes.CDLL(name)
lib.mpv_create.restype = ctypes.c_void_p
lib.mpv_initialize.argtypes = [ctypes.c_void_p]
lib.mpv_initialize.restype = ctypes.c_int
lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
lib.mpv_terminate_destroy.restype = None

print("locale before app:", locale.setlocale(locale.LC_ALL), flush=True)

from PySide6.QtCore import QCoreApplication
app = QCoreApplication(sys.argv)
print("locale after app:", locale.setlocale(locale.LC_ALL), flush=True)
print("LC_NUMERIC after app:", locale.setlocale(locale.LC_NUMERIC), flush=True)

# Try setting LC_NUMERIC to C AFTER app creation
try:
    locale.setlocale(locale.LC_NUMERIC, 'C')
    print("set LC_NUMERIC=C after app OK", flush=True)
except Exception as e:
    print("setlocale failed:", e, flush=True)

mpv = lib.mpv_create()
print("mpv_create after app + locale fix:", mpv, flush=True)
if mpv:
    lib.mpv_terminate_destroy(mpv)
    print("destroyed OK", flush=True)
else:
    print("mpv_create NULL", flush=True)