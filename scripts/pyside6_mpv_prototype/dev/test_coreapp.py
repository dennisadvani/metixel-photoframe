#!/usr/bin/env python3
"""Test mpv_create with QCoreApplication vs QApplication under X11."""
import ctypes
import ctypes.util
import locale
import sys
locale.setlocale(locale.LC_NUMERIC, 'C')

name = ctypes.util.find_library("mpv") or "libmpv.so.2"
lib = ctypes.CDLL(name)
lib.mpv_create.restype = ctypes.c_void_p
lib.mpv_initialize.argtypes = [ctypes.c_void_p]
lib.mpv_initialize.restype = ctypes.c_int
lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
lib.mpv_terminate_destroy.restype = None

# Test 1: QCoreApplication (no GUI)
from PySide6.QtCore import QCoreApplication
app = QCoreApplication(sys.argv)
print("QCoreApplication created", flush=True)
mpv = lib.mpv_create()
print("mpv_create after QCoreApplication:", mpv, flush=True)
if mpv:
    lib.mpv_terminate_destroy(mpv)
    print("destroyed OK", flush=True)
else:
    print("mpv_create NULL after QCoreApplication", flush=True)