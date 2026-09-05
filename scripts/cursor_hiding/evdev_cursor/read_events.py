#!/usr/bin/env python3
"""Read events from a uinput device node for a few seconds (non-blocking)."""
import select
import sys
import time

from evdev import InputDevice

node = sys.argv[1] if len(sys.argv) > 1 else "/dev/input/event5"
duration = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0

d = InputDevice(node)
print("name:", d.name)
print("capabilities:", d.capabilities())
print(f"reading {node} for {duration}s (non-blocking)...")
start = time.time()
count = 0
while time.time() - start < duration:
    r, _, _ = select.select([d], [], [], 0.5)
    if r:
        try:
            for ev in d.read():
                print("EVENT:", ev)
                count += 1
        except Exception as ex:  # noqa: BLE001
            print("read error:", ex)
            break
print(f"done, {count} events in {duration}s")