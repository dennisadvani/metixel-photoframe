#!/usr/bin/env python3
"""Test mpv_create BEFORE QApplication under cage."""
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

# Create mpv BEFORE QApplication
mpv = lib.mpv_create()
print("mpv_create (before Qt):", mpv)
if mpv:
    r = lib.mpv_initialize(mpv)
    print("initialize:", r)

app = QApplication(sys.argv)
print("QApplication created")

# Try creating another mpv after Qt
mpv2 = lib.mpv_create()
print("mpv_create (after Qt):", mpv2)

if mpv:
    lib.mpv_terminate_destroy(mpv)
    print("destroyed first mpv OK")