#!/usr/bin/env bash
#
# Metixel fixup — retire the user-editable data/etc/logging.conf.
#
# WHY
# ---
# logging.conf used to be a user-editable copy of the Python logging
# configuration, seeded into /opt/metixel/data/etc/ on every install.  It caused
# two concrete problems:
#
#   1. It hardcoded the log file path, so the backend AND the frontend both
#      loaded it and attached their own RotatingFileHandler to the SAME file.
#      Two processes rotating one file truncate each other's output, which is
#      why log lines visible in the web UI never reached metixel.log.
#   2. Its handler level overrode `system.log_level`, so setting INFO in the web
#      UI did not reliably stop DEBUG lines being written to disk.
#
# Logging is now configured in code: one file per process
# (metixel-backend.log / metixel-frontend.log), with the level coming solely
# from `system.log_level`.  This matches the project ethos of avoiding CLI/file
# configuration where the app can own it.
#
# This is a ONE-WAY REMOVAL: the target state ("logging.conf does not exist")
# cannot be derived from the release, because a device that never had the file
# is indistinguishable from one that did and has already been cleaned.  Running
# it exactly once per device is the correct semantic.
#
# Idempotent: safe to re-run (tracked exactly-once in installed_fixups.json).
set -euo pipefail

DATA_DIR="${METIXEL_INSTALL_ROOT:-/opt/metixel}/data"

removed_any=0

# ── 1. The canonical location ──────────────────────────────────────────────
CONF="${DATA_DIR}/etc/logging.conf"
if [ -f "${CONF}" ]; then
    # Back it up rather than deleting outright: a user who genuinely customised
    # their logging format can recover it, and the file is tiny.
    BACKUP="${DATA_DIR}/backups/logging.conf.retired"
    mkdir -p "$(dirname "${BACKUP}")"
    cp -a "${CONF}" "${BACKUP}" 2>/dev/null || true
    rm -f "${CONF}"
    echo "Removed ${CONF} (backup: ${BACKUP})"
    removed_any=1
else
    echo "No logging.conf at ${CONF} — nothing to remove"
fi

# ── 2. Legacy location (pre-atomic layout) ─────────────────────────────────
# Devices migrated from the monolithic layout may still carry a copy at the
# install root's etc/.  Left behind it would be re-seeded by nothing, but it is
# confusing to leave a dead config file.
LEGACY="${METIXEL_INSTALL_ROOT:-/opt/metixel}/etc/logging.conf"
if [ -f "${LEGACY}" ]; then
    rm -f "${LEGACY}"
    echo "Removed legacy ${LEGACY}"
    removed_any=1
fi

# ── 3. Remove data/etc only when it is now EMPTY ───────────────────────────
# rmdir (not rm -rf) on purpose: if anything else lives here, fail silently and
# leave it alone rather than deleting unrelated user data.
if [ -d "${DATA_DIR}/etc" ]; then
    if rmdir "${DATA_DIR}/etc" 2>/dev/null; then
        echo "Removed now-empty ${DATA_DIR}/etc"
    else
        echo "Kept ${DATA_DIR}/etc — not empty (contents: $(ls -A "${DATA_DIR}/etc" 2>/dev/null | tr '\n' ' '))"
    fi
fi

if [ "${removed_any}" -eq 1 ]; then
    echo "REBOOT_REQUIRED: restart metixel services to apply in-code logging config"
fi

exit 0
