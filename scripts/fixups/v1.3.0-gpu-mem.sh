#!/usr/bin/env bash
#
# Metixel fixup — correct the GPU memory setting on Pi 2/3/Zero2W.
#
# Older installs may have an incorrect gpu_mem= in /boot/firmware/config.txt
# (e.g. 16 from a generic image). The setup script sets gpu_mem=128 for
# hardware video decode; this fixup repairs devices that predate that.
#
# Idempotent: only touches the file if gpu_mem is missing or not exactly 128.
# Handles duplicate gpu_mem= lines (the LAST one wins in config.txt, so any
# duplicate is a bug) by removing ALL gpu_mem= lines and appending a single
# gpu_mem=128. Prints REBOOT_REQUIRED if it changes anything.
set -euo pipefail

BOOT="/boot/firmware/config.txt"
[ -f "${BOOT}" ] || exit 0

# Count gpu_mem lines and check whether any is exactly 128.
COUNT="$(grep -c '^gpu_mem=' "${BOOT}" 2>/dev/null || true)"
HAS_128="$(grep -c '^gpu_mem=128' "${BOOT}" 2>/dev/null || true)"

# Correct only if there is no single gpu_mem=128 (i.e. missing, wrong, or
# duplicated — a duplicate means the last line wins and may not be 128).
if [ "${COUNT}" -ne 1 ] || [ "${HAS_128}" -ne 1 ]; then
    # Remove every gpu_mem= line, then append a single gpu_mem=128.
    sed -i '/^gpu_mem=/d' "${BOOT}"
    echo "gpu_mem=128" >> "${BOOT}"
    echo "gpu_mem corrected to 128 in ${BOOT}"
    echo "REBOOT_REQUIRED: gpu_mem change needs a reboot"
fi

exit 0
