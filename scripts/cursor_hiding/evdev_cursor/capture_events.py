#!/usr/bin/env python3
"""Capture EV_ABS events from a device node for N seconds while a trigger fires."""
import sys
import time

import evdev
import evdev.ecodes as ec

node = sys.argv[1] if len(sys.argv) > 1 else "/dev/input/event5"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0

dev = evdev.InputDevice(node)
print(f"Device: {dev.name} node={node}")
print(f"Capabilities (ABS): {dev.capabilities().get(ec.EV_ABS, [])}")
try:
    print(f"absinfo: {dev.absinfo()}")
except Exception as e:  # noqa: BLE001
    print(f"absinfo error: {e}")

end = time.monotonic() + seconds
count = 0
while time.monotonic() < end:
    try:
        for ev in dev.read():
            if ev.type == ec.EV_ABS:
                print(f"ABS code={ev.code} value={ev.value}")
                count += 1
    except BlockingIOError:
        continue
    except Exception as e:  # noqa: BLE001
        print(f"read error: {e}")
        break
print(f"TOTAL ABS events captured: {count}")