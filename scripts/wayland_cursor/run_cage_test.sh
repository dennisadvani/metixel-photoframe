#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Runs cage with a transparent cursor theme for 5 seconds so we can test
# whether the cursor is hidden.
#
# The key mechanism: cage loads the `left_ptr` cursor from the theme named
# by XCURSOR_THEME. We point it at a theme whose left_ptr is a 1x1
# transparent cursor, so the cursor is invisible.
#
# IMPORTANT: cage must run as a proper systemd service (like metixel does)
# to get DRM/seat access via logind. Running it directly over SSH fails with
# "Could not open target tty: Permission denied" / swapchain errors.
#
# Usage (on the Pi):
#   bash run_cage_test.sh [--duration 5]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION="${DURATION_SEC:-5}"
SERVICE="metixel-cursor-test"

# The transparent cursor theme lives in ./cursor.
CURSOR_DIR="${SCRIPT_DIR}/cursor"

# Generate the transparent XCursor if it doesn't exist yet.
if [[ ! -f "${CURSOR_DIR}/left_ptr" ]]; then
    echo "==> Generating transparent cursor theme"
    python3 -c "from PIL import Image; Image.new('RGBA',(1,1),(0,0,0,0)).save('${CURSOR_DIR}/transparent.png')"
    (cd "${CURSOR_DIR}" && xcursorgen left_ptr.cursor left_ptr)
fi

# XCURSOR_THEME must be a theme NAME that wlroots resolves via XCURSOR_PATH.
# We create a theme dir named "transparent" and add its parent to XCURSOR_PATH.
THEME_NAME="transparent"
THEME_DIR="${SCRIPT_DIR}/themes/${THEME_NAME}/cursors"
mkdir -p "${THEME_DIR}"
cp "${CURSOR_DIR}/left_ptr" "${THEME_DIR}/left_ptr"

# Write the systemd service that runs cage with the transparent theme.
cat > "${SCRIPT_DIR}/cursor-test.service" <<EOF
[Unit]
Description=Metixel Wayland Cursor Test
After=seatd.service
Wants=seatd.service

[Service]
Type=simple
User=pi
Group=pi
SupplementaryGroups=video render input tty
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=XCURSOR_THEME=${THEME_NAME}
Environment=XCURSOR_PATH=${SCRIPT_DIR}/themes
Environment=XCURSOR_SIZE=24
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
fi
echo "    Check the screen: you should see only the solid colour, no arrow."
echo "    If the cursor was visible, the theme approach needs adjusting."
echo ""
echo "    Journal: journalctl -u ${SERVICE} --no-pager"