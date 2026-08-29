# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#!/bin/bash
# =============================================================================
# Metixel Photoframe — Trixie Setup Script (Self-Bootstrapping)
#
# Complete setup for a fresh Trixie Lite install on Raspberry Pi 2/3/Zero 2 W.
#
# ONE-STEP USAGE (download & run):
#   wget https://raw.githubusercontent.com/dennisadvani/metixel-photoframe/main/scripts/setup_trixie_metixel.sh
#   sudo bash setup_trixie_metixel.sh
#
# The script auto-detects if it's running standalone and will:
#   1. Install git
#   2. Clone the repository to /opt/metixel
#   3. Run the full setup from the cloned location
#   4. Reboot when complete
#
# If already inside the cloned repo, it runs the setup directly:
#   sudo bash /opt/metixel/scripts/setup_trixie_metixel.sh
# =============================================================================

set -euo pipefail

# -- Root check --------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)." >&2
    exit 1
fi

# ============================================================================
# PHASE 0: BOOTSTRAP — Detect if we're running standalone (outside the repo)
# ============================================================================
# If this script is NOT inside a cloned Metixel repo, we need to clone first.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
INSIDE_REPO=false

if [ -f "${REPO_ROOT}/pyproject.toml" ]; then
    INSIDE_REPO=true
    METIXEL_DIR="${REPO_ROOT}"
elif [ -f "/opt/metixel/pyproject.toml" ]; then
    INSIDE_REPO=true
    METIXEL_DIR="/opt/metixel"
fi

if [ "${INSIDE_REPO}" = false ]; then
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║     Metixel Photoframe — Setup                               ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    # ── Ask questions BEFORE touching the system ──────────────────
    # Nothing is installed or modified until the user confirms below.

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
    echo "  → Using ${RELEASE_CHANNEL} channel"
    echo ""

    echo "WiFi country code (e.g. AU, US, GB, DE, NZ):"
    echo "  This sets the regulatory domain for correct channel availability."
    read -p "  Country code [AU]: " WIFI_COUNTRY
    WIFI_COUNTRY="${WIFI_COUNTRY:-AU}"
    WIFI_COUNTRY=$(echo "$WIFI_COUNTRY" | tr '[:lower:]' '[:upper:]')
    echo "  → WiFi country: ${WIFI_COUNTRY}"
    echo ""

    echo "Ready to install. This will take 30–60 minutes."
    read -p "Press Enter to continue or Ctrl+C to cancel..."
    echo ""

    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║     Cloning repository and running full setup...             ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    # -- Install git if not present --
    if ! command -v git &>/dev/null; then
        echo "[bootstrap] Installing git..."
        apt-get update -qq
        apt-get install -y git
    else
        echo "[bootstrap] git already installed."
    fi

    # -- Prepare /opt/metixel --
    if [ -d "/opt/metixel/.git" ]; then
        echo "[bootstrap] Repository already exists at /opt/metixel — updating..."
        cd /opt/metixel
        git pull --ff-only || true
    elif [ -d "/opt/metixel" ] && [ "$(ls -A /opt/metixel 2>/dev/null)" ]; then
        echo "[bootstrap] /opt/metixel exists but is not a git repository."
        echo "[bootstrap] Moving existing content to /opt/metixel.bak..."
        mv /opt/metixel /opt/metixel.bak.$(date +%s)
        mkdir -p /opt/metixel
        echo "[bootstrap] Cloning Metixel Photoframe..."
        git clone https://github.com/dennisadvani/metixel-photoframe.git /opt/metixel
    else
        mkdir -p /opt/metixel
        echo "[bootstrap] Cloning Metixel Photoframe..."
        git clone https://github.com/dennisadvani/metixel-photoframe.git /opt/metixel
    fi

    echo "[bootstrap] Repository ready. Running full setup..."
    echo ""
    # Pass answers through environment variables so Phase 1 doesn't re-prompt
    exec env METIXEL_CHANNEL="${RELEASE_CHANNEL}" \
             METIXEL_WIFI_COUNTRY="${WIFI_COUNTRY}" \
        bash /opt/metixel/scripts/setup_trixie_metixel.sh
    # exec replaces this process — we never reach here
    exit 0
fi

# ============================================================================
# PHASE 1: MAIN SETUP (running from inside the cloned repo)
# ============================================================================

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     Metixel Photoframe — Trixie Setup                        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "Project root: ${METIXEL_DIR}"
echo "Target: Raspberry Pi 2/3/4/5 (Trixie)"
echo ""

# -- Release channel & WiFi country (from env or prompt) --------------------

# If passed via environment from Phase 0 bootstrap, use those values.
# Otherwise prompt (e.g. when running the script directly from the repo).
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

# Switch the repository to the correct version before installing anything.
# Both channels pin to the latest release tag on the main branch —
# stable uses non-prerelease tags (v1.0.0), beta uses pre-release tags
# (v1.0.4-beta.4).
cd "${METIXEL_DIR}"
git fetch origin --tags 2>/dev/null || true

# Ensure we're on main before looking for tags
git checkout main 2>/dev/null || true
git pull --ff-only 2>/dev/null || true

if [ "${RELEASE_CHANNEL}" = "stable" ]; then
    # Latest non-prerelease tag (e.g. v1.0.0) — excludes tags with "-"
    LATEST_TAG=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]' \
        | grep -v -- '-' \
        | sort -V \
        | tail -1)
else
    # Latest pre-release tag (e.g. v1.0.4-beta.4)
    LATEST_TAG=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]-*' \
        | sort -V \
        | tail -1)
fi

if [ -n "${LATEST_TAG}" ]; then
    git checkout "${LATEST_TAG}" 2>/dev/null || true
    echo "  → Pinned to ${LATEST_TAG} (${RELEASE_CHANNEL})"
else
    echo "  → No ${RELEASE_CHANNEL} tag found — staying on main branch HEAD"
fi

# -- System packages ---------------------------------------------------------
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

pip3 install ${PIP_IGNORE} pi3d 2>/dev/null || \
    pip3 install ${PIP_IGNORE} pi3d

pip3 install ${PIP_IGNORE} -r requirements-pip.txt 2>/dev/null || \
    pip3 install ${PIP_IGNORE} -r requirements-pip.txt

# -- Git safe.directory (OTA updates run as root via systemd-run) ------------
# Marks the canonical install location AND the release dir (added in step 4).
echo "[3b/9] Marking repository as safe for git..."
git config --system --add safe.directory /opt/metixel 2>/dev/null || true
git config --system --add safe.directory /opt/metixel/releases 2>/dev/null || true

# -- Directory structure (atomic Blue/Green layout) --------------------------
echo "[4/9] Creating directory structure (data / releases / live)..."
mkdir -p /opt/metixel/data/config /opt/metixel/data/logs /opt/metixel/data/media/sync/immich /opt/metixel/data/media/my_media /opt/metixel/data/cache /opt/metixel/data/backups /opt/metixel/releases /run/metixel

# Move the cloned app code into a versioned release folder, and put config in
# /data (persistent). The app runs from the live symlink.
# RELEASE_TAG is deterministic: prefer the nearest tag, else fall back to the
# branch name; strip any 'v' prefix and any -dirty/-g<hash> suffix so the
# release folder name is stable and matches what update.sh expects.
RELEASE_TAG="${LATEST_TAG:-$(git -C "${METIXEL_DIR}" describe --tags --abbrev=0 2>/dev/null || git -C "${METIXEL_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}"
RELEASE_TAG="${RELEASE_TAG#v}"
RELEASE_TAG="${RELEASE_TAG%%-*}"
RELEASE_TAG="v${RELEASE_TAG}"
RELEASE_DIR="/opt/metixel/releases/${RELEASE_TAG}"
mkdir -p "${RELEASE_DIR}"
# Move the app code into the release folder.  RELEASE_DIR lives INSIDE
# METIXEL_DIR (/opt/metixel/releases/<tag>), so we cannot `cp -a
# "${METIXEL_DIR}/." "${RELEASE_DIR}/"` — that would copy a directory into its
# own subdirectory and fail.  Instead move each top-level entry EXCEPT the
# dirs that belong to the data/releases layer (data, releases, live, cache,
# logs, media, etc), which are created/kept separately.
for entry in "${METIXEL_DIR}"/.* "${METIXEL_DIR}"/*; do
    name="$(basename "$entry")"
    case "$name" in
        "."|".."|"data"|"releases"|"live"|"cache"|"logs"|"media"|"etc")
            continue
            ;;
    esac
    if [ -e "${RELEASE_DIR}/${name}" ]; then
        continue
    fi
    mv "$entry" "${RELEASE_DIR}/" 2>/dev/null || true
done
# Keep the git checkout inside the release so future updates reference the repo.
if [ -d "${METIXEL_DIR}/.git" ] && [ ! -d "${RELEASE_DIR}/.git" ]; then
    mv "${METIXEL_DIR}/.git" "${RELEASE_DIR}/" 2>/dev/null || true
fi
# Recreate an empty 'etc' for any code-side default templates (config in /data).
mkdir -p "${METIXEL_DIR}/etc"

# Seed sample media into /data/media (persistent). The repo ships sample media
# under data/media/sample_media/ (tracked in git); copy it into the device's
# data/media so a fresh install has content to display. Never overwrite an
# existing sample_media (user may have replaced it).
if [ -d "${METIXEL_DIR}/data/media/sample_media" ]; then
    mkdir -p /opt/metixel/data/media/sample_media
    cp -n "${METIXEL_DIR}"/data/media/sample_media/* /opt/metixel/data/media/sample_media/ 2>/dev/null || true
fi

# Persist config.json + logging.conf into /data (user-editable). __main__.py
# resolves logging.conf as data_dir()/etc/logging.conf, so it lives at
# /opt/metixel/data/etc/logging.conf alongside config.json.
mkdir -p /opt/metixel/data/etc
# etc/ stays at METIXEL_DIR (excluded from the release move) — read the
# default templates from there.
cp "${METIXEL_DIR}/etc/config.example.json" /opt/metixel/data/config.json 2>/dev/null || true
cp -n "${METIXEL_DIR}/etc/logging.conf" /opt/metixel/data/etc/logging.conf 2>/dev/null || true
chown -R pi:pi "${RELEASE_DIR}" /opt/metixel/data /run/metixel 2>/dev/null || true

# Create the live symlink → active release.
ln -sfn "${RELEASE_DIR}" /opt/metixel/live
chown -h pi:pi /opt/metixel/live 2>/dev/null || true
git config --system --add safe.directory "${RELEASE_DIR}" 2>/dev/null || true

# -- systemd services --------------------------------------------------------
echo "[5/9] Installing systemd services..."
# The app code (including systemd/) now lives in the release dir.
cp "${RELEASE_DIR}/systemd/metixel-backend.service" /etc/systemd/system/
cp "${RELEASE_DIR}/systemd/metixel-cage.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable metixel-backend
systemctl enable metixel-cage

# Enable linger for the pi user so systemd-logind creates
# /run/user/1000 at boot even without a user login.
# Required for cage (Wayland compositor) to start on headless boots.
loginctl enable-linger pi

# -- Enable Wi-Fi -----------------------------------------------------------
echo "[6/9] Enabling Wi-Fi..."
# Raspberry Pi Imager disables WiFi at the OS level if you skip Wi-Fi
# configuration during imaging.  Re-enable it before configuring hostapd
# so the wireless interface is available for the captive portal.
rfkill unblock wifi 2>/dev/null || true
rfkill unblock wlan 2>/dev/null || true
# Also ensure NetworkManager doesn't treat wlan0 as unmanaged
if command -v nmcli &>/dev/null; then
    nmcli radio wifi on 2>/dev/null || true
fi

# Disable Wi-Fi power management — Pi 3 WiFi is flakey with power saving
# enabled (failed beacons, missed connections).  NetworkManager's default
# is to enable powersave, which causes the captive portal AP to be
# unreliable and client connections to drop.
echo "     Disabling Wi-Fi power management..."
mkdir -p /etc/NetworkManager/conf.d
tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf > /dev/null <<'NMPOWEREOF'
[connection]
wifi.powersave = 2
NMPOWEREOF

# Apply WiFi regulatory domain — prompted upfront, applied here
if command -v iw &>/dev/null; then
    iw reg set "$WIFI_COUNTRY" 2>/dev/null || true
    echo "     WiFi regulatory domain set to: $WIFI_COUNTRY"
fi
# Also set via cfg80211 module parameter for persistence across reboots
if [ -f /etc/modprobe.d/cfg80211.conf ]; then
    sed -i "s/^options cfg80211 ieee80211_regdom=.*/options cfg80211 ieee80211_regdom=$WIFI_COUNTRY/" /etc/modprobe.d/cfg80211.conf 2>/dev/null || true
else
    echo "options cfg80211 ieee80211_regdom=$WIFI_COUNTRY" > /etc/modprobe.d/cfg80211.conf
fi
# Write to Metixel config so the Web UI reflects it
python3 -c "
import json, os
cfg_path = '/opt/metixel/data/config.json'
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
# Configure hostapd (open network "Metixel-Setup")
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
# Replace the entire file with a clean version to avoid quoting issues
tee /etc/default/hostapd > /dev/null <<'HOSTAPDDEF'
# Defaults for hostapd — managed by Metixel Photoframe
DAEMON_CONF=/etc/hostapd/hostapd.conf
DAEMON_OPTS=
HOSTAPDDEF

# Configure dnsmasq (DHCP + captive DNS)
tee /etc/dnsmasq.conf > /dev/null <<'DNSMASQEOF'
interface=wlan0
dhcp-range=192.168.42.10,192.168.42.100,12h
dhcp-option=3,192.168.42.1
dhcp-option=6,192.168.42.1
address=/#/192.168.42.1
no-resolv
DNSMASQEOF

# Disable auto-start — the Metixel NetworkMonitor controls these services.
# If disable fails, log it but don't stop the install — the service may
# not be fully registered yet (first install).  The NetworkMonitor
# explicitly stops hostapd before taking control.
if ! systemctl disable hostapd dnsmasq; then
    echo "WARNING: Could not disable hostapd/dnsmasq auto-start — may need manual fix"
fi
systemctl unmask hostapd dnsmasq 2>/dev/null || true

# -- Samba share (media only) ------------------------------------------------
# Only shares /opt/metixel/data/media so users can add/remove photos/videos.
# For full-project access during development, run setup_trixie_dev_env.sh
# which adds a separate [metixel] share pointing to /opt/metixel.
echo "[8/9] Configuring Samba share (/opt/metixel/data/media as 'metixel-media')..."

# Add 'invalid users = nobody' to the [homes] section so the system
# 'nobody' user doesn't get an auto-share (don't comment out [homes]
# itself — orphaned lines will corrupt smb.conf).
# Also disable printer sharing in [global] (don't comment out [printers]
# / [print$] headers for the same reason).
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

# Append share definition to smb.conf if not already present
if ! grep -q '\[metixel-media\]' "${SMB_CONF}" 2>/dev/null; then
    tee -a "${SMB_CONF}" > /dev/null <<'SMBEOF'
[metixel-media]
   comment = Metixel Photoframe Media Share
   path = /opt/metixel/data/media
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
echo -e "raspberry\nraspberry" | smbpasswd -a -s pi 2>/dev/null || true

systemctl enable smbd
systemctl restart smbd

# -- Boot config -------------------------------------------------------------
echo "[9/9] Configuring boot..."

BOOT_CONFIG="/boot/firmware/config.txt"
if [ -f "${BOOT_CONFIG}" ]; then
    # ── GPU memory: 128 MB for all Pi models.
    # Pi 2/3/Zero2W need a static GPU partition — 128 MB provides room
    # for the KMS framebuffer (~8 MB) plus ~30 pi3d textures at 1080p
    # RGB565 (~4 MB each) with fragmentation headroom.
    # Pi 4/5 use CMA dynamic allocation and ignore gpu_mem entirely, so
    # setting it to 128 is harmless on those models.  A single value
    # avoids model-detection complexity and keeps the base image portable.
    GPU_MEM=128

    # Ensure KMS overlay is enabled
    if ! grep -q "dtoverlay=vc4-kms-v3d" "${BOOT_CONFIG}"; then
        echo "" | tee -a "${BOOT_CONFIG}"
        echo "# Metixel Photoframe — KMS driver for GPU" | tee -a "${BOOT_CONFIG}"
        echo "dtoverlay=vc4-kms-v3d" | tee -a "${BOOT_CONFIG}"
        echo "gpu_mem=${GPU_MEM}" | tee -a "${BOOT_CONFIG}"
    fi

    # If KMS overlay is already present, still ensure gpu_mem is set.
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

# ============================================================================
# SETUP COMPLETE — Reboot
# ============================================================================
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     Setup Complete!                                          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "After reboot, Metixel will auto-start."
echo "Access the dashboard at: http://<pi-ip-address>"
echo ""
echo "Rebooting in 10 seconds... (press Ctrl+C to cancel)"

for i in $(seq 10 -1 1); do
    echo -n "  $i... "
    sleep 1
done
echo ""

reboot
