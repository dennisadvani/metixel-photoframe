#!/usr/bin/env bash
#
# Metixel fixup — correct the GPU memory setting on Pi 2/3/Zero2W.
#
# Older installs may have an incorrect gpu_mem= in /boot/firmware/config.txt
# (e.g. 16 from a generic image). The setup script sets gpu_mem=128 for
# hardware video decode; this fixup repairs devices that predate that.
#
# Idempotent: only touches the file if gpu_mem is present and not already 128.
# Prints REBOOT_REQUIRED if it changes anything (a reboot is needed to apply).
set -euo pipefail

BOOT="/boot/firmware/config.txt"
[ -f "${BOOT}" ] || exit 0

if grep -q '^gpu_mem=' "${BOOT}" && ! grep -q '^gpu_mem=128' "${BOOT}"; then
    sed -i 's/^gpu_mem=.*/gpu_mem=128/' "${BOOT}"
    echo "gpu_mem corrected to 128 in ${BOOT}"
    echo "REBOOT_REQUIRED: gpu_mem change needs a reboot"
fi

exit 0
