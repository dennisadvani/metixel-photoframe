#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
# cage client launcher for the Metixel frontend (Trixie).
#
# WHY THIS EXISTS
# --------------
# cage starts the Wayland compositor with every output the DRM layer
# reports as "connected" enabled.  A Raspberry Pi 5 has two HDMI ports,
# and an empty port still reports "connected" with a low-resolution
# fallback mode and no EDID.  If both outputs are enabled, the compositor's
# surface spans their bounding box (e.g. 1920 + 1024 = 2944px wide), so the
# frontend renders a 2944x1200 canvas that the compositor then scales back
# down to the 1920x1200 monitor — distorting the slideshow aspect ratio.
#
# This launcher disables outputs with no real monitor (no EDID) BEFORE the
# frontend starts, so the compositor's surface is created at the real
# monitor's native resolution.  It then execs the frontend.
#
# This is a COMPOSITOR-side concern, not an X11 one: it would be required
# whatever the app used to draw.  Do not remove it when tidying up X11 or
# Wayland references — the aspect-ratio distortion it prevents is real and
# has been observed on a Pi 5 with an empty second HDMI port.
#
# The backend also performs the same cleanup defensively (covers mid-session
# hot-plug / desktop testing).
set -u


# Trigger the cursor-hider to park the cursor off-screen.  This is the single
# source of truth — it runs from the same launcher that starts the frontend,
# regardless of how the app is launched (cage systemd unit, CLI, etc.).
# Best-effort: if the hider service isn't running, this is a harmless no-op.
/usr/bin/env python3 /opt/metixel/live/scripts/trigger_cursor_hider.py

# Wait for the compositor's Wayland socket (cage creates it on startup).
for _ in $(seq 1 100); do
    [ -S "${XDG_RUNTIME_DIR:-/run/user/1000}/wayland-0" ] && break
    sleep 0.1
done


# Resolve the compositor's real output geometry and export it for the frontend.
#
# Qt cannot reliably read this itself: a Wayland surface is configured
# asynchronously, so the size Qt sees immediately after show() can still be a
# placeholder (observed: 200x100 and 640x480).  Latching that produced an
# intermittent low-resolution slideshow drawn into the top-left corner.
#
# The compositor already knows the answer, and this script is the one place that
# has just finished talking to it (disabling phantom outputs above), so ask it
# here and hand the result to the frontend as METIXEL_LAUNCH_{WIDTH,HEIGHT}.
# The Qt backend treats those as authoritative.
#
# Best-effort: on failure the variables stay unset and the backend falls back to
# reading the container size, so this can never break the launch.
eval "$(/usr/bin/env python3 - "$XDG_RUNTIME_DIR" <<'PY'
import json
import os
import subprocess
import sys

wl = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
xdg = sys.argv[1]
env = {
    "WAYLAND_DISPLAY": wl,
    "XDG_RUNTIME_DIR": xdg,
    "PATH": "/usr/bin:/bin",
    "HOME": os.environ.get("HOME", "/home/pi"),
}
wlr = "/usr/bin/wlr-randr"
if not os.path.exists(wlr):
    sys.exit(0)


def run(*cmd):
    subprocess.run(cmd, env=env, capture_output=True, timeout=5)


def query():
    out = subprocess.run([wlr, "--json"], env=env, capture_output=True, timeout=5)
    try:
        return json.loads(out.stdout.decode(errors="replace") or "[]")
    except Exception:
        return []


# 1. Disable outputs that report "connected" with no EDID (an empty HDMI port).
#    cage enables every connected output, so leaving a phantom enabled widens the
#    compositor surface and the frontend renders a stretched canvas.
for o in query():
    if o.get("enabled") and not (o.get("make") or o.get("model")):
        name = o.get("name")
        if isinstance(name, str):
            print("metixel cage-launch: disabling phantom output (no monitor):", name,
                  file=sys.stderr)
            run(wlr, "--output", name, "--off")

# 2. Re-query after the change and report the size of the output that remains.
#    This is the geometry the frontend should render at.
for o in query():
    if not o.get("enabled"):
        continue
    # A real monitor carries make/model (set from EDID); prefer those.
    if not (o.get("make") or o.get("model")):
        continue
    # The mode in use is flagged `current` inside `modes`.  Older/newer
    # wlr-randr builds also expose a top-level `current_mode` dict; accept
    # either, because relying on `current_mode` alone silently yields nothing
    # on the version shipping with Trixie (observed: current_mode is None).
    modes = o.get("modes") or []
    current = o.get("current_mode")
    if not isinstance(current, dict):
        current = next(
            (m for m in modes if isinstance(m, dict) and m.get("current")),
            modes[0] if modes and isinstance(modes[0], dict) else None,
        )
    if isinstance(current, dict):
        width, height = current.get("width"), current.get("height")
        if isinstance(width, int) and isinstance(height, int) and width >= 100 and height >= 100:
            print(f"metixel cage-launch: output {o.get('name')} is {width}x{height}",
                  file=sys.stderr)
            print(f"export METIXEL_LAUNCH_WIDTH={width}")
            print(f"export METIXEL_LAUNCH_HEIGHT={height}")
            break
PY
)"

# Launch the frontend as cage's client.
exec python3 -m metixel --mode frontend --config /opt/metixel/data/config.json
