#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Runs cage with a fullscreen coloured client, then parks the mouse cursor
# off-screen using the Python pynput route (Controller.position absolute
# move), to test whether the cursor is hidden.
#
# This is the pynput equivalent of scripts/cursor_hiding/evdev_cursor and
# wayland_cursor.  Unlike ydotool it needs no daemon/socket; it uses
# pynput's absolute positioning (uinput backend on Wayland).
#
# IMPORTANT: cage must run as a proper systemd service (like metixel does)
# to get DRM/seat access via logind. Running it directly over SSH fails with
# "Could not open target tty: Permission denied" / swapchain errors.  The
# park is fired from a *separate* unit (not ExecStartPost) so we can tune the
# delay independently.
#
# Prerequisites (on the Pi):
#   pip3 install --break-system-packages pynput
#   sudo usermod -aG input pi       # for /dev/uinput access
#   (the parking unit runs as root, so this is only needed for the
#    standalone park_cursor.py path)
#
# Usage (on the Pi):
#   bash run_pynput_cursor_test.sh [--duration 5] [--delay 0.5]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION="${DURATION_SEC:-5}"
DELAY="${CURSOR_DELAY:-0.5}"
SERVICE="metixel-cursor-test"
PARK_SERVICE="metixel-cursor-park"

# Write both systemd units: one runs cage (with the client), the other waits
# DELAY seconds then parks the cursor off-screen via pynput's absolute move.
cat > "${SCRIPT_DIR}/cursor-test.service" <<EOF
[Unit]
Description=Metixel Wayland Cursor Test (cage)
After=seatd.service
Wants=seatd.service

[Service]
Type=simple
User=pi
Group=pi
SupplementaryGroups=video render input tty
Environment=XDG_RUNTIME_DIR=/run/user/1000
ExecStartPre=+/bin/mkdir -p /run/user/1000
ExecStartPre=+/bin/chown pi:pi /run/user/1000
ExecStartPre=+/bin/chmod 0700 /run/user/1000
ExecStart=/usr/bin/timeout ${DURATION} /usr/bin/cage -d -- ${SCRIPT_DIR}/client
# timeout returns 124 when it kills the process after the duration — that's
# the expected "success" for this test.
SuccessExitStatus=124
Restart=no

[Install]
WantedBy=multi-user.target
EOF

cat > "${SCRIPT_DIR}/cursor-park.service" <<EOF
[Unit]
Description=Metixel Wayland Cursor Test (pynput park)
After=${SERVICE}.service
Requires=${SERVICE}.service

[Service]
Type=oneshot
# Run as 'pi' so Python finds pynput in ~/.local (pip --break-system-packages
# installs to the user site-packages; a root-run python3 cannot see them).
User=pi
Group=pi
Environment=HOME=/home/pi
# pynput's Controller is X-driven on Linux; DISPLAY=:0 targets the XWayland
# server that cage exposes inside its session (socket /tmp/.X11-unix/X0).
Environment=DISPLAY=:0
# Delaying inside the unit lets cage fully initialise its pointer first.
ExecStart=/bin/sleep ${DELAY}
ExecStart=/usr/bin/env python3 ${SCRIPT_DIR}/park_cursor.py --x 9999 --y 9999
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF

echo "==> Installing and starting ${SERVICE} (${DURATION}s)"
sudo cp "${SCRIPT_DIR}/cursor-test.service" "/etc/systemd/system/${SERVICE}.service"
sudo cp "${SCRIPT_DIR}/cursor-park.service" "/etc/systemd/system/${PARK_SERVICE}.service"
sudo systemctl daemon-reload
sudo systemctl reset-failed "${SERVICE}" 2>/dev/null || true
sudo systemctl reset-failed "${PARK_SERVICE}" 2>/dev/null || true

# Stop any previous park unit, then start cage and the delayed park.
sudo systemctl stop "${PARK_SERVICE}" 2>/dev/null || true
sudo systemctl start "${SERVICE}"
sudo systemctl start "${PARK_SERVICE}"

echo "==> Waiting ${DURATION}s for the test to run..."
sleep "$((DURATION + 2))"

echo "==> Result:"
if systemctl is-active --quiet "${SERVICE}"; then
    echo "    Service still active (unexpected)."
else
    echo "    Service finished."
fi
echo "    Check the screen: you should see only the solid colour, no arrow."
echo "    Journal: journalctl -u ${SERVICE} --no-pager"
echo "    Park log: journalctl -u ${PARK_SERVICE} --no-pager"

# Clean up the park unit so it doesn't linger.
sudo systemctl stop "${PARK_SERVICE}" 2>/dev/null || true
sudo rm -f "/etc/systemd/system/${PARK_SERVICE}.service"
sudo systemctl daemon-reload