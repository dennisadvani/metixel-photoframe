# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#!/bin/bash
# =============================================================================
# Metixel Photoframe — LEGACY MONOLITHIC Trixie Setup Script (TESTING ONLY)
#
# Installs the OLD monolithic layout (pre-1.2.2 / pre-Blue-Green) so you can
# test the monolithic → atomic migration path on a real device.
#
# This is a TESTING helper, NOT for end users.  It deliberately:
#   * Clones the repo to /opt/metixel and checks out a SPECIFIC version
#     (default v1.2.2, override with METIXEL_VERSION) — no self-bootstrap
#     re-exec, so the script you run is the one that installs.
#   * Installs the MONOLITHIC layout: code stays at /opt/metixel, config at
#     /opt/metixel/etc, media/logs/cache at /opt/metixel/.
#   * Installs the MONOLITHIC systemd units (WorkingDirectory=/opt/metixel,
#     PYTHONPATH=/opt/metixel/src, --config /opt/metixel/etc/config.json).
#   * Shares /opt/metixel/media via Samba (the old path).
#
# USAGE:
#   sudo bash legacy_setup_trixie_metixel.sh
#   METIXEL_VERSION=v1.2.1 sudo bash legacy_setup_trixie_metixel.sh
# =============================================================================

set -euo pipefail

# -- Root check --------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)." >&2
    exit 1
fi

# -- Version to install (default v1.2.2, the last monolithic release) --------
LEGACY_VERSION="${METIXEL_VERSION:-v1.2.2}"
REPO_URL="${METIXEL_REPO_URL:-https://github.com/dennisadvani/metixel-photoframe.git}"

# -- Clone / checkout the repo to /opt/metixel -------------------------------
# If the script is run from inside a checkout, use that.  Otherwise clone the
# repo to /opt/metixel and check out the requested version.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
if [ -f "${REPO_ROOT}/pyproject.toml" ]; then
    METIXEL_DIR="${REPO_ROOT}"
    echo "Using repo at ${METIXEL_DIR}"
elif [ -d "/opt/metixel/.git" ]; then
    METIXEL_DIR="/opt/metixel"
    echo "Updating existing repo at /opt/metixel..."
    cd "${METIXEL_DIR}"
    git fetch origin --tags 2>/dev/null || true
    git checkout "${LEGACY_VERSION}" 2>/dev/null || true
    echo "  → Checked out ${LEGACY_VERSION}"
elif [ -d "/opt/metixel" ] && [ "$(ls -A /opt/metixel 2>/dev/null)" ]; then
    echo "/opt/metixel exists but is not a git repository."
    echo "Moving existing content to /opt/metixel.bak..."
    mv /opt/metixel /opt/metixel.bak.$(date +%s)
    mkdir -p /opt/metixel
    echo "Cloning Metixel Photoframe..."
    git clone "${REPO_URL}" /opt/metixel
    cd /opt/metixel
    git checkout "${LEGACY_VERSION}" 2>/dev/null || true
    echo "  → Checked out ${LEGACY_VERSION}"
    METIXEL_DIR="/opt/metixel"
else
    mkdir -p /opt/metixel
    echo "Cloning Metixel Photoframe..."
    git clone "${REPO_URL}" /opt/metixel
    cd /opt/metixel
    git checkout "${LEGACY_VERSION}" 2>/dev/null || true
    echo "  → Checked out ${LEGACY_VERSION}"
    METIXEL_DIR="/opt/metixel"
fi

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Metixel — LEGACY MONOLITHIC Setup (testing only)            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "Project root: ${METIXEL_DIR}"
echo "Version: ${LEGACY_VERSION}"
echo "Target: Raspberry Pi 2/3/4/5 (Trixie) — MONOLITHIC layout"
echo ""

# -- Release channel & WiFi country (from env or prompt) --------------------
RELEASE_CHANNEL="${METIXEL_CHANNEL:-}"
WIFI_COUNTRY="${METIXEL_WIFI_COUNTRY:-}"

if [ -z "${RELEASE_CHANNEL}" ]; then
    echo "Release channel:"
    echo "  stable = Latest stable release (recommended)"
    echo "  beta   = Pre-release with latest features"
    read -p "  Channel [stable]: " RELEASE_CHANNEL
    RELEASE_CHANNEL="${RELEASE_CHANNEL:-stable}"
    case "$RELEASE_CHANNEL" in
        stable|beta) ;;
        *)
            echo "  Invalid choice '${RELEASE_CHANNEL}' — using stable."
            RELEASE_CHANNEL="stable"
            ;;
    esac
fi
echo "  → Using ${RELEASE_CHANNEL} channel"

if [ -z "${WIFI_COUNTRY}" ]; then
    echo ""
    echo "WiFi country code (e.g. AU, US, GB, DE, NZ):"
    echo "  This sets the regulatory domain for correct channel availability."
    read -p "  Country code [AU]: " WIFI_COUNTRY
    WIFI_COUNTRY="${WIFI_COUNTRY:-AU}"
    WIFI_COUNTRY=$(echo "$WIFI_COUNTRY" | tr '[:lower:]' '[:upper:]')
fi
echo "  → WiFi country: ${WIFI_COUNTRY}"
echo ""

# -- System packages ---------------------------------------------------------
echo "[1/9] Updating package lists..."
apt-get update -qq

echo "[2/9] Installing system packages..."
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-pip \
    python3-pil \
    python3-numpy \
    python3-libcamera \
    libopenblas0 \
    cec-utils \
    ddcutil \
    i2c-tools \
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
    dnsmasq \
    iw \
    python3-evdev

# Redirect port 80 → 8080 so the web dashboard is reachable without a port
# number and the captive portal detection works on port 80.
if ! iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080 2>/dev/null; then
    iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080
    netfilter-persistent save
    echo "iptables: port 80 → 8080 redirect installed"
fi

# -- Python packages ---------------------------------------------------------
echo "[3/9] Installing Python packages..."
cd "${METIXEL_DIR}"

PIP_IGNORE="--break-system-packages --ignore-installed"

pip3 install ${PIP_IGNORE} pi3d 2>/dev/null || \
    pip3 install ${PIP_IGNORE} pi3d

pip3 install ${PIP_IGNORE} -r requirements-pip.txt 2>/dev/null || \
    pip3 install ${PIP_IGNORE} -r requirements-pip.txt

# Dev & testing tools (pytest, pytest-cov, ruff, mypy) — installed as part of
# the base install so no separate dev-env script is needed.
pip3 install ${PIP_IGNORE} ruff mypy pytest pytest-cov 2>/dev/null || \
    pip3 install ${PIP_IGNORE} ruff mypy pytest pytest-cov

# -- Git safe.directory ------------------------------------------------------
echo "[3b/9] Marking repository as safe for git..."
git config --system --add safe.directory /opt/metixel 2>/dev/null || true

# -- Directory structure (MONOLITHIC layout) --------------------------------
echo "[4/9] Creating directory structure (monolithic)..."
mkdir -p /opt/metixel/media /opt/metixel/media/sync/immich /opt/metixel/media/my_media /opt/metixel/cache /opt/metixel/logs /opt/metixel/etc /run/metixel
cp -n "${METIXEL_DIR}/etc/config.example.json" /opt/metixel/etc/config.json 2>/dev/null || true
cp -n "${METIXEL_DIR}/etc/logging.conf" /opt/metixel/etc/logging.conf 2>/dev/null || true
chown -R pi:pi /opt/metixel /run/metixel 2>/dev/null || true

# -- systemd services (MONOLITHIC units) ------------------------------------
echo "[5/9] Installing systemd services..."
cp "${METIXEL_DIR}/systemd/metixel-backend.service" /etc/systemd/system/
cp "${METIXEL_DIR}/systemd/metixel-cage.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable metixel-backend
systemctl enable metixel-cage

# Enable linger for the pi user so systemd-logind creates
# /run/user/1000 at boot even without a user login.
loginctl enable-linger pi

# -- Enable Wi-Fi -----------------------------------------------------------
echo "[6/9] Enabling Wi-Fi..."
rfkill unblock wifi 2>/dev/null || true
rfkill unblock wlan 2>/dev/null || true
if command -v nmcli &>/dev/null; then
    nmcli radio wifi on 2>/dev/null || true
fi

echo "     Disabling Wi-Fi power management..."
mkdir -p /etc/NetworkManager/conf.d
tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf > /dev/null <<'NMPOWEREOF'
[connection]
wifi.powersave = 2
NMPOWEREOF

if command -v iw &>/dev/null; then
    iw reg set "$WIFI_COUNTRY" 2>/dev/null || true
    echo "     WiFi regulatory domain set to: $WIFI_COUNTRY"
fi
if [ -f /etc/modprobe.d/cfg80211.conf ]; then
    sed -i "s/^options cfg80211 ieee80211_regdom=.*/options cfg80211 ieee80211_regdom=$WIFI_COUNTRY/" /etc/modprobe.d/cfg80211.conf 2>/dev/null || true
else
    echo "options cfg80211 ieee80211_regdom=$WIFI_COUNTRY" > /etc/modprobe.d/cfg80211.conf
fi
# Write to Metixel config so the Web UI reflects it
python3 -c "
import json, os
cfg_path = '/opt/metixel/etc/config.json'
if os.path.exists(cfg_path):
    with open(cfg_path) as f:
        cfg = json.load(f)
    cfg.setdefault('network', {})['wifi_country'] = '$WIFI_COUNTRY'
    cfg.setdefault('update', {})['channel'] = '$RELEASE_CHANNEL'
    with open(cfg_path, 'w') as f:
        json.dump(cfg, f, indent=2)
" 2>/dev/null || true

# -- Captive Portal (AP mode) -----------------------------------------------
echo "[7/9] Configuring Wi-Fi captive portal (AP fallback)..."
tee /etc/hostapd/hostapd.conf > /dev/null <<'HOSTAPDEOF'
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
sed -i 's|^#DAEMON_CONF=""|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd 2>/dev/null || true
tee /etc/default/hostapd > /dev/null <<'HOSTAPDDEF'
# Defaults for hostapd — managed by Metixel Photoframe
DAEMON_CONF=/etc/hostapd/hostapd.conf
DAEMON_OPTS=
HOSTAPDDEF

tee /etc/dnsmasq.conf > /dev/null <<'DNSMASQEOF'
interface=wlan0
dhcp-range=192.168.42.10,192.168.42.100,12h
dhcp-option=3,192.168.42.1
dhcp-option=6,192.168.42.1
address=/#/192.168.42.1
no-resolv
DNSMASQEOF

if ! systemctl disable hostapd dnsmasq; then
    echo "WARNING: Could not disable hostapd/dnsmasq auto-start — may need manual fix"
fi
systemctl unmask hostapd dnsmasq 2>/dev/null || true

# -- Samba share (media only, MONOLITHIC path) ------------------------------
echo "[8/9] Configuring Samba share (/opt/metixel/media as 'metixel-media')..."
SMB_CONF="/etc/samba/smb.conf"
if grep -q '^\[homes\]' "${SMB_CONF}" 2>/dev/null; then
    if ! grep -A10 '^\[homes\]' "${SMB_CONF}" | grep -q 'invalid users'; then
        sed -i '/^\[homes\]/a\   invalid users = nobody' "${SMB_CONF}"
    fi
fi
if ! grep -q 'load printers = no' "${SMB_CONF}" 2>/dev/null; then
    sed -i '/^\[global\]/a\   load printers = no' "${SMB_CONF}"
fi
if ! grep -q 'disable spoolss = yes' "${SMB_CONF}" 2>/dev/null; then
    sed -i '/^\[global\]/a\   disable spoolss = yes' "${SMB_CONF}"
fi

if ! grep -q '\[metixel-media\]' "${SMB_CONF}" 2>/dev/null; then
    tee -a "${SMB_CONF}" > /dev/null <<'SMBEOF'
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

echo -e "raspberry\nraspberry" | smbpasswd -a -s pi 2>/dev/null || true

systemctl enable smbd
systemctl restart smbd

# -- Boot config -------------------------------------------------------------
echo "[9/9] Configuring boot..."

BOOT_CONFIG="/boot/firmware/config.txt"
if [ -f "${BOOT_CONFIG}" ]; then
    GPU_MEM=128

    if ! grep -q "dtoverlay=vc4-kms-v3d" "${BOOT_CONFIG}"; then
        echo "" | tee -a "${BOOT_CONFIG}"
        echo "# Metixel Photoframe — KMS driver for GPU" | tee -a "${BOOT_CONFIG}"
        echo "dtoverlay=vc4-kms-v3d" | tee -a "${BOOT_CONFIG}"
        echo "gpu_mem=${GPU_MEM}" | tee -a "${BOOT_CONFIG}"
    fi

    if grep -q "^gpu_mem=" "${BOOT_CONFIG}"; then
        CURRENT_GPU_MEM=$(grep "^gpu_mem=" "${BOOT_CONFIG}" | head -1 | cut -d= -f2)
        if [ "${CURRENT_GPU_MEM}" -ne "${GPU_MEM}" ] 2>/dev/null; then
            echo "  Setting gpu_mem to ${GPU_MEM} (was ${CURRENT_GPU_MEM})"
            sed -i "s/^gpu_mem=.*/gpu_mem=${GPU_MEM}/" "${BOOT_CONFIG}"
        fi
    else
        echo "  Adding gpu_mem=${GPU_MEM}"
        echo "gpu_mem=${GPU_MEM}" | tee -a "${BOOT_CONFIG}"
    fi
fi

# -- I²C (ddcutil) -----------------------------------------------------------
# ddcutil talks DDC/CI to the monitor over the I²C bus.  The i2c-dev kernel
# module must be loaded.  Persist it via modules-load.d so it loads on every
# boot, and load it now so ddcutil works without a reboot.
echo "Configuring I²C (ddcutil)…"
echo "i2c-dev" > /etc/modules-load.d/metixel-i2c.conf
modprobe i2c-dev 2>/dev/null || true
echo "  + Enabled i2c-dev module (persistent via /etc/modules-load.d/metixel-i2c.conf)"

# ============================================================================
# SETUP COMPLETE — Reboot
# ============================================================================
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     Setup Complete! (LEGACY MONOLITHIC)                      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "After reboot, Metixel will auto-start (monolithic layout)."
echo "Access the dashboard at: http://<pi-ip-address>"
echo ""
echo "Rebooting in 10 seconds... (press Ctrl+C to cancel)"

for i in $(seq 10 -1 1); do
    echo -n "  $i... "
    sleep 1
done
echo ""

reboot