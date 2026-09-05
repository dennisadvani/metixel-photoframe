#!/usr/bin/env python3
"""Minimal repro: does mpv_set_property_string crash after render API use?"""

import ctypes
import ctypes.util
import time

name = ctypes.util.find_library("mpv") or "libmpv.so.2"
lib = ctypes.CDLL(name)
lib.mpv_create.restype = ctypes.c_void_p
lib.mpv_initialize.argtypes = [ctypes.c_void_p]
lib.mpv_initialize.restype = ctypes.c_int
lib.mpv_set_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
lib.mpv_set_property_string.restype = ctypes.c_int
lib.mpv_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
lib.mpv_command.restype = ctypes.c_int
lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
lib.mpv_terminate_destroy.restype = None

mpv = lib.mpv_create()
print("mpv handle:", mpv)
for k, v in [("vo", "null"), ("mute", "yes"), ("hwdec", "auto")]:
    r = lib.mpv_set_property_string(mpv, k.encode(), v.encode())
    print(f"set {k}: {r}")
r = lib.mpv_initialize(mpv)
print("initialize:", r)

# Play a video with vo=null (no render context)
cmd = ["loadfile", "/opt/metixel/data/media/sample_media/13131508_1920_1080_24fps.mp4"]
arr = (ctypes.c_char_p * (len(cmd) + 1))()
for i, c in enumerate(cmd):
    arr[i] = c.encode()
arr[len(cmd)] = None
r = lib.mpv_command(mpv, arr)
print("loadfile:", r)

time.sleep(3)
print("setting pause...")
r = lib.mpv_set_property_string(mpv, b"pause", b"yes")
print("pause set:", r)
print("setting pause again...")
r = lib.mpv_set_property_string(mpv, b"pause", b"yes")
print("pause set again:", r)
print("NO CRASH - basic API works without render context")
lib.mpv_terminate_destroy(mpv)
