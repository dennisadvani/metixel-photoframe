# evdev Cursor-Hiding Prototype

Investigates an alternative to the [ydotool off-screen move](../wayland_cursor/)
for hiding the cursor when running Metixel under **cage**, the Wayland kiosk
compositor.

## The problem

`cage -d` suppresses client-side decorations but does **not** hide the
cursor when a mouse is attached (see `scripts/wayland_cursor/README.md`).
The confirmed fix is to move the mouse pointer far off the display so the
cursor is invisible.

## The approach: Python `evdev` native route

`scripts/wayland_cursor` solves this with **ydotool** (a daemon + socket).
This prototype uses the **Python `evdev`** library instead — no daemon, no
socket. It creates a *virtual relative mouse* in the Linux kernel (via
`/dev/uinput`), then blasts it 5000px down and to the right so the cursor is
pushed off-screen into the bottom-right corner.

**Prerequisite (on the Pi):**
```bash
sudo apt install python3-evdev        # provides the evdev Python bindings
sudo usermod -aG input pi             # gives 'pi' write access to /dev/uinput
```

> Note: creating the virtual device requires write access to
> `/dev/uinput`. Running under root (as this test does) works without the
> `usermod`; the `input` group is only needed for the non-root
> `park_cursor.py` path.

## How to test

```bash
# 1. Build the minimal Wayland test client (a fullscreen coloured surface)
make

# 2. Run cage and park the cursor off-screen 2s after cage starts
bash run_evdev_cursor_test.sh
```

If the cursor is hidden you see only the solid colour; if visible, you see
the arrow in a corner.

## The key timing: 2s delay

Like the ydotool variant, the **timing matters**. The test parks the cursor
**2 seconds after cage starts** (a controlled delay, not 0.2s), so cage has
time to register its pointer. The park is fired from a *separate* systemd
unit (`metixel-cursor-park.service`) rather than `ExecStartPost`, which makes
the delay easy to tune:

```bash
bash run_evdev_cursor_test.sh --duration 8 --delay 2   # custom duration/delay
# or via env:
DURATION_SEC=8 CURSOR_DELAY=2 bash run_evdev_cursor_test.sh
```

## Files

| File | Purpose |
|---|---|
| `park_cursor.py` | Creates a virtual relative mouse and parks the cursor off-screen (`--delay`, `--x`, `--y`) |
| `run_evdev_cursor_test.sh` | Installs + runs cage with the delayed evdev park |
| `Makefile` | Builds `client` (see below) |

## How it maps to production

The real fix would call `park_cursor` from the frontend startup (e.g.
`renderer.py`) right after the display backend creates its surface — or from
`cage_launch.sh` — so the cursor is hidden on every boot without needing a
separate service. The `2s` delay built into this run script is an artefact of
the isolated test; in production you'd park once the display is up.