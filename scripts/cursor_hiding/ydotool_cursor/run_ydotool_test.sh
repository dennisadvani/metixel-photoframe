#!/bin/bash
# Test the ydotool off-screen cursor hack: start cage, then immediately move
# the mouse to coordinates far off the display so the cursor is invisible.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION="${DURATION_SEC:-5}"
SERVICE="metixel-cursor-test"

# Write the systemd service that runs cage and then moves the mouse off-screen.
cat > "${SCRIPT_DIR}/cursor-test.service" <<EOF
[Unit]
Description=Metixel Wayland Cursor Test (ydotool)
After=seatd.service
Wants=seatd.service

[Service]
Type=simple
User=pi
Group=pi
SupplementaryGroups=video render input tty
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=YDOTOOL_SOCKET=/tmp/.ydotool_socket
ExecStartPre=+/bin/mkdir -p /run/user/1000
ExecStartPre=+/bin/chown pi:pi /run/user/1000
ExecStartPre=+/bin/chmod 0700 /run/user/1000
ExecStartPre=+/bin/chmod 666 /tmp/.ydotool_socket
ExecStartPre=+/bin/chown pi:pi /tmp/.ydotool_socket
ExecStart=/usr/bin/timeout ${DURATION} /usr/bin/cage -d -- ${SCRIPT_DIR}/client
# Move the mouse off-screen shortly after cage starts (0.2s delay lets cage
# initialise its pointer first).
ExecStartPost=+/bin/sh -c 'sleep 0.2; YDOTOOL_SOCKET=/tmp/.ydotool_socket /usr/local/bin/ydotool mousemove --absolute --x 5000 --y 5000'
SuccessExitStatus=124
Restart=no

[Install]
WantedBy=multi-user.target
EOF

echo "==> Installing and starting ${SERVICE} (${DURATION}s)"
sudo cp "${SCRIPT_DIR}/cursor-test.service" "/etc/systemd/system/${SERVICE}.service"
sudo systemctl daemon-reload
sudo systemctl reset-failed "${SERVICE}" 2>/dev/null || true
sudo systemctl start "${SERVICE}"

echo "==> Waiting ${DURATION}s for the test to run..."
sleep "$((DURATION + 2))"

echo "==> Result:"
if systemctl is-active --quiet "${SERVICE}"; then
    echo "    Service still active (unexpected)."
else
    echo "    Service finished."
    echo "    Check the screen: the cursor should be pushed off-screen."
    echo "    Journal: journalctl -u ${SERVICE} --no-pager"
fi