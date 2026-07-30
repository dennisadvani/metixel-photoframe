# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#!/bin/bash
# =============================================================================
# Metixel Photoframe — Trixie Setup Script
#
# Complete setup for a fresh Trixie Lite install on Raspberry Pi 2/3/Zero 2 W.
# Run this ONCE after flashing Trixie Lite to set up Metixel Photoframe.
#
# Usage (run on the Pi as root or with sudo):
#   git clone https://github.com/dennisadvani/metixel-photoframe.git /opt/metixel
#   sudo bash /opt/metixel/scripts/setup_trixie.sh
# =============================================================================

set -euo pipefail

# -- Detect project root -----------------------------------------------------
if [ -f "/opt/metixel/scripts/setup_trixie.sh" ]; then
    METIXEL_DIR="/opt/metixel"
else
    METIXEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

echo "=== Metixel Photoframe — Trixie Setup ==="
echo "Project root: ${METIXEL_DIR}"
echo "Target: Raspberry Pi 2/3/Zero 2 W (Trixie + KMS)"
echo ""

# -- System packages ---------------------------------------------------------
echo "[1/9] Updating package lists..."
sudo apt-get update -qq

echo "[2/9] Installing system packages..."
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-pip \
    python3-pil \
    python3-numpy \
    python3-libcamera \
    cec-utils \
    libcec-dev \
    cage \
    xwayland \
    wlr-randr \
    seatd \
    ffmpeg \
    vlc-bin vlc-plugin-base vlc-plugin-video-output vlc-data \
    cpulimit \
    git \
    samba \
    iptables-persistent \
    hostapd \
    dnsmasq

# Redirect port 80 → 8080 so the web dashboard is reachable without a port
# number and the captive portal detection works on port 80.
# The Flask app runs as user 'pi' on port 8080 — this iptables rule avoids
# needing root privileges to bind port 80.
if ! iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080 2>/dev/null; then
    iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080
    netfilter-persistent save
    echo "iptables: port 80 → 8080 redirect installed"
fi

# -- Python packages ---------------------------------------------------------
echo "[3/9] Installing Python packages..."
cd "${METIXEL_DIR}"

# Use --ignore-installed to skip packages already provided by apt
# (python3-numpy, python3-pil). This avoids "Cannot uninstall" errors
# with PEP 668 externally-managed environments.
PIP_IGNORE="--break-system-packages --ignore-installed"

sudo pip3 install ${PIP_IGNORE} pi3d 2>/dev/null || \
    pip3 install ${PIP_IGNORE} pi3d

sudo pip3 install ${PIP_IGNORE} -r requirements-phase1.txt 2>/dev/null || \
    pip3 install ${PIP_IGNORE} -r requirements-phase1.txt

# -- Directory structure -----------------------------------------------------
echo "[4/9] Creating directory structure..."
sudo mkdir -p /opt/metixel/media /opt/metixel/media/sync/immich /opt/metixel/media/my_media /opt/metixel/cache /opt/metixel/logs /opt/metixel/etc /run/metixel
sudo cp -n "${METIXEL_DIR}/etc/config.example.json" /opt/metixel/etc/config.json 2>/dev/null || true
sudo cp -n "${METIXEL_DIR}/etc/logging.conf" /opt/metixel/etc/logging.conf 2>/dev/null || true
sudo chown -R pi:pi /opt/metixel /run/metixel 2>/dev/null || true

# -- systemd services --------------------------------------------------------
echo "[5/9] Installing systemd services..."
sudo cp "${METIXEL_DIR}/systemd/metixel-backend.service" /etc/systemd/system/
sudo cp "${METIXEL_DIR}/systemd/metixel-cage.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable metixel-backend
sudo systemctl enable metixel-cage

# Enable linger for the pi user so systemd-logind creates
# /run/user/1000 at boot even without a user login.
# Required for cage (Wayland compositor) to start on headless boots.
sudo loginctl enable-linger pi

# -- Enable Wi-Fi -----------------------------------------------------------
echo "[6/9] Enabling Wi-Fi..."
# Raspberry Pi Imager disables WiFi at the OS level if you skip Wi-Fi
# configuration during imaging.  Re-enable it before configuring hostapd
# so the wireless interface is available for the captive portal.
sudo rfkill unblock wifi 2>/dev/null || true
sudo rfkill unblock wlan 2>/dev/null || true
# Also ensure NetworkManager doesn't treat wlan0 as unmanaged
if command -v nmcli &>/dev/null; then
    sudo nmcli radio wifi on 2>/dev/null || true
fi

# -- Captive Portal (AP mode) -----------------------------------------------
echo "[7/9] Configuring Wi-Fi captive portal (AP fallback)..."
# Configure hostapd (open network "Metixel-Setup")
sudo tee /etc/hostapd/hostapd.conf > /dev/null <<'HOSTAPDEOF'
interface=wlan0
driver=nl80211
ssid=Metixel-Setup
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=0
HOSTAPDEOF
sudo sed -i 's|^#DAEMON_CONF=""|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd 2>/dev/null || true
# Replace the entire file with a clean version to avoid quoting issues
sudo tee /etc/default/hostapd > /dev/null <<'HOSTAPDDEF'
# Defaults for hostapd — managed by Metixel Photoframe
DAEMON_CONF=/etc/hostapd/hostapd.conf
DAEMON_OPTS=
HOSTAPDDEF

# Configure dnsmasq (DHCP + captive DNS)
sudo tee /etc/dnsmasq.conf > /dev/null <<'DNSMASQEOF'
interface=wlan0
dhcp-range=192.168.42.10,192.168.42.100,12h
dhcp-option=3,192.168.42.1
dhcp-option=6,192.168.42.1
address=/#/192.168.42.1
no-resolv
DNSMASQEOF

# Disable auto-start — the Metixel NetworkMonitor controls these services
sudo systemctl disable hostapd dnsmasq 2>/dev/null || true
sudo systemctl unmask hostapd dnsmasq 2>/dev/null || true

# -- Samba share (media only) ------------------------------------------------
# Only shares /opt/metixel/media so users can add/remove photos and videos.
# For full-project access during development, run setup_trixie_dev_env.sh
# which adds a separate [metixel] share pointing to /opt/metixel.
echo "[8/9] Configuring Samba share (/opt/metixel/media as 'metixel-media')..."

# Add 'invalid users = nobody' to the [homes] section so the system
# 'nobody' user doesn't get an auto-share (don't comment out [homes]
# itself — orphaned lines will corrupt smb.conf).
# Also disable printer sharing in [global] (don't comment out [printers]
# / [print$] headers for the same reason).
SMB_CONF="/etc/samba/smb.conf"
if grep -q '^\[homes\]' "${SMB_CONF}" 2>/dev/null; then
    if ! grep -A10 '^\[homes\]' "${SMB_CONF}" | grep -q 'invalid users'; then
        sudo sed -i '/^\[homes\]/a\   invalid users = nobody' "${SMB_CONF}"
    fi
fi
if ! grep -q 'load printers = no' "${SMB_CONF}" 2>/dev/null; then
    sudo sed -i '/^\[global\]/a\   load printers = no' "${SMB_CONF}"
fi
if ! grep -q 'disable spoolss = yes' "${SMB_CONF}" 2>/dev/null; then
    sudo sed -i '/^\[global\]/a\   disable spoolss = yes' "${SMB_CONF}"
fi

# Append share definition to smb.conf if not already present
if ! grep -q '\[metixel-media\]' "${SMB_CONF}" 2>/dev/null; then
    sudo tee -a "${SMB_CONF}" > /dev/null <<'SMBEOF'
[metixel-media]
   comment = Metixel Photoframe Media Share
   path = /opt/metixel/media
   browseable = yes
   read only = no
   guest ok = no
   valid users = pi
   create mask = 0664
   directory mask = 0775
   force user = pi
   force group = pi
SMBEOF
fi

# Set Samba password for user pi (non-interactive)
echo -e "raspberry\nraspberry" | sudo smbpasswd -a -s pi 2>/dev/null || true

sudo systemctl enable smbd
sudo systemctl restart smbd

# -- Boot config -------------------------------------------------------------
echo "[9/9] Configuring boot..."

BOOT_CONFIG="/boot/firmware/config.txt"
if [ -f "${BOOT_CONFIG}" ]; then
    # Ensure KMS overlay is enabled
    if ! grep -q "dtoverlay=vc4-kms-v3d" "${BOOT_CONFIG}"; then
        echo "" | sudo tee -a "${BOOT_CONFIG}"
        echo "# Metixel Photoframe — KMS driver for GPU" | sudo tee -a "${BOOT_CONFIG}"
        echo "dtoverlay=vc4-kms-v3d" | sudo tee -a "${BOOT_CONFIG}"
        echo "gpu_mem=16" | sudo tee -a "${BOOT_CONFIG}"
    fi
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Reboot and Metixel will auto-start: sudo reboot"
