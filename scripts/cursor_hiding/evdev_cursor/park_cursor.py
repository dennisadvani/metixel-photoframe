#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""
Park the compositor cursor off-screen using a virtual relative mouse.

Creates a virtual mouse device in the kernel (via evdev /dev/uinput) and
blasts it far down and to the right, so cage pushes the cursor into the
bottom-right corner and out of view.  This is the evdev equivalent of the
ydotool off-screen move (scripts/wayland_cursor), but needs no daemon/socket
and is a pure Python native route.

Designed for the Metixel cursor-hiding prototype.  It can be run standalone:

    sudo python3 park_cursor.py            # park immediately
    sudo python3 park_cursor.py --delay 2  # wait 2s (let cage init its pointer)
    sudo python3 park_cursor.py --x 5000 --y 5000

Why repeated bursts: a brand-new virtual device takes a few hundred ms for
libinput's udev monitor to discover before its events are routed.  Instead of
guessing a single "warmup" delay (too short → the move is dropped, too long →
the cursor flashes on screen), we fire the full off-screen burst repeatedly
every ``burst_interval`` seconds as early as possible.  The first bursts may
be dropped during discovery, but the moment libinput registers the device, a
burst lands and parks the cursor — hiding it as early as physically possible
while remaining reliable.
"""

import argparse
import sys
import time

import evdev
from evdev import UInput, ecodes as e


def park_cursor(
    x: int = 5000,
    y: int = 5000,
    hold: float = 2.0,
    step: int = 500,
    interval: float = 0.05,
    warmup: float = 0.0,
    instant: bool = True,
    bursts: int = 10,
    burst_interval: float = 0.1,
    absolute: bool = False,
    delays: tuple[float, ...] = (),
) -> None:
    """Move the compositor cursor off-screen via a virtual uinput mouse.

    With ``absolute=False`` (default) this uses relative (EV_REL) motion,
    fire repeatedly so libinput's device-discovery latency doesn't drop the
    move.  NOTE: relative motion CLAMPS at the screen edge — it can push the
    cursor to a corner but it stays on-screen.

    With ``absolute=True`` this replicates ydotool: it creates a device with
    absolute axes (EV_ABS / ABS_X / ABS_Y) and writes coordinates BEYOND the
    output extents, so the pointer is genuinely off-screen.  This is the
    behaviour that actually hides the cursor.

    If ``delays`` is non-empty, the move is fired once at each of those
    (seconds-after-creation) times instead of the fixed ``burst_interval``
    loop.  Staggering the delays covers the unpredictable libinput/udev
    discovery window — at least one fire lands after the device is attached.

    ``step``/``interval`` are ignored when ``instant`` is True.
    """
    if absolute:
        cap = {
            e.EV_ABS: (
                (e.ABS_X, (0, 0, 1 << 20)),
                (e.ABS_Y, (0, 0, 1 << 20)),
            ),
            e.EV_KEY: (e.BTN_LEFT, e.BTN_RIGHT),
        }
    else:
        cap = {
            e.EV_REL: (e.REL_X, e.REL_Y),
            e.EV_KEY: (e.BTN_LEFT, e.BTN_RIGHT),
        }

    steps_x = max(1, int(abs(x) / step)) if x else 0
    steps_y = max(1, int(abs(y) / step)) if y else 0
    total_steps = max(steps_x, steps_y)
    # Leftover remainder after the full steps (keeps the total accurate).
    rem_x = x - steps_x * step if x else 0
    rem_y = y - steps_y * step if y else 0

    def _fire(ui: UInput) -> None:
        """Send one full move (either a single burst or a stepped stream)."""
        if absolute:
            # Absolute jump — place the pointer at (x, y) directly.
            ui.write(e.EV_ABS, e.ABS_X, x)
            ui.write(e.EV_ABS, e.ABS_Y, y)
            ui.syn()
        elif instant:
            if x:
                ui.write(e.EV_REL, e.REL_X, x)
            if y:
                ui.write(e.EV_REL, e.REL_Y, y)
            ui.syn()
        else:
            for _ in range(total_steps):
                if x:
                    ui.write(e.EV_REL, e.REL_X, step)
                if y:
                    ui.write(e.EV_REL, e.REL_Y, step)
                ui.syn()
                time.sleep(interval)
            if rem_x:
                ui.write(e.EV_REL, e.REL_X, rem_x)
            if rem_y:
                ui.write(e.EV_REL, e.REL_Y, rem_y)
            if rem_x or rem_y:
                ui.syn()

    try:
        # Create a virtual input device in the Linux kernel.  Requires
        # write access to /dev/uinput (root, or the input group).
        with UInput(cap, name="metixel-virtual-mouse") as ui:
            # Optional pre-delay before the burst loop (rarely needed).
            time.sleep(max(0.0, warmup))

            if delays:
                # Fire once at each staggered delay (seconds after creation).
                # Covers the unpredictable discovery window.
                last = 0.0
                for d in sorted(delays):
                    if d > last:
                        time.sleep(d - last)
                        last = d
                    _fire(ui)
                # Keep the device alive for the rest of the hold window.
                remaining = hold - last
                if remaining > 0:
                    time.sleep(remaining + 0.2)
            else:
                # Fire the full move repeatedly — drops during discovery, but
                # the first delivered burst parks the cursor as early as
                # possible.
                for _ in range(max(1, bursts)):
                    _fire(ui)
                    time.sleep(burst_interval)

                # Keep the device alive for the rest of the hold window so
                # the compositor has time to process the last burst.
                remaining = hold - (bursts * burst_interval + warmup)
                if remaining > 0:
                    time.sleep(remaining + 0.2)
    except PermissionError:
        print(
            "Warning: Insufficient permissions to access /dev/uinput. "
            "Run as root or add the user to the 'input' group.",
            file=sys.stderr,
        )
    except Exception as ex:  # noqa: BLE001 - never crash the test
        print(f"Failed to park cursor: {ex}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to wait before creating the virtual mouse (default 0).",
    )
    parser.add_argument(
        "--x",
        type=int,
        default=5000,
        help="Relative X movement (default 5000).",
    )
    parser.add_argument(
        "--y",
        type=int,
        default=5000,
        help="Relative Y movement (default 5000).",
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=2.0,
        help="Seconds to keep the virtual device alive (default 2).",
    )
    parser.add_argument(
        "--step",
        type=int,
        default=500,
        help="Relative increment per motion step (default 500). Only used "
        "when --instant is not set.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.05,
        help="Seconds between motion steps (default 0.05). Only used when "
        "--instant is not set.",
    )
    parser.add_argument(
        "--warmup",
        type=float,
        default=0.0,
        help="Optional pre-delay before the burst loop (default 0). The "
        "repeated bursts make a warmup unnecessary.",
    )
    parser.add_argument(
        "--bursts",
        type=int,
        default=10,
        help="Number of times to fire the off-screen move (default 10).",
    )
    parser.add_argument(
        "--burst-interval",
        type=float,
        default=0.1,
        help="Seconds between each burst (default 0.1).",
    )
    parser.add_argument(
        "--instant",
        action="store_true",
        help="Do each move in one relative burst (no visible sweep) instead "
        "of streaming in increments.",
    )
    parser.add_argument(
        "--absolute",
        action="store_true",
        help="Use an absolute-axis (EV_ABS) virtual mouse and place the "
        "pointer at (--x, --y), replicating ydotool. This genuinely moves "
        "the pointer beyond the screen (relative motion clamps at the edge).",
    )
    parser.add_argument(
        "--delays",
        type=float,
        nargs="+",
        default=None,
        help="Fire the move once at each of these seconds-after-creation "
        "times (e.g. --delays 0.2 0.5 1.0 1.5 2.0). Staggering covers the "
        "unpredictable libinput/udev discovery window.",
    )
    args = parser.parse_args()

    if args.delay:
        time.sleep(args.delay)

    park_cursor(
        x=args.x,
        y=args.y,
        hold=args.hold,
        step=args.step,
        interval=args.interval,
        warmup=args.warmup,
        instant=args.instant,
        bursts=args.bursts,
        burst_interval=args.burst_interval,
        absolute=args.absolute,
        delays=tuple(args.delays) if args.delays else (),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())