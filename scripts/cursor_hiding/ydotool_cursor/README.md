# Wayland Cursor-Hiding Prototype

Investigates how to hide the cursor when running Chromium (or any client)
under **cage**, the Wayland kiosk compositor used by Metixel.

## The problem

`cage -d` suppresses client-side decorations but does **not** hide the cursor.
From the cage source (`seat.c`):

- Cage loads the cursor from the `XCURSOR_THEME` environment variable
  (`create_xcursor_manager()` reads `XCURSOR_THEME` / `XCURSOR_SIZE`).
- It always tries to load the `left_ptr` cursor (`DEFAULT_XCURSOR "left_ptr"`).
- `update_capabilities()` only hides the cursor if the seat has **no pointer
  capability** — so with a mouse attached, the cursor always shows.
- Clients can override the cursor via `request_set_cursor` / `cursor-shape-v1`.

## The solution (WORKING): ydotool off-screen move

The **transparent cursor theme** approach does NOT work for native Wayland
clients — cage only loads the theme for XWayland clients, not native Wayland
clients. The working solution is **ydotool** (the Wayland equivalent of
`xdotool`): move the mouse to coordinates far off the display so the cursor
is invisible.

### Setup

```bash
# Build ydotool from source (not in Debian repos)
sudo apt install libevdev-dev cmake scdoc
git clone https://github.com/ReimuNotMoe/ydotool.git
cd ydotool && mkdir build && cd build
cmake .. && make && sudo make install

# Start the ydotoold daemon (as root)
sudo ydotoold

# Fix socket permissions so the pi user can access it
sudo chmod 666 /tmp/.ydotool_socket
sudo chown pi:pi /tmp/.ydotool_socket
```

### The key timing: 0.2s delay

The mouse move must run **0.2s after cage starts** — not before, not
immediately. Moving too early (before cage's pointer is ready) doesn't stick;
moving with no delay also fails. A 0.2s delay is the confirmed sweet spot.

In the systemd service, use `ExecStartPost` with a 0.2s sleep:

```ini
ExecStart=/usr/bin/timeout ${DURATION} /usr/bin/cage -d -- /path/to/client
ExecStartPost=+/bin/sh -c 'sleep 0.2; YDOTOOL_SOCKET=/tmp/.ydotool_socket /usr/local/bin/ydotool mousemove --absolute --x 5000 --y 5000'
```

The `+` prefix runs as root so it can access the daemon socket.

## How to test

```bash
# 1. Build the minimal Wayland test client (a fullscreen coloured surface)
make

# 2. Run cage with the ydotool off-screen move for 5 seconds
bash run_ydotool_test.sh
```

The test client shows a solid colour fullscreen. If the cursor is hidden, you
see only the colour. If it's visible, you see the arrow.

**Important:** cage must run as a **systemd service** (like metixel does) to
get DRM/seat access via logind. Running it directly over SSH fails with
`Could not open target tty: Permission denied` / swapchain errors. The
`run_ydotool_test.sh` script writes and starts a `metixel-cursor-test.service`
that runs cage with the ydotool move for the requested duration.

## Files

| File | Purpose |
|---|---|
| `client.c` | Minimal Wayland client — fullscreen coloured surface |
| `Makefile` | Builds `client` using `wayland-scanner` + `libwayland-client` |
| `run_ydotool_test.sh` | Installs + runs the cage test with the ydotool off-screen move |
| `run_cage_test.sh` | Installs + runs the cage test with the transparent theme (does NOT work for native clients) |
| `cursor/` | The transparent cursor theme (`left_ptr` = 1×1 transparent) — does NOT work for native clients |

## Prerequisites (on the Pi)

```bash
sudo apt install libwayland-dev libwayland-bin wayland-protocols xcursorgen
```

## Notes

- The ydotool off-screen move is the **only** working approach for native
  Wayland clients under cage.
- The transparent cursor theme (`XCURSOR_THEME`) does NOT work for native
  Wayland clients — cage only loads it for XWayland.
- Disabling the HDMI pointer devices via udev does NOT hide the cursor.
- The 0.2s delay is critical — moving too early or too late fails.
- The client's shm buffer uses `/dev/shm` (a relative `mkstemp` template
  fails when the service has no writable working directory).