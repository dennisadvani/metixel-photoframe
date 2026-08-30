#!/usr/bin/env python3
"""Test mpv_create after importing QtCore but WITHOUT creating an app."""
import ctypes
import ctypes.util
import locale
locale.setlocale(locale.LC_NUMERIC, 'C')

name = ctypes.util.find_library("mpv") or "libmpv.so.2"
lib = ctypes.CDLL(name)
lib.mpv_create.restype = ctypes.c_void_p
lib.mpv_initialize.argtypes = [ctypes.c_void_p]
lib.mpv_initialize.restype = ctypes.c_int
lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
lib.mpv_terminate_destroy.restype = None

# Test 1: mpv_create before importing Qt
mpv = lib.mpv_create()
print("mpv_create (before Qt import):", mpv, flush=True)
if mpv:
    lib.mpv_terminate_destroy(mpv)
    print("destroyed OK", flush=True)

# Test 2: import QtCore, then mpv_create
import PySide6.QtCore
print("imported PySide6.QtCore", flush=True)
mpv2 = lib.mpv_create()
print("mpv_create (after QtCore import):", mpv2, flush=True)
if mpv2:
    lib.mpv_terminate_destroy(mpv2)
    print("destroyed OK", flush=True)
else:
    print("mpv_create NULL after QtCore import", flush=True)