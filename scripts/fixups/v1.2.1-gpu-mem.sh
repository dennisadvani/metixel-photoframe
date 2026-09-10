#!/usr/bin/env bash
#
# Metixel fixup — correct the GPU memory setting on Pi 2/3/Zero 2 W.
#
# Older installs may have an incorrect gpu_mem= in /boot/firmware/config.txt
# (e.g. 16 from a generic image).  Metixel needs 128 MB for the KMS framebuffer
# plus pi3d textures; this fixup repairs devices that predate that value.
#
# WHY THIS IS STILL A FIXUP AND NOT PART OF reconcile.sh
# -----------------------------------------------------
# config.txt is the DEVICE's file and a gpu_mem change only takes effect after
# a reboot.  Re-asserting 128 on every update would silently override a value a
# user deliberately chose, and would schedule a reboot-dependent change on an
# otherwise unrelated upgrade.  A fixup runs exactly ONCE per device, which is
# the correct semantic for this repair.
#
# The actual edit lives in scripts/configure_boot.sh so this fixup and the
# fresh-install path share one implementation instead of drifting copies.
#
# Idempotent: safe to re-run (tracked exactly-once in installed_fixups.json).
set -euo pipefail

# Root of the release whose fixup manifest is running (…/releases/<version>).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIGURE_BOOT="${REPO}/scripts/configure_boot.sh"

if [ ! -f "${CONFIGURE_BOOT}" ]; then
    # Never fail the update over a cosmetic repair — warn-and-continue.
    echo "WARNING: ${CONFIGURE_BOOT} not found — cannot repair gpu_mem"
    exit 0
fi

# configure_boot.sh is idempotent and prints its own REBOOT_REQUIRED line.
bash "${CONFIGURE_BOOT}"

exit 0
