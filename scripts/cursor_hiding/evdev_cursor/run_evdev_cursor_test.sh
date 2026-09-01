#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Runs cage with a fullscreen coloured client, then — 2 seconds after cage
# starts — parks the mouse cursor off-screen using the Python evdev native
# route (a virtual relative mouse), to test whether the cursor is hidden.
#
# This is the evdev equivalent of scripts/wayland_cursor/run_ydotool_test.sh.
# Unlike ydotool it needs no daemon/socket — just python3-evdev + /dev/uinput
# write access.
#
# IMPORTANT: cage must run as a proper systemd service (like metixel does)
# to get DRM/seat access via logind. Running it directly over SSH fails with
# "Could not open target tty: Permission denied" / swapchain errors.  The
# test also parks the cursor from a *separate* unit (not ExecStartPost) so we
# can fire it at a controlled 2s delay after cage starts.
#
# Prerequisites (on the Pi):
#   sudo apt install python3-evdev        # python3-evdev
#   sudo usermod -aG input pi             # for /dev/uinput access as 'pi'
#   (the cursor-parking unit runs as root, so this is only needed for the
#    standalone park_cursor.py path)
#
# Usage (on the Pi):
#   bash run_evdev_cursor_test.sh [--duration 5] [--delay 2] [--warmup 0.5]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION="${DURATION_SEC:-5}"
DELAY="${CURSOR_DELAY:-2}"
WARMUP="${CURSOR_WARMUP:-0.0}"
INSTANT="${CURSOR_INSTANT:-1}"
SERVICE="metixel-cursor-test"
PARK_SERVICE="metixel-cursor-park"

# Build the park_cursor.py argument list.  --instant makes each of the
# repeated bursts a single relative teleport (no visible sweep, like
# ydotool's absolute jump).  We fire every 0.1s for ~10 bursts so the cursor
# hides as soon as libinput discovers the virtual device.
PARK_BURSTS="${CURSOR_BURSTS:-10}"
PARK_BURST_INTERVAL="${CURSOR_BURST_INTERVAL:-0.1}"
PARK_EXTRA="--bursts ${PARK_BURSTS} --burst-interval ${PARK_BURST_INTERVAL}"
if [ "${INSTANT}" = "1" ]; then
    PARK_EXTRA="${PARK_EXTRA} --instant"
fi
# --absolute replicates ydotool: an EV_ABS virtual mouse placed beyond the
# screen (relative motion clamps at the edge and stays visible).
if [ "${CURSOR_ABSOLUTE:-0}" = "1" ]; then
    PARK_EXTRA="${PARK_EXTRA} --absolute"
fi

# Write both systemd units: one runs cage (with the client), the other waits
# DELAY seconds then parks the cursor off-screen via evdev.
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
Description=Metixel Wayland Cursor Test (evdev park)
After=${SERVICE}.service
Requires=${SERVICE}.service

[Service]
Type=oneshot
# Delaying inside the unit lets cage fully initialise its pointer first.
ExecStart=/bin/sleep ${DELAY}
ExecStart=/usr/bin/env python3 ${SCRIPT_DIR}/park_cursor.py --x 5000 --y 5000 ${PARK_EXTRA} --warmup ${WARMUP}
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