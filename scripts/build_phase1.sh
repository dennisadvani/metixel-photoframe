# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#!/bin/bash
# =============================================================================
# Metixel Photoframe — Phase 1 Build Script
# Builds a Trixie Lite image for Raspberry Pi 2/3/Zero 2 W.
#
# Prerequisites:
#   - Debian/Ubuntu host with qemu-user-static, debootstrap, and git
#   - Root privileges (for debootstrap and loopback mounts)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="${PROJECT_ROOT}/build/phase1"
IMAGE_NAME="metixel-photoframe-phase1"
IMAGE_SIZE_MB=2048  # 2GB image

echo "=== Metixel Photoframe Phase 1 Build ==="
echo "Target: Raspberry Pi 2/3/Zero 2 W (Trixie)"
echo ""

# ---------------------------------------------------------------------------
# 1. Create build directory
# ---------------------------------------------------------------------------
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

# ---------------------------------------------------------------------------
# 2. Create empty image
# ---------------------------------------------------------------------------
echo "[1/6] Creating ${IMAGE_SIZE_MB}MB image..."
dd if=/dev/zero of="${IMAGE_NAME}.img" bs=1M count="${IMAGE_SIZE_MB}" status=progress

# ---------------------------------------------------------------------------
# 3. Partition image
# ---------------------------------------------------------------------------
echo "[2/6] Partitioning image..."
# Create two partitions:
#   p1: 256MB FAT32 (/boot/firmware on Trixie)
#   p2: rest ext4 (/)
sfdisk "${IMAGE_NAME}.img" <<EOF
label: dos
size=256M, type=c
type=83
EOF

# ---------------------------------------------------------------------------
# 4. Mount loopback and debootstrap
# ---------------------------------------------------------------------------
echo "[3/6] Setting up loopback device..."
LOOP_DEV=$(sudo losetup -Pf --show "${IMAGE_NAME}.img")
BOOT_PART="${LOOP_DEV}p1"
ROOT_PART="${LOOP_DEV}p2"

echo "Loop device: ${LOOP_DEV}"
echo "Boot partition: ${BOOT_PART}"
echo "Root partition: ${ROOT_PART}"

# Format partitions
sudo mkfs.vfat -F 32 "${BOOT_PART}"
sudo mkfs.ext4 -F "${ROOT_PART}"

# Mount
MOUNT_DIR="${BUILD_DIR}/mnt"
sudo mkdir -p "${MOUNT_DIR}"
sudo mount "${ROOT_PART}" "${MOUNT_DIR}"
sudo mkdir -p "${MOUNT_DIR}/boot/firmware"
sudo mount "${BOOT_PART}" "${MOUNT_DIR}/boot/firmware"

# ---------------------------------------------------------------------------
# 5. Debootstrap Trixie
# ---------------------------------------------------------------------------
echo "[4/6] Bootstrapping Debian Trixie (armhf)..."
# Note: In production, you'd run debootstrap or use the RPi imager.
# This script demonstrates the structure — actual debootstrap requires
# QEMU user mode for cross-architecture builds.
echo "  (debootstrap step — requires qemu-user-static for cross-arch builds)"
echo "  sudo debootstrap --arch=armhf trixie ${MOUNT_DIR} http://deb.debian.org/debian/"

# ---------------------------------------------------------------------------
# 6. Configure the OS
# ---------------------------------------------------------------------------
echo "[5/6] Configuring OS..."

# Copy config.txt for silent boot
if [ -f "${PROJECT_ROOT}/scripts/quiet_boot.sh" ]; then
    echo "  Running quiet boot configuration..."
    sudo bash "${PROJECT_ROOT}/scripts/quiet_boot.sh" "${MOUNT_DIR}"
fi

# Install Metixel Photoframe — atomic Blue/Green layout.
# NOTE: the prebuilt image is a flat (non-git) copy into a versioned release
# folder + live symlink. It is NOT OTA-updatable via git (no .git) — a fresh
# clone via setup_trixie_metixel.sh is required for OTA updates.
echo "  Installing Metixel Photoframe application..."
APP_VERSION="$(grep -E '^__version__' "${PROJECT_ROOT}/src/metixel/__init__.py" 2>/dev/null | sed -E 's/.*"([^"]+)".*/\1/')"
APP_VERSION="${APP_VERSION:-v1.0.0}"
RELEASE_DIR="${MOUNT_DIR}/opt/metixel/releases/v${APP_VERSION#v}"
LIVE_DIR="${MOUNT_DIR}/opt/metixel/live"

sudo mkdir -p "${MOUNT_DIR}/opt/metixel/data" \
    "${MOUNT_DIR}/opt/metixel/data/config" \
    "${MOUNT_DIR}/opt/metixel/data/logs" \
    "${MOUNT_DIR}/opt/metixel/data/media" \
    "${MOUNT_DIR}/opt/metixel/data/cache" \
    "${MOUNT_DIR}/opt/metixel/releases"
sudo mkdir -p "${RELEASE_DIR}"

# Copy application code into the versioned release folder.
sudo cp -r "${PROJECT_ROOT}/src/metixel" "${RELEASE_DIR}/src/"
sudo cp -r "${PROJECT_ROOT}/etc" "${RELEASE_DIR}/etc/"
sudo cp "${PROJECT_ROOT}/requirements-pip.txt" "${RELEASE_DIR}/"
sudo cp -r "${PROJECT_ROOT}/scripts" "${RELEASE_DIR}/scripts/"

# Seed the persistent config from the example (user edits live in /data).
sudo cp "${RELEASE_DIR}/etc/config.example.json" "${MOUNT_DIR}/opt/metixel/data/config.json"

# Seed sample media into /data/media (persistent) so a fresh image has content.
if [ -d "${PROJECT_ROOT}/data/media/sample_media" ]; then
    sudo mkdir -p "${MOUNT_DIR}/opt/metixel/data/media/sample_media"
    sudo cp -n "${PROJECT_ROOT}"/data/media/sample_media/* \
        "${MOUNT_DIR}/opt/metixel/data/media/sample_media/" 2>/dev/null || true
fi

# Create the live symlink → active release.
sudo ln -sfn "/opt/metixel/releases/v${APP_VERSION#v}" "${LIVE_DIR}"

# Copy systemd units
sudo cp "${PROJECT_ROOT}/systemd/metixel-backend.service" "${MOUNT_DIR}/etc/systemd/system/"
sudo cp "${PROJECT_ROOT}/systemd/metixel-cage.service" "${MOUNT_DIR}/etc/systemd/system/"
sudo cp "${PROJECT_ROOT}/systemd/metixel-cursor-hider.service" "${MOUNT_DIR}/etc/systemd/system/"

# Enable services
sudo ln -sf /etc/systemd/system/metixel-backend.service \
    "${MOUNT_DIR}/etc/systemd/system/multi-user.target.wants/metixel-backend.service" 2>/dev/null || true
sudo ln -sf /etc/systemd/system/metixel-cage.service \
    "${MOUNT_DIR}/etc/systemd/system/multi-user.target.wants/metixel-cage.service" 2>/dev/null || true
sudo ln -sf /etc/systemd/system/metixel-cursor-hider.service \
    "${MOUNT_DIR}/etc/systemd/system/multi-user.target.wants/metixel-cursor-hider.service" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 7. Cleanup and finalize
# ---------------------------------------------------------------------------
echo "[6/6] Cleaning up..."
sudo umount "${MOUNT_DIR}/boot/firmware" || true
sudo umount "${MOUNT_DIR}" || true
sudo losetup -d "${LOOP_DEV}" || true

echo ""
echo "=== Build Complete ==="
echo "Image: ${BUILD_DIR}/${IMAGE_NAME}.img"
echo ""
echo "Write to SD card:"
echo "  sudo dd if=${IMAGE_NAME}.img of=/dev/sdX bs=4M status=progress conv=fsync"
