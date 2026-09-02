#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Cursor-hiding test using a PERSISTENT evdev daemon (the evdev equivalent of
# ydotoold).  The daemon creates a virtual absolute mouse device and KEEPS IT
# OPEN, firing an absolute move every 0.1s.  Because the device is persistent,
# libinput attaches it to the Wayland seat the moment cage boots, and the
# periodic absolute moves park the cursor off-screen.
#
# This is the evdev equivalent of scripts/wayland_cursor/run_ydotool_test.sh.
# Unlike ydotool it needs no daemon/socket — just python3-evdev + /dev/uinput
# write access.
#
# IMPORTANT: cage must run as a proper systemd service (like metixel does)
# to get DRM/seat access via logind. Running it directly over SSH fails with
# "Could not open target tty: Permission denied" / swapchain errors.
#
# Prerequisites (on the Pi):
#   sudo apt install python3-evdev        # python3-evdev
#   sudo usermod -aG input pi             # for /dev/uinput access as 'pi'
#   (the daemon unit runs as root, so this is only needed for the
#    standalone cursor_daemon.py path)
#
# Usage (on the Pi):
#   bash run_evdev_cursor_test.sh [--duration 10]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION="${DURATION_SEC:-10}"
SERVICE="metixel-cursor-test"
DAEMON_SERVICE="metixel-cursor-daemon"

# The daemon fires an absolute move every 0.1s for the whole run.
DAEMON_INTERVAL="${CURSOR_DAEMON_INTERVAL:-0.1}"

# Write both systemd units: one runs cage (with the client), the other runs
# the persistent cursor-parking daemon (keeps the device open + fires moves).
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

cat > "${SCRIPT_DIR}/cursor-daemon.service" <<EOF
[Unit]
Description=Metixel Wayland Cursor Test (evdev park daemon)
After=seatd.service

[Service]
Type=simple
# Persistent daemon — keeps the uinput device open and fires an absolute
# move every ${DAEMON_INTERVAL}s.  Runs as root for /dev/uinput access.
# ABS range is set to the screen (1920x1200) so coords beyond it go off-screen.
ExecStart=/usr/bin/env python3 ${SCRIPT_DIR}/cursor_daemon.py --x 5000 --y 5000 --interval ${DAEMON_INTERVAL} --width 1920 --height 1200
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF

echo "==> Installing and starting ${SERVICE} + ${DAEMON_SERVICE}"
sudo cp "${SCRIPT_DIR}/cursor-test.service" "/etc/systemd/system/${SERVICE}.service"
sudo cp "${SCRIPT_DIR}/cursor-daemon.service" "/etc/systemd/system/${DAEMON_SERVICE}.service"
sudo systemctl daemon-reload
sudo systemctl reset-failed "${SERVICE}" 2>/dev/null || true
sudo systemctl reset-failed "${DAEMON_SERVICE}" 2>/dev/null || true

# Stop any previous units, then start the daemon FIRST (so the persistent
# device exists when cage boots), then cage.
sudo systemctl stop "${DAEMON_SERVICE}" 2>/dev/null || true
sudo systemctl stop "${SERVICE}" 2>/dev/null || true
sudo systemctl start "${DAEMON_SERVICE}"
sleep 0.5
sudo systemctl start "${SERVICE}"

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
echo "    Daemon log: journalctl -u ${DAEMON_SERVICE} --no-pager"

# Clean up the daemon unit so it doesn't linger.
sudo systemctl stop "${DAEMON_SERVICE}" 2>/dev/null || true
sudo rm -f "/etc/systemd/system/${DAEMON_SERVICE}.service"
sudo systemctl daemon-reload