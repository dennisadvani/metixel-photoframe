#!/usr/bin/env bash
#
# Metixel fixup — provision a writable ddcutil cache directory.
#
# ddcutil persists its performance stats and cached capabilities under
# $XDG_CACHE_HOME (falling back to $HOME/.cache).  The backend service runs
# with ProtectHome=yes, so /home is read-only for it — ddcutil could not write
# its cache, and every probe therefore re-ran the slow I²C sleep/retry timing.
# On marginal DDC buses (e.g. a Pi 3 driving an older HDMI monitor) that pushed
# `ddcutil capabilities` past the per-command timeout, so the web UI would
# intermittently report "No DDC/CI-capable monitor detected / no adjustable
# features" even though the monitor supports DDC.
#
# The adapter now points ddcutil's cache at
# /opt/metixel/data/cache/ddcutil (inside the service's ReadWritePaths), and
# creates it at runtime.  This fixup guarantees it also exists — and is
# pi-owned — on existing devices (e.g. after a root-run install left parts of
# the cache tree owned by root), which the pi-run service cannot repair itself.
#
# Idempotent: safe to re-run (tracked exactly-once in installed_fixups.json).
set -euo pipefail

CACHE_DIR="/opt/metixel/data/cache/ddcutil"

mkdir -p "${CACHE_DIR}"
chown -R pi:pi "${CACHE_DIR}" 2>/dev/null || true

# A root-owned legacy cache under the pi user's home (created before the
# service was hardened) is now unusable and only causes confusing permission
# warnings in the journal — remove it so ddcutil starts clean.
rm -rf /home/pi/.cache/ddcutil 2>/dev/null || true

echo "Ensured writable ddcutil cache dir ${CACHE_DIR} (owned by pi:pi)"
exit 0
