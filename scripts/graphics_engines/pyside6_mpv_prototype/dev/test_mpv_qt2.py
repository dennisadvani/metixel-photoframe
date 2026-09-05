#!/usr/bin/env python3
"""Test mpv_create inside QApplication, capturing mpv's stderr."""

import ctypes
import ctypes.util
import sys

from PySide6.QtWidgets import QApplication

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
