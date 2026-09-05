#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""
Park the compositor cursor off-screen using the cross-platform ``pynput``
package.

``pynput`` presents a higher-level API than raw evdev: it creates an internal
virtual mouse (via ``uinput`` on Linux/Wayland) and ``Controller.position``
performs an **absolute** move to an extreme coordinate.

Crucially, unlike the raw-evdev route in ``../evdev_cursor``, ``pynput`` uses
*absolute* coordinates — ``mouse.position = (9999, 9999)`` places the pointer
**beyond** the output extents.  Identify whether that beats the relative-motion
clamping problem (relative moves stop at the screen edge; absolute moves can go
off-screen entirely).

Designed for the Metixel cursor-hiding prototype.  Runs standalone:

    sudo python3 park_cursor.py              # move to (9999, 9999) now
    sudo python3 park_cursor.py --x 5000 --y 5000
    sudo python3 park_cursor.py --delay 0.5  # wait before moving

Linux uinput fallback requires write access to /dev/uinput.  If pynput tries
XWayland/Xlib first and that fails, it falls back to uinput automatically.
"""

import argparse
import sys
import time


def park_cursor(x: int = 9999, y: int = 9999) -> None:
    """Move the cursor to an extreme absolute (x, y), likely off-screen."""
    try:
        from pynput.mouse import Controller

        mouse = Controller()
        mouse.position = (x, y)
        # Give the compositor a moment to process the move so it lands even
        # if the device-sync is slightly asynchronous.
        time.sleep(0.1)
    except PermissionError:
        print(
            "Warning: Insufficient permissions to create the uinput device. "
            "Run as root or add the user to the 'input' group.",
            file=sys.stderr,
        )
    except Exception as ex:  # noqa: BLE001 - never crash the test
        print(f"Failed to park cursor: {ex}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--x",
        type=int,
        default=9999,
        help="Absolute X coordinate to move to (default 9999).",
    )
    parser.add_argument(
        "--y",
        type=int,
        default=9999,
        help="Absolute Y coordinate to move to (default 9999).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to wait before moving (default 0).",
    )
    args = parser.parse_args()

    if args.delay:
        time.sleep(args.delay)

    park_cursor(x=args.x, y=args.y)
    return 0


if __name__ == "__main__":
    sys.exit(main())