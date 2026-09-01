# pynput Cursor-Hiding Prototype

Investigates a third approach to hiding the cursor when running Metixel under
**cage**, the Wayland kiosk compositor — this time using the cross-platform
**`pynput`** package.

## The problem

`cage -d` suppresses client-side decorations but does **not** hide the cursor
when a mouse is attached. The confirmed fix is to move the pointer off-screen
(see `../wayland_cursor/README.md`).

## Why pynput?

The other two prototypes both use **relative** motion under the hood:

- `../wayland_cursor` — ydotool (works, but a daemon/socket + from-source build).
- `../evdev_cursor` — raw evdev virtual relative mouse (reliable, but relative
  motion clamps at the screen edge; and requires repeated bursts to beat the
  device-discovery race).

`pynput` offers a **higher level API with absolute positioning**:
`Controller.position = (9999, 9999)` performs an *absolute* move — the pointer
is placed **beyond** the output extents, so it goes genuinely off-screen (like
ydotool's single absolute jump). On Linux/Wayland, pynput uses `uinput` under
the hood; it tries Xlib (XWayland) first and falls back to uinput if that
fails.

## Setup (on the Pi)

```bash
# pynput is a pip package (not in Debian apt)
pip3 install --break-system-packages pynput

# /dev/uinput access for non-root
sudo usermod -aG input pi
```

> The cursor-parking unit runs as root, so `usermod` is only needed for the
> standalone `park_cursor.py` path.

## How to test

```bash
# 1. Build the minimal Wayland test client (a fullscreen coloured surface)
make

# 2. Run cage and park the cursor off-screen (absolute move) after cage starts
bash run_pynput_cursor_test.sh --delay 0.5
```

If the cursor is hidden you see only the solid colour; if visible, you see the
arrow. Tune the delay after cage starts with `--delay` (or `CURSOR_DELAY`).

## Files

| File | Purpose |
|---|---|
| `park_cursor.py` | `pynput.mouse.Controller.position = (9999, 9999)` absolute off-screen move |
| `run_pynput_cursor_test.sh` | Installs + runs cage with the delayed pynput park |
| `client.c` / `Makefile` | Minimal fullscreen coloured Wayland test client |

## Notes / expected caveats

- If pynput falls back to uinput, it may still suffer the same
  device-discovery race as raw evdev; the `--delay` knob lets us test whether
  an absolute move lands even when fired very early.
- `Controller.position` clamps to the screen in some compositor/backend
  combinations — the 9999 target is chosen well beyond any real resolution to
  maximise the chance it truly lands off-screen.