#!/usr/bin/env python3
"""Test python-mpv inside QApplication under cage with faulthandler."""

import faulthandler
import sys

faulthandler.enable()
import mpv
from PySide6.QtWidgets import QApplication

print("creating QApplication...", flush=True)
app = QApplication(sys.argv)
print("QApplication created", flush=True)
try:
    print("creating MPV...", flush=True)
    player = mpv.MPV(vo="null", mute=True)
    print("python-mpv MPV created OK", flush=True)
    player.terminate()
    print("terminated OK", flush=True)
except Exception as e:
    print("python-mpv failed:", type(e).__name__, e, flush=True)
