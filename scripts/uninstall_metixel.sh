#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
# =============================================================================
# Metixel Photoframe — Uninstall Script
#
# Reverts a Raspberry Pi back to its pre-setup state (before running
# setup_trixie_metixel.sh). This:
#   1. Reverts all quiet-boot settings (restores factory boot defaults)
#   2. Stops & removes the Metixel systemd services
#   3. Removes the iptables port 80 → 8080 redirect
#   4. Removes the Samba [metixel-media] share and related smb.conf changes
#   5. Removes the Wi-Fi captive-portal (hostapd/dnsmasq) config
#   6. Removes the Wi-Fi power-management / regulatory-domain changes
#   7. Removes the boot config changes (gpu_mem, vc4-kms-v3d)
#   8. Disables loginctl linger for the pi user
#   9. Deletes /opt/metixel
#
# NOTE: This does NOT uninstall the system packages that setup installed
# (cage, ffmpeg, vlc, samba, hostapd, dnsmasq, python3-*, etc.) — those are
# shared system packages and removing them could break other software. It
# only reverts the Metixel-specific configuration and removes the app.
#
# Usage:
#   sudo bash /opt/metixel/scripts/uninstall_metixel.sh
# =============================================================================

set -euo pipefail

# -- Root check --------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)." >&2
    exit 1
fi

METIXEL_DIR="/opt/metixel"
BOOT_CONFIG="/boot/firmware/config.txt"
CMDLINE="/boot/firmware/cmdline.txt"
SMB_CONF="/etc/samba/smb.conf"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     Metixel Photoframe — Uninstall                           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "This will revert the Pi to its pre-Metixel state and delete /opt/metixel."
echo "System packages installed by setup (cage, ffmpeg, vlc, samba, etc.) are"
echo "NOT removed — only Metixel-specific configuration and the app itself."
echo ""
read -p "Type 'yes' to continue: " CONFIRM
if [ "${CONFIRM}" != "yes" ]; then
    echo "Aborted."
    exit 1
fi
echo ""

# ============================================================================
# 1. Revert quiet boot settings
# ============================================================================
echo "[1/9] Reverting quiet boot settings..."
if [ -f "${METIXEL_DIR}/scripts/quiet_boot.sh" ]; then
    bash "${METIXEL_DIR}/scripts/quiet_boot.sh" --revert / || \
        echo "  WARNING: quiet_boot revert reported an error (continuing)"
else
    echo "  ! quiet_boot.sh not found — reverting manually"
    # Manual revert of the key quiet-boot changes (in case the script is gone)
    if [ -f "${CMDLINE}" ]; then
        sed -i -E 's/\bconsole=tty3\b/console=tty1/g; s/\bconsole=ttynull\b/console=tty1/g' "${CMDLINE}"
        sed -i -E 's/\bquiet\b//g; s/\bsplash\b//g; s/\blogo\.nologo\b//g; s/\bvt\.global_cursor_default=[0-9]\b//g; s/\bconsoleblank=[0-9]+\b//g; s/\bloglevel=[0-9]\b//g; s/\bfsck\.mode=[a-z]+\b//g' "${CMDLINE}"
        sed -i -E 's/ +/ /g; s/^ //; s/ $//' "${CMDLINE}"
        echo "  + cmdline.txt quiet params stripped"
    fi
    if [ -f "${BOOT_CONFIG}" ]; then
        sed -i '/^disable_splash=1/d; /^avoid_warnings=2/d' "${BOOT_CONFIG}"
        echo "  + config.txt splash settings removed"
    fi
    rm -f /etc/systemd/system/getty@tty1.service
    rm -rf /etc/systemd/system/getty@tty1.service.d
    rm -f /etc/systemd/system.conf.d/10-metixel-quiet.conf
    rm -f /etc/systemd/journald.conf.d/10-metixel.conf
    rm -f /etc/sysctl.d/99-metixel.conf
    rmdir /etc/systemd/system.conf.d 2>/dev/null || true
    rmdir /etc/systemd/journald.conf.d 2>/dev/null || true
fi

# ============================================================================
# 2. Stop & remove Metixel systemd services
# ============================================================================
echo "[2/9] Stopping and removing Metixel systemd services..."
for svc in metixel-backend metixel-cage metixel-frontend; do
    if systemctl list-unit-files | grep -q "^${svc}\.service"; then
        systemctl stop "${svc}.service" 2>/dev/null || true
        systemctl disable "${svc}.service" 2>/dev/null || true
        echo "  + Stopped & disabled ${svc}.service"
    fi
    if [ -f "/etc/systemd/system/${svc}.service" ]; then
        rm -f "/etc/systemd/system/${svc}.service"
        echo "  + Removed /etc/systemd/system/${svc}.service"
    fi
done
systemctl daemon-reload

# ============================================================================
# 3. Remove iptables port 80 → 8080 redirect
# ============================================================================
echo "[3/9] Removing iptables port 80 → 8080 redirect..."
if iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080 2>/dev/null; then
    iptables -t nat -D PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080
    echo "  + Removed iptables redirect"
fi
# Persist the change (iptables-persistent)
if command -v netfilter-persistent &>/dev/null; then
    netfilter-persistent save 2>/dev/null || true
fi

# ============================================================================
# 4. Remove Samba [metixel-media] share and related changes
# ============================================================================
echo "[4/9] Removing Samba [metixel-media] share..."
if [ -f "${SMB_CONF}" ]; then
    # Remove the [metixel-media] share block
    if grep -q '^\[metixel-media\]' "${SMB_CONF}"; then
        sed -i '/^\[metixel-media\]/,/^$/d' "${SMB_CONF}"
        echo "  + Removed [metixel-media] share block"
    fi
    # Remove 'invalid users = nobody' from [homes] (added by setup)
    if grep -q '^\[homes\]' "${SMB_CONF}"; then
        sed -i '/^\[homes\]/,/^\[/ { /invalid users = nobody/d }' "${SMB_CONF}"
        echo "  + Removed 'invalid users = nobody' from [homes]"
    fi
    # Remove 'load printers = no' and 'disable spoolss = yes' from [global]
    sed -i '/^\[global\]/,/^\[/ { /load printers = no/d; /disable spoolss = yes/d }' "${SMB_CONF}"
    echo "  + Removed printer-disabling lines from [global]"
fi
# Remove the pi Samba password (best-effort)
smbpasswd -x pi 2>/dev/null || true

# ============================================================================
# 5. Remove Wi-Fi captive-portal (hostapd/dnsmasq) config
# ============================================================================
echo "[5/9] Removing Wi-Fi captive-portal config..."
# Restore hostapd defaults
if [ -f "/etc/default/hostapd" ]; then
    cat > /etc/default/hostapd <<'HOSTAPDDEF'
# Defaults for hostapd in case this is a standalone hostapd package
# (not managed by Metixel Photoframe)
DAEMON_CONF=""
HOSTAPDDEF
    echo "  + Restored /etc/default/hostapd"
fi
# Remove the Metixel hostapd.conf (only if it matches the Metixel SSID)
if [ -f "/etc/hostapd/hostapd.conf" ] && grep -q "ssid=Metixel-Setup" "/etc/hostapd/hostapd.conf" 2>/dev/null; then
    rm -f /etc/hostapd/hostapd.conf
    echo "  + Removed /etc/hostapd/hostapd.conf (Metixel-Setup)"
fi
# Remove the Metixel dnsmasq.conf (only if it matches the Metixel config)
if [ -f "/etc/dnsmasq.conf" ] && grep -q "192.168.42.10" "/etc/dnsmasq.conf" 2>/dev/null; then
    rm -f /etc/dnsmasq.conf
    echo "  + Removed /etc/dnsmasq.conf (Metixel captive-portal config)"
fi

# ============================================================================
# 6. Remove Wi-Fi power-management & regulatory-domain changes
# ============================================================================
echo "[6/9] Removing Wi-Fi power-management & regulatory-domain changes..."
rm -f /etc/NetworkManager/conf.d/wifi-powersave-off.conf
echo "  + Removed wifi-powersave-off.conf"
# Remove the cfg80211 regdom override (only if it was set by Metixel)
if [ -f /etc/modprobe.d/cfg80211.conf ] && grep -q "ieee80211_regdom" /etc/modprobe.d/cfg80211.conf; then
    rm -f /etc/modprobe.d/cfg80211.conf
    echo "  + Removed /etc/modprobe.d/cfg80211.conf"
fi

# ============================================================================
# 7. Remove boot config changes (gpu_mem, vc4-kms-v3d)
# ============================================================================
echo "[7/9] Removing boot config changes..."
if [ -f "${BOOT_CONFIG}" ]; then
    # Remove the Metixel KMS overlay block (only if it's the Metixel-added one)
    if grep -q "dtoverlay=vc4-kms-v3d" "${BOOT_CONFIG}"; then
        # Remove the comment header + dtoverlay + gpu_mem lines added by setup
        sed -i '/^# Metixel Photoframe — KMS driver for GPU$/d' "${BOOT_CONFIG}"
        sed -i '/^dtoverlay=vc4-kms-v3d$/d' "${BOOT_CONFIG}"
        sed -i '/^gpu_mem=128$/d' "${BOOT_CONFIG}"
        echo "  + Removed vc4-kms-v3d overlay and gpu_mem=128"
    fi
fi

# ============================================================================
# 8. Disable loginctl linger for pi
# ============================================================================
echo "[8/9] Disabling loginctl linger for pi..."
if loginctl show-user pi 2>/dev/null | grep -q "Linger=yes"; then
    loginctl disable-linger pi 2>/dev/null || true
    echo "  + Disabled linger for pi"
else
    echo "  = Linger not enabled for pi"
fi

# ============================================================================
# 9. Delete /opt/metixel
# ============================================================================
echo "[9/9] Deleting /opt/metixel..."
if [ -d "${METIXEL_DIR}" ]; then
    rm -rf "${METIXEL_DIR}"
    echo "  + Deleted ${METIXEL_DIR}"
else
    echo "  = ${METIXEL_DIR} not present"
fi
# Remove the runtime directory created by setup
rm -rf /run/metixel
echo "  + Removed /run/metixel"

# ============================================================================
# Summary
# ============================================================================
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     Metixel Uninstall Complete                               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Reverted:"
echo "  - Quiet boot settings (factory boot defaults restored)"
echo "  - Metixel systemd services removed"
echo "  - iptables port 80 → 8080 redirect removed"
echo "  - Samba [metixel-media] share removed"
echo "  - Wi-Fi captive-portal (hostapd/dnsmasq) config removed"
echo "  - Wi-Fi power-management / regulatory-domain changes removed"
echo "  - Boot config (gpu_mem, vc4-kms-v3d) removed"
echo "  - loginctl linger disabled"
echo "  - /opt/metixel deleted"
echo ""
echo "NOT removed (shared system packages):"
echo "  cage, xwayland, ffmpeg, vlc, samba, hostapd, dnsmasq,"
echo "  python3-pip, python3-pil, python3-numpy, python3-libcamera,"
echo "  cec-utils, libcec-dev, seatd, cpulimit, iw, python3-evdev,"
echo "  and the pip packages (pi3d, Flask, etc.)"
echo ""
echo "A reboot is recommended to fully restore the pre-setup boot behaviour."
echo "Reboot now? (y/n)"
read -r REBOOT
if [ "${REBOOT}" = "y" ] || [ "${REBOOT}" = "Y" ]; then
    reboot
fi