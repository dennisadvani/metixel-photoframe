#!/usr/bin/env python3
"""Test mpv_create inside QApplication with LC_NUMERIC=C."""

import ctypes
import ctypes.util
import locale
import sys

from PySide6.QtWidgets import QApplication

# Set LC_NUMERIC to C before Qt/mpv init
try:
    locale.setlocale(locale.LC_NUMERIC, "C")
    print("LC_NUMERIC set to C")
except Exception as e:
    print("setlocale failed:", e)

name = ctypes.util.find_library("mpv") or "libmpv.so.2"
lib = ctypes.CDLL(name)
lib.mpv_create.restype = ctypes.c_void_p
lib.mpv_initialize.argtypes = [ctypes.c_void_p]
lib.mpv_initialize.restype = ctypes.c_int
lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
lib.mpv_terminate_destroy.restype = None

app = QApplication(sys.argv)
print("QApplication created")
mpv = lib.mpv_create()
print("mpv_create:", mpv)
if mpv:
    r = lib.mpv_initialize(mpv)
    print("initialize:", r)
    lib.mpv_terminate_destroy(mpv)
    print("destroyed OK")
else:
    print("mpv_create returned NULL inside QApplication")
