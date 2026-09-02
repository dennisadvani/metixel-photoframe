#!/usr/bin/env python3
"""Minimal test: create a uinput ABS device, write a change, read it back."""
import time

from evdev import UInput, ecodes as e

cap = {
    e.EV_ABS: (
        (e.ABS_X, (0, 0, 1 << 20)),
        (e.ABS_Y, (0, 0, 1 << 20)),
    ),
    e.EV_KEY: (e.BTN_LEFT, e.BTN_RIGHT),
}

ui = UInput(cap, name="metixel-abs-test")
print("created device, writing ABS_X=5000, ABS_Y=5000")
ui.write(e.EV_ABS, e.ABS_X, 5000)
ui.write(e.EV_ABS, e.ABS_Y, 5000)
ui.syn()
print("wrote + syn. sleeping 2s...")
time.sleep(2)
print("closing")
ui.close()