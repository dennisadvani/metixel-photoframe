#!/usr/bin/env python3
"""Test python-mpv inside QApplication under cage."""
import sys
from PySide6.QtWidgets import QApplication
import mpv

app = QApplication(sys.argv)
print("QApplication created")
try:
    player = mpv.MPV(vo="null", mute=True)
    print("python-mpv MPV created OK")
    player.terminate()
    print("terminated OK")
except Exception as e:
    print("python-mpv failed:", type(e).__name__, e)