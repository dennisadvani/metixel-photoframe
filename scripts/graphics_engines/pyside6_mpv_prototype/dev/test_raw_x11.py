#!/usr/bin/env python3
"""Test raw mpv_create under X11 with QApplication."""

import ctypes
import ctypes.util
import locale
import sys

locale.setlocale(locale.LC_NUMERIC, "C")
from PySide6.QtWidgets import QApplication

name = ctypes.util.find_library("mpv") or "libmpv.so.2"
lib = ctypes.CDLL(name)
lib.mpv_create.restype = ctypes.c_void_p
lib.mpv_initialize.argtypes = [ctypes.c_void_p]
lib.mpv_initialize.restype = ctypes.c_int
lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
lib.mpv_terminate_destroy.restype = None

app = QApplication(sys.argv)
print("QApplication created", flush=True)
mpv = lib.mpv_create()
print("mpv_create:", mpv, flush=True)
if mpv:
    r = lib.mpv_initialize(mpv)
    print("initialize:", r, flush=True)
    lib.mpv_terminate_destroy(mpv)
    print("destroyed OK", flush=True)
else:
    print("mpv_create returned NULL", flush=True)
