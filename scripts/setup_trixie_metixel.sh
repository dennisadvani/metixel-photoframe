# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#!/bin/bash
# =============================================================================
# Metixel Photoframe - Trixie setup (run from inside a checkout)
#
# Complete setup for a fresh Trixie Lite install on Raspberry Pi 2/3/Zero 2 W.
#
# USAGE
#   git clone https://github.com/dennisadvani/metixel-photoframe.git /opt/metixel
#   cd /opt/metixel
#   sudo bash scripts/setup_trixie_metixel.sh
#
# This script has NO self-bootstrap phase on purpose.  It used to be
# downloadable from main and clone the repository itself, which meant the
# installer had to be committed and promoted to main before it could be
# tested - so the version a user ran and the version being worked on could
# differ.  It now operates on the checkout it lives in, so whatever branch you
# cloned is exactly what you are testing.
#
# The one-line wget-pipe-bash convenience install was removed with it.  For an
# unattended install, clone first and run this script with METIXEL_CHANNEL and
# METIXEL_WIFI_COUNTRY set to skip the interactive prompts.
# =============================================================================

set -euo pipefail

# -- Root check --------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)." >&2
    exit 1
fi

# -- Locate the checkout -----------------------------------------------------
# The script installs the very code it lives in, so it must run from inside a
# cloned repository: later steps reference sibling files (systemd/, other
# scripts/, requirements*.txt) relative to this root.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"

if [ -f "${REPO_ROOT}/pyproject.toml" ]; then
    METIXEL_DIR="${REPO_ROOT}"
elif [ -f "/opt/metixel/pyproject.toml" ] \
     && [ -f "/opt/metixel/scripts/setup_trixie_metixel.sh" ]; then
    # Invoked from a copy outside the checkout (e.g. a synced temp file) while a
    # checkout already exists at the canonical location.
    METIXEL_DIR="/opt/metixel"
else
    echo "ERROR: this script must be run from inside a Metixel checkout." >&2
    echo "" >&2
    echo "  git clone https://github.com/dennisadvani/metixel-photoframe.git /opt/metixel" >&2
    echo "  cd /opt/metixel" >&2
    echo "  sudo bash scripts/setup_trixie_metixel.sh" >&2
    echo "" >&2
    echo "Refusing to run: it would install a checkout that does not exist." >&2
    exit 1
fi

# ============================================================================
# MAIN SETUP
# ============================================================================


echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     Metixel Photoframe — Trixie Setup                        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "Project root: ${METIXEL_DIR}"
echo "Target: Raspberry Pi 2/3/4/5 (Trixie)"
echo ""

# -- Release channel & WiFi country (from env or prompt) --------------------

# Values may come from the environment (METIXEL_CHANNEL /
# METIXEL_WIFI_COUNTRY) for unattended installs; otherwise prompt.
RELEASE_CHANNEL="${METIXEL_CHANNEL:-}"
WIFI_COUNTRY="${METIXEL_WIFI_COUNTRY:-}"

if [ -z "${RELEASE_CHANNEL}" ]; then
    echo "Release channel:"
    echo "  stable = Latest stable release (recommended)"
    echo "  beta   = Pre-release with latest features"
    echo "  dev    = Development branch (latest commits, unstable)"
    read -p "  Channel [stable]: " RELEASE_CHANNEL
    RELEASE_CHANNEL="${RELEASE_CHANNEL:-stable}"
    case "$RELEASE_CHANNEL" in
        stable|beta|dev) ;;
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
# stable / beta pin to the latest release tag on the main branch —
# stable uses non-prerelease tags (v1.0.0), beta uses pre-release tags
# (v1.0.4-beta.4).  dev tracks the latest dev branch HEAD (unstable).
cd "${METIXEL_DIR}"
git fetch origin --tags 2>/dev/null || true
git fetch origin dev:dev 2>/dev/null || true

# Ensure we're on main before looking for tags
git checkout main 2>/dev/null || true
git pull --ff-only 2>/dev/null || true

if [ "${RELEASE_CHANNEL}" = "stable" ]; then
    # Latest non-prerelease tag (e.g. v1.0.0) — excludes tags with "-"
    LATEST_TAG=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]' \
        | grep -v -- '-' \
        | sort -V \
        | tail -1)
elif [ "${RELEASE_CHANNEL}" = "beta" ]; then
    # Latest pre-release tag (e.g. v1.0.4-beta.4)
    LATEST_TAG=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]-*' \
        | sort -V \
        | tail -1)
else
    # dev channel: track the latest dev branch HEAD (no tag)
    LATEST_TAG=""
fi

if [ -n "${LATEST_TAG}" ]; then
    git checkout "${LATEST_TAG}" 2>/dev/null || true
    echo "  → Pinned to ${LATEST_TAG} (${RELEASE_CHANNEL})"
elif [ "${RELEASE_CHANNEL}" = "dev" ]; then
    # Switch to the dev branch deterministically.  `git checkout dev` can
    # silently fail (and, with `|| true`, leave the repo on main) when no
    # local dev branch tracks origin/dev yet — so use --track explicitly.
    git checkout --track origin/dev 2>/dev/null \
        || git checkout dev 2>/dev/null \
        || git checkout -B dev origin/dev
    git pull --ff-only 2>/dev/null || true
    echo "  → Using dev branch HEAD (${RELEASE_CHANNEL})"
else
    echo "  → No ${RELEASE_CHANNEL} tag found — staying on main branch HEAD"
fi

# -- System packages ---------------------------------------------------------
# -- System packages ---------------------------------------------------------
echo "[1/7] Updating package lists..."
apt-get update -qq

echo "[2/7] Installing system packages..."
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3-pip \
    python3-pil \
    python3-numpy \
    python3-libcamera \
    libopenblas0 \
    cec-utils \
    libcec-dev \
    ddcutil \
    i2c-tools \
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
echo "[3/7] Installing Python packages..."
cd "${METIXEL_DIR}"

# Use --ignore-installed to skip packages already provided by apt
# (python3-numpy, python3-pil). This avoids "Cannot uninstall" errors
# with PEP 668 externally-managed environments.
PIP_IGNORE="--break-system-packages --ignore-installed"

pip3 install ${PIP_IGNORE} pi3d 2>/dev/null || \
    pip3 install ${PIP_IGNORE} pi3d

pip3 install ${PIP_IGNORE} -r requirements-pip.txt 2>/dev/null || \
    pip3 install ${PIP_IGNORE} -r requirements-pip.txt

# Dev & testing tools (pytest, pytest-cov, ruff, mypy) — installed as part of
# the base install so no separate dev-env script is needed. These mirror the
# [dev] extra in pyproject.toml.
pip3 install ${PIP_IGNORE} ruff mypy pytest pytest-cov 2>/dev/null || \
    pip3 install ${PIP_IGNORE} ruff mypy pytest pytest-cov

# -- Git safe.directory (OTA updates run as root via systemd-run) ------------
# Marks the canonical install location AND the release dir (added in step 4).
echo "[3b/7] Marking repository as safe for git..."
git config --system --add safe.directory /opt/metixel 2>/dev/null || true
git config --system --add safe.directory /opt/metixel/releases 2>/dev/null || true

# -- Directory structure (atomic Blue/Green layout) --------------------------
# The DATA tree (data/logs, data/media, data/cache, data/etc …) is owned by
# scripts/reconcile.sh — the single source of truth for it — which is invoked
# near the end of setup.  Only the layout directories that reconcile.sh does
# not know about are created here.
echo "[4/7] Creating directory structure (releases / live)..."
mkdir -p /opt/metixel/releases /run/metixel

# Move the cloned app code into a versioned release folder, and put config in
# /data (persistent). The app runs from the live symlink.
# RELEASE_TAG is deterministic: prefer the nearest tag, else fall back to the
# branch name; strip any 'v' prefix and any -dirty/-g<hash> suffix so the
# release folder name is stable and matches what update.sh expects.
if [ "${RELEASE_CHANNEL}" = "dev" ]; then
    # Dev installs must NOT collide with a tagged release: the release dir
    # separates installs by version, and a dev checkout whose nearest reachable
    # tag matches an already-installed release (e.g. v1.2.3) would have its
    # systemd/ (and src/, scripts/) silently skipped by the move loop below —
    # it never overwrites an existing directory.  Use a branch-derived
    # non-tag name so the dev code — including systemd/ — is always fresh.
    RELEASE_BRANCH="$(git -C "${METIXEL_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
    RELEASE_TAG="dev-${RELEASE_BRANCH}"
else
    RELEASE_TAG="${LATEST_TAG:-$(git -C "${METIXEL_DIR}" describe --tags --abbrev=0 2>/dev/null || git -C "${METIXEL_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}"
    RELEASE_TAG="${RELEASE_TAG#v}"
    RELEASE_TAG="${RELEASE_TAG%%-*}"
    RELEASE_TAG="v${RELEASE_TAG}"
fi
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
    # Copy the landscape/portrait subfolders recursively (never overwrite
    # existing sample_media the user may have replaced).
    cp -rn "${METIXEL_DIR}"/data/media/sample_media/. /opt/metixel/data/media/sample_media/ 2>/dev/null || true
fi

# logging.conf is the one config file the application does NOT create, and it is
# a documented user-editable surface (data/etc/logging.conf), so it is still
# seeded here.  Never overwrite an existing one.
mkdir -p /opt/metixel/data/etc
cp -n "${METIXEL_DIR}/etc/logging.conf" /opt/metixel/data/etc/logging.conf 2>/dev/null || true

# Config is NOT created here.  The application owns the config schema and
# creates data/config.json from its own DEFAULT_CONFIG on first start (see
# Config.load), which also randomises the auto-update schedule.
#
# Installer answers are written to data/init.json instead — a partial overlay
# using the same schema as config.json.  The application merges and consumes it
# on first start (renaming it to init.json.applied), and reconcile.sh reads it
# for host state (e.g. the WiFi regulatory domain) BEFORE the app has run.
# This keeps the config schema in one place (Python) instead of duplicating it
# in shell, and makes the flow order-independent.
echo ""
echo "Writing provisioning answers to /opt/metixel/data/init.json..."
python3 -c "
import json, os
path = '/opt/metixel/data/init.json'
overlay = {
    'network': {'wifi_country': '${WIFI_COUNTRY}'},
    'update': {'channel': '${RELEASE_CHANNEL}'},
}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w') as f:
    json.dump(overlay, f, indent=2)
print('  -> wrote', path)
" 2>/dev/null || echo "  ! could not write init.json (non-fatal)"

chown -R pi:pi "${RELEASE_DIR}" /opt/metixel/data /run/metixel 2>/dev/null || true
chown pi:pi /opt/metixel/data/init.json 2>/dev/null || true

# Create the live symlink → active release.
ln -sfn "${RELEASE_DIR}" /opt/metixel/live
chown -h pi:pi /opt/metixel/live 2>/dev/null || true
git config --system --add safe.directory "${RELEASE_DIR}" 2>/dev/null || true

# -- systemd services --------------------------------------------------------
echo "[5/7] Installing systemd services..."
# The app code (including systemd/) now lives in the release dir.
cp "${RELEASE_DIR}/systemd/metixel-backend.service" /etc/systemd/system/
cp "${RELEASE_DIR}/systemd/metixel-cage.service" /etc/systemd/system/
# The cursor-hider mode is a newer feature; only install its service if the
# release actually ships it (older releases lack the mode by design, and a
# unit that execs --mode cursor-hider on old code would crash-loop forever).
if [ -f "${RELEASE_DIR}/systemd/metixel-cursor-hider.service" ] \
   && grep -q "cursor-hider" "${RELEASE_DIR}/src/metixel/__main__.py"; then
    cp "${RELEASE_DIR}/systemd/metixel-cursor-hider.service" /etc/systemd/system/
    CURSOR_HIDER_PRESENT=true
else
    echo "  ! Release does not support cursor-hider — skipping its service"
    rm -f /etc/systemd/system/metixel-cursor-hider.service 2>/dev/null || true
    CURSOR_HIDER_PRESENT=false
fi
systemctl daemon-reload
systemctl enable metixel-backend
systemctl enable metixel-cage
if [ "${CURSOR_HIDER_PRESENT}" = true ]; then
    systemctl enable metixel-cursor-hider
fi

# -- Enable Wi-Fi -----------------------------------------------------------
echo "[6/7] Enabling Wi-Fi..."
# Raspberry Pi Imager disables WiFi at the OS level if you skip Wi-Fi
# configuration during imaging.  Re-enable it before configuring hostapd
# so the wireless interface is available for the captive portal.
rfkill unblock wifi 2>/dev/null || true
rfkill unblock wlan 2>/dev/null || true
# Also ensure NetworkManager doesn't treat wlan0 as unmanaged
if command -v nmcli &>/dev/null; then
    nmcli radio wifi on 2>/dev/null || true
fi

# Apply WiFi regulatory domain.
# The value is recorded in init.json (above, as network.wifi_country) and
# reconcile.sh (step 7) applies it from there — reading the config rather than
# being told the value keeps one code path for install and OTA.  `iw reg set`
# here only avoids waiting for the reboot that ends this script.
if command -v iw &>/dev/null; then
    iw reg set "$WIFI_COUNTRY" 2>/dev/null || true
    echo "     WiFi regulatory domain set to: $WIFI_COUNTRY"
fi

# The Samba share definition, service enablement and the pi account are all
# owned by reconcile.sh (step 7) — see that script for the single definition.

# -- Host configuration (I²C, ddcutil, networking, data tree, boot config) ---
# scripts/reconcile.sh is the SINGLE owner of Metixel-managed host state.  It
# is the same script the OTA updater runs, so a fresh install and an upgraded
# device converge to exactly the same host — no duplicated lists to drift.
echo "[7/7] Reconciling host configuration..."
bash "${METIXEL_DIR}/scripts/reconcile.sh"

# Boot configuration is NOT part of reconciliation (config.txt is the device's
# file and changes need a reboot), so it is applied explicitly here.  The same
# script is invoked by the one-time v1.2.1-gpu-mem.sh fixup, so both paths share
# one implementation.  Guarded on the Raspberry Pi boot config existing.
if [ -d /boot/firmware ]; then
    bash "${METIXEL_DIR}/scripts/configure_boot.sh"
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
