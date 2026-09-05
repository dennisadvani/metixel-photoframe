#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
# cage client launcher for the Metixel frontend (Phase 1 / Trixie).
#
# WHY THIS EXISTS
# --------------
# cage starts the Wayland compositor with every output the DRM layer
# reports as "connected" enabled.  A Raspberry Pi 5 has two HDMI ports,
# and an empty port still reports "connected" with a low-resolution
# fallback mode and no EDID.  If both outputs are enabled, cage's XWayland
# root window spans their bounding box (e.g. 1920 + 1024 = 2944px wide),
# so pi3d renders a 2944x1200 canvas that the compositor scales back down
# to the 1920x1200 monitor — distorting the slideshow aspect ratio.
#
# This launcher disables outputs with no real monitor (no EDID) BEFORE
# the frontend connects to XWayland, so the XWayland root is created at
# the real monitor's native resolution.  It then execs the frontend.
#
# The backend's Pi3dBackend also performs the same cleanup defensively
# (covers mid-session hot-plug / desktop testing).
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


# Disable phantom outputs (no EDID) before the frontend starts.
/usr/bin/env python3 - "$XDG_RUNTIME_DIR" <<'PY'
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


out = subprocess.run([wlr, "--json"], env=env, capture_output=True, timeout=5)
try:
    outputs = json.loads(out.stdout.decode(errors="replace") or "[]")
except Exception:
    outputs = []

for o in outputs:
    if o.get("enabled") and not (o.get("make") or o.get("model")):
        name = o.get("name")
        if isinstance(name, str):
            print("metixel cage-launch: disabling phantom output (no monitor):", name)
            run(wlr, "--output", name, "--off")
PY

# Launch the frontend as cage's client.
exec python3 -m metixel --mode frontend --config /opt/metixel/data/config.json
