#!/usr/bin/env bash
#
# Metixel fixup — enable the i2c-dev kernel module for ddcutil.
#
# ddcutil talks DDC/CI to the monitor over the I²C bus, which requires the
# i2c-dev kernel module.  Devices installed before this fixup may not have
# it configured.  This creates a persistent modules-load.d entry (so it
# loads on every boot) and loads it now so ddcutil works without a reboot.
#
# Idempotent: only writes the config if it's missing or wrong, and always
# attempts modprobe (harmless if already loaded).  No reboot required.
set -euo pipefail

CONF="/etc/modules-load.d/metixel-i2c.conf"

# Ensure the persistent module-load entry exists and contains i2c-dev.
if [ ! -f "${CONF}" ] || ! grep -qx "i2c-dev" "${CONF}" 2>/dev/null; then
    echo "i2c-dev" > "${CONF}"
    echo "Enabled i2c-dev module (persistent via ${CONF})"
fi

# Load the module now so ddcutil works without a reboot.
if modprobe i2c-dev 2>/dev/null; then
    echo "Loaded i2c-dev module"
else
    echo "WARNING: could not load i2c-dev module (may need a reboot)"
fi

exit 0