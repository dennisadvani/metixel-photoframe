#!/usr/bin/env bash
#
# Metixel fixup — provision the metixel-cursor-hider systemd service.
#
# The cursor-hider (a pure-Python virtual mouse that parks the compositor
# cursor off-screen) was introduced in 1.2.5-beta.1 as a NEW systemd unit.
# Devices that were installed or migrated by earlier releases never received
# the unit — migrate_to_atomic.sh only provisioned backend + cage — so the
# cursor was never hidden on them.
#
# This fixup runs from the NEW release's ota_install.sh (fixups are executed
# from the release being installed, before the Blue/Green swap), so it is
# applied the first time a device upgrades to a release that ships it.
#
# Idempotent: safe to re-run (tracked exactly-once in installed_fixups.json).
set -euo pipefail

# Root of the release whose fixup manifest is running (…/releases/<version>).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

UNIT_SRC="${REPO}/systemd/metixel-cursor-hider.service"
UNIT_DST="/etc/systemd/system/metixel-cursor-hider.service"
LIVE_MAIN="/opt/metixel/live/src/metixel/__main__.py"

# Only provision when this release actually ships the mode — a unit that execs
# `--mode cursor-hider` against code that lacks it would crash-loop forever.
# Mirrors the guard in setup_trixie_metixel.sh / migrate_to_atomic.sh.
if [ ! -f "${UNIT_SRC}" ] \
   || ! grep -q "cursor-hider" "${REPO}/src/metixel/__main__.py"; then
    echo "Release does not support cursor-hider — removing any stale unit"
    rm -f "${UNIT_DST}" 2>/dev/null || true
    exit 0
fi

cp "${UNIT_SRC}" "${UNIT_DST}"
systemctl daemon-reload
systemctl enable metixel-cursor-hider.service 2>/dev/null || true

if systemctl is-active --quiet metixel-cursor-hider.service; then
    echo "metixel-cursor-hider.service already active"
    exit 0
fi

# Start now only if the currently-live code supports the mode.  Fixups run
# BEFORE the swap on ordinary atomic upgrades, so live may still be the OLD
# release (the monolithic self-migration flips live first).  If live is too
# old, enabling is sufficient — the enabled service starts at next boot.
if [ -f "${LIVE_MAIN}" ] && grep -q "cursor-hider" "${LIVE_MAIN}"; then
    systemctl start metixel-cursor-hider.service 2>/dev/null || true
    if systemctl is-active --quiet metixel-cursor-hider.service; then
        echo "metixel-cursor-hider.service installed, enabled and started"
        exit 0
    fi
fi

echo "metixel-cursor-hider.service installed and enabled (will start at next boot)"
echo "REBOOT_REQUIRED: start metixel-cursor-hider at next boot"
exit 0