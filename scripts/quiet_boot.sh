# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#!/bin/bash
# =============================================================================
# Metixel Photoframe — Quiet Boot Configuration (Trixie)
#
# Configures a Trixie Lite image for silent boot:
#   - Hides kernel messages and systemd output
#   - Hides the blinking cursor
#   - Supports a debug mode toggle (GPIO pin or /boot/firmware/debug file)
#
# Usage: sudo bash quiet_boot.sh /path/to/mounted/rootfs
# =============================================================================

set -euo pipefail

ROOTFS="${1:-}"

if [ -z "${ROOTFS}" ]; then
    echo "Usage: sudo bash quiet_boot.sh /path/to/mounted/rootfs"
    exit 1
fi

# Trixie uses /boot/firmware for the boot partition
BOOT_DIR="${ROOTFS}/boot/firmware"
if [ ! -d "${BOOT_DIR}" ]; then
    echo "Error: ${BOOT_DIR} not found. Is the rootfs mounted?"
    exit 1
fi

echo "=== Configuring Quiet Boot (Trixie) ==="
echo "RootFS: ${ROOTFS}"

# ---------------------------------------------------------------------------
# 1. /boot/firmware/config.txt
# ---------------------------------------------------------------------------
echo "[1/5] Configuring /boot/firmware/config.txt..."
BOOT_CONFIG="${BOOT_DIR}/config.txt"

# Backup existing config
if [ -f "${BOOT_CONFIG}" ]; then
    cp "${BOOT_CONFIG}" "${BOOT_CONFIG}.bak"
fi

cat >> "${BOOT_CONFIG}" <<'EOF'

# --- Metixel Photoframe Quiet Boot (Trixie/KMS) ---
disable_splash=1
avoid_warnings=2

# KMS driver (mainline Mesa) — required for Trixie
dtoverlay=vc4-kms-v3d
gpu_mem=16

# Disable rainbow splash at power-on
disable_splash=1

# Disable Bluetooth (saves power on non-Zero-W models)
# dtoverlay=disable-bt
EOF

# ---------------------------------------------------------------------------
# 2. /boot/firmware/cmdline.txt
# ---------------------------------------------------------------------------
echo "[2/5] Configuring /boot/firmware/cmdline.txt..."
CMDLINE="${BOOT_DIR}/cmdline.txt"

if [ -f "${CMDLINE}" ]; then
    cp "${CMDLINE}" "${CMDLINE}.bak"
    # Append quiet boot parameters
    sed -i 's/$/ quiet loglevel=3 logo.nologo vt.global_cursor_default=0 fsck.mode=auto consoleblank=0/' "${CMDLINE}"
fi

# ---------------------------------------------------------------------------
# 3. Console blanking & cursor hiding
# ---------------------------------------------------------------------------
echo "[3/5] Configuring console blanking..."

# Hide cursor and set console blank timeout
if [ -f "${ROOTFS}/etc/issue" ]; then
    echo -e '\033[?25l' | sudo tee "${ROOTFS}/etc/issue" > /dev/null 2>&1 || true
fi

# Set kernel console blank timeout in sysctl
SYSCTL_CONF="${ROOTFS}/etc/sysctl.d/99-metixel.conf"
mkdir -p "$(dirname "${SYSCTL_CONF}")"
cat > "${SYSCTL_CONF}" <<'EOF'
# Metixel Photoframe — console blanking
kernel.consoleblank=0
EOF

# ---------------------------------------------------------------------------
# 4. systemd console suppression
# ---------------------------------------------------------------------------
echo "[4/5] Configuring systemd..."

# Suppress getty clearing (prevents flicker)
GETTY_OVERRIDE="${ROOTFS}/etc/systemd/system/getty@tty1.service.d"
mkdir -p "${GETTY_OVERRIDE}"
cat > "${GETTY_OVERRIDE}/noclear.conf" <<'EOF'
[Service]
TTYVTDisallocate=no
EOF

# Suppress journal forwarding to console
JOURNAL_CONF="${ROOTFS}/etc/systemd/journald.conf"
if [ -f "${JOURNAL_CONF}" ]; then
    cp "${JOURNAL_CONF}" "${JOURNAL_CONF}.bak"
fi
cat >> "${JOURNAL_CONF}" <<'EOF'

[Journal]
ForwardToConsole=no
MaxLevelConsole=warning
EOF

# ---------------------------------------------------------------------------
# 5. Debug mode
# ---------------------------------------------------------------------------
echo "[5/5] Setting up debug mode..."

# Add debug mode check to rc.local
RC_LOCAL="${ROOTFS}/etc/rc.local"
if [ -f "${RC_LOCAL}" ]; then
    cp "${RC_LOCAL}" "${RC_LOCAL}.bak"
fi

# Insert debug check before 'exit 0'
if [ -f "${RC_LOCAL}" ] && grep -q "exit 0" "${RC_LOCAL}"; then
    sed -i '/^exit 0$/i\
# Metixel Photoframe: Check for debug mode\
if [ -f /boot/debug ] || [ "$(raspi-gpio get 17 2>/dev/null | grep level=1)" ]; then\
    echo "Metixel Photoframe: Debug mode enabled"\
    /bin/dmesg -n 7\
    systemctl stop metixel-frontend.service 2>/dev/null || true\
fi' "${RC_LOCAL}"
fi

echo ""
echo "=== Quiet Boot Configuration Complete ==="
echo "Debug mode:"
echo "  - Create file '/boot/debug' on the FAT partition, OR"
echo "  - Pull GPIO 17 (physical pin 11) HIGH at boot"
echo "  - This re-enables all boot messages and stops the frontend"
