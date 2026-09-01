#!/usr/bin/env python3
"""Test mpv_create under cage/Wayland."""
import ctypes
import ctypes.util

name = ctypes.util.find_library("mpv") or "libmpv.so.2"
lib = ctypes.CDLL(name)
lib.mpv_create.restype = ctypes.c_void_p
lib.mpv_initialize.argtypes = [ctypes.c_void_p]
lib.mpv_initialize.restype = ctypes.c_int
lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
lib.mpv_terminate_destroy.restype = None

print("lib:", name)
mpv = lib.mpv_create()
print("mpv_create:", mpv)
if mpv:
    r = lib.mpv_initialize(mpv)
    print("initialize:", r)
    lib.mpv_terminate_destroy(mpv)
    print("destroyed OK")