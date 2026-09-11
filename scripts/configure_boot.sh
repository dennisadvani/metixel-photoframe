#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# Metixel Photoframe — boot configuration (/boot/firmware/config.txt).
#
# WHY THIS IS NOT IN reconcile.sh
# ------------------------------
# reconcile.sh owns CONVERGENT host state — configuration that is safe to
# re-assert on every single update, because the desired end state is derived
# from the repo (systemd units, module load lists, directory ownership).
#
# Boot configuration is different, and deliberately excluded:
#
#   1. `config.txt` is the DEVICE's file, not Metixel's.  A user may have set a
#      deliberate `gpu_mem` value for their own reasons; re-asserting 128 on
#      every update would silently override that choice, forever.
#   2. Changes only take effect after a REBOOT.  An update that silently edits
#      boot config schedules a behaviour change that manifests later, detached
#      from the action that caused it — the hardest kind of change to diagnose.
#
# So boot config is applied at exactly two lifecycle points:
#
#   * provisioning (scripts/update.sh on a fresh install) — a fresh device has
#     no user customisation to preserve.
#   * a ONE-TIME fixup (scripts/fixups/v1.2.1-gpu-mem.sh) — repairs devices
#     that predate the current value, exactly once, never again.
#
# Keeping the logic here means those two callers share ONE implementation
# instead of drifting copies.  It is intentionally NOT added to
# reconcile.sh's convergent set.
#
# Usage: sudo bash scripts/configure_boot.sh [--dry-run]

set -uo pipefail

BOOT="/boot/firmware/config.txt"

# GPU memory: 128 MB for all Pi models.
# Pi 2/3/Zero 2 W need a static GPU partition — 128 MB provides room for the
# KMS framebuffer (~8 MB) plus pi3d textures at 1080p RGB565 with fragmentation
# headroom.  Pi 4/5 use CMA dynamic allocation and ignore gpu_mem, so setting
# 128 is harmless there.  A single value avoids model detection and keeps the
# base image portable.
GPU_MEM=128

DRY_RUN="no"
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY_RUN="yes" ;;
        -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown argument: ${arg}" >&2; exit 1 ;;
    esac
done

if [ "$(id -u)" -ne 0 ] && [ "${DRY_RUN}" = "no" ]; then
    echo "ERROR: this script must run as root (use sudo)" >&2
    exit 1
fi

if [ ! -f "${BOOT}" ]; then
    echo "  ! ${BOOT} not found — skipping boot configuration"
    exit 0
fi

CHANGED="no"

# ── KMS overlay ────────────────────────────────────────────────────────────
# Required for any rendering on Trixie: without vc4-kms-v3d there is no
# KMS/DRM, so cage and pi3d have no display path.  Added only when absent.
if grep -q 'dtoverlay=vc4-kms-v3d' "${BOOT}" 2>/dev/null; then
    echo "  = dtoverlay=vc4-kms-v3d already present"
else
    if [ "${DRY_RUN}" = "yes" ]; then
        echo "      [dry-run] add dtoverlay=vc4-kms-v3d"
    else
        printf '\n# Metixel Photoframe — KMS driver for GPU\ndtoverlay=vc4-kms-v3d\n' >> "${BOOT}"
        echo "  + added dtoverlay=vc4-kms-v3d"
    fi
    CHANGED="yes"
fi

# ── GPU memory ─────────────────────────────────────────────────────────────
# Correct only when there is not exactly one gpu_mem=128.  Duplicates are a bug
# because the LAST line wins in config.txt, so any duplicate is collapsed.
COUNT="$(grep -c '^gpu_mem=' "${BOOT}" 2>/dev/null || true)"
HAS_VALUE="$(grep -c "^gpu_mem=${GPU_MEM}$" "${BOOT}" 2>/dev/null || true)"

if [ "${COUNT}" = "1" ] && [ "${HAS_VALUE}" = "1" ]; then
    echo "  = gpu_mem=${GPU_MEM} already set"
else
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] set gpu_mem=%s (found %s line(s))\n' "${GPU_MEM}" "${COUNT}"
    else
        sed -i '/^gpu_mem=/d' "${BOOT}"
        printf 'gpu_mem=%s\n' "${GPU_MEM}" >> "${BOOT}"
        echo "  + set gpu_mem=${GPU_MEM} (removed ${COUNT} old line(s))"
    fi
    CHANGED="yes"
fi

if [ "${CHANGED}" = "yes" ] && [ "${DRY_RUN}" = "no" ]; then
    echo "REBOOT_REQUIRED: boot config changed; reboot to apply"
fi
exit 0
