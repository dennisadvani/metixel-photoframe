# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#!/bin/bash
# =============================================================================
# Metixel Photoframe — Trixie Launch Script
#
# Starts Metixel under cage (minimal Wayland kiosk compositor) with XWayland.
# This provides the X11 surface pi3d needs on Trixie with KMS/DRM.
#
# This is the primary launch method for all Phase 1 targets:
#   Pi Zero 2 W, Pi 2, Pi 3
#
# For desktop development without Pi hardware, run directly:
#   python3 -m metixel --mode frontend --config etc/config.json
# =============================================================================

set -euo pipefail

METIXEL_DIR="/opt/metixel"
CONFIG="${METIXEL_DIR}/etc/config.json"

# -- Check dependencies ------------------------------------------------------

if ! command -v cage &>/dev/null; then
    echo "cage is not installed. Install with:"
    echo "  sudo apt-get install -y cage xwayland"
    exit 1
fi

if ! python3 -c "import pi3d" 2>/dev/null; then
    echo "pi3d is not installed. Install with:"
    echo "  sudo pip3 install pi3d --break-system-packages"
    exit 1
fi

# -- Workaround: cage doesn't pass exit signals well. Use a trap. ------------
# Cage captures the terminal; we use a wrapper script that runs metixel
# and handles Ctrl+C properly.

cleanup() {
    echo ""
    echo "Metixel stopped."
    exit 0
}
trap cleanup INT TERM

# -- Launch ------------------------------------------------------------------

echo "Starting Metixel Photoframe under cage (Wayland + XWayland)..."
echo "Press Ctrl+C to exit."

cd "${METIXEL_DIR}"

# cage starts a Wayland session with XWayland support.
# The -- argument passes the command to run inside the session.
exec cage -- python3 -m metixel --mode frontend --config "${CONFIG}"
