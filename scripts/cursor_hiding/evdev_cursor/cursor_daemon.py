#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""
Persistent cursor-parking daemon (the evdev equivalent of ydotoold).

Creates a virtual absolute mouse device in the kernel (via /dev/uinput) and
KEEPS IT OPEN for the lifetime of the process, firing an absolute move to
far-off coordinates every ``interval`` seconds.  Because the device is
persistent (never torn down), libinput attaches it to the Wayland seat the
moment cage boots, and the periodic absolute moves park the cursor off-screen
— mirroring how ydotoold + a periodic ``ydotool mousemove --absolute`` works.

Unlike the one-shot ``park_cursor.py``, this daemon does NOT use a ``with``
block, so the device node is never destroyed while the service runs.

Run as a systemd service (see cursor-park-daemon.service):

    [Service]
    ExecStart=/usr/bin/env python3 /path/to/cursor_daemon.py
    Restart=always

Or standalone (foreground):

    sudo python3 cursor_daemon.py --interval 0.1
"""

import argparse
import logging
import random
import sys
import time

from evdev import UInput, ecodes as e

logger = logging.getLogger("metixel.cursor_daemon")


class CursorDaemon:
    """Persistent virtual absolute mouse that parks the cursor off-screen."""

    def __init__(
        self,
        x: int = 5000,
        y: int = 5000,
        interval: float = 0.1,
        lo: int = 5000,
        hi: int = 6000,
        width: int = 1920,
        height: int = 1200,
    ):
        self._x = x
        self._y = y
        self._interval = interval
        self._lo = lo
        self._hi = hi
        self._width = width
        self._height = height
        self._ui: UInput | None = None
        self._count = 0

    def start(self) -> None:
        """Create the persistent device (kept open for the process lifetime).

        The ABS range is set to the screen resolution (0..width, 0..height).
        Cage maps this range onto the output, so writing coordinates BEYOND
        the screen (e.g. 5000) places the pointer off-screen.  (If the range
        were 0..1<<20, a value of 5000 would map to ~0.5% — near the top-left
        corner, which is the jitter we saw.)
        """
        cap = {
            e.EV_ABS: (
                (e.ABS_X, (0, 0, self._width)),
                (e.ABS_Y, (0, 0, self._height)),
            ),
            e.EV_KEY: (e.BTN_LEFT, e.BTN_RIGHT),
        }
        try:
            # Persistent device — do NOT use a context manager.
            self._ui = UInput(cap, name="metixel-cursor-daemon")
            # Initialise the absolute axes to 0 so the first park() write to
            # (x, y) is a CHANGE and actually emits an event.  (Writing the
            # same value the device already holds is a kernel no-op.)
            self._ui.write(e.EV_ABS, e.ABS_X, 0)
            self._ui.write(e.EV_ABS, e.ABS_Y, 0)
            self._ui.syn()
            logger.info("Created persistent uinput device metixel-cursor-daemon")
        except PermissionError:
            logger.error(
                "Insufficient permissions to access /dev/uinput. "
                "Run as root or add the user to the 'input' group."
            )
            raise
        except Exception as ex:  # noqa: BLE001
            logger.error("Failed to create uinput device: %s", ex)
            raise

    def park(self) -> None:
        """Send one absolute move to a random off-screen (x, y).

        Random values in [lo, hi] guarantee the value CHANGES on each write,
        which forces the kernel to emit an event.  (Writing the same value the
        device already holds is a no-op and produces no event.)
        """
        if not self._ui:
            return
        try:
            rx = random.randint(self._lo, self._hi)
            ry = random.randint(self._lo, self._hi)
            self._ui.write(e.EV_ABS, e.ABS_X, rx)
            self._ui.write(e.EV_ABS, e.ABS_Y, ry)
            self._ui.syn()
            self._count += 1
            if self._count % 50 == 0:
                logger.info("park() fired %d times (last %d,%d)", self._count, rx, ry)
        except Exception as ex:  # noqa: BLE001
            logger.warning("Failed to send park events: %s", ex)

    def run(self) -> None:
        """Fire the absolute move every ``interval`` seconds forever."""
        self.start()
        logger.info("Parking cursor at (%d, %d) every %.2fs", self._x, self._y, self._interval)
        while True:
            self.park()
            time.sleep(self._interval)

    def close(self) -> None:
        if self._ui:
            try:
                self._ui.close()
            except Exception:  # noqa: BLE001
                pass
            self._ui = None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--x", type=int, default=5000, help="Absolute X (default 5000).")
    parser.add_argument("--y", type=int, default=5000, help="Absolute Y (default 5000).")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.1,
        help="Seconds between absolute moves (default 0.1).",
    )
    parser.add_argument(
        "--lo",
        type=int,
        default=5000,
        help="Lower bound for random off-screen coords (default 5000).",
    )
    parser.add_argument(
        "--hi",
        type=int,
        default=6000,
        help="Upper bound for random off-screen coords (default 6000).",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1920,
        help="Screen width — sets the ABS_X max (default 1920).",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=1200,
        help="Screen height — sets the ABS_Y max (default 1200).",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    daemon = CursorDaemon(
        x=args.x,
        y=args.y,
        interval=args.interval,
        lo=args.lo,
        hi=args.hi,
        width=args.width,
        height=args.height,
    )
    try:
        daemon.run()
    except KeyboardInterrupt:
        logger.info("Interrupted, closing device")
    finally:
        daemon.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())