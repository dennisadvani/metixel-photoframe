#!/usr/bin/env bash
#
# Metixel Photoframe — migrate an existing v1 (monolithic) installation to the
# atomic Blue/Green layout.
#
# BEFORE
#   /opt/metixel/          git checkout + config + logs + media + cache (all together)
#   systemd units point WorkingDirectory/ExecStart/PYTHONPATH at /opt/metixel
#
# AFTER
#   /opt/metixel/data/     persistent state  (config, logs, media, cache)
#   /opt/metixel/releases/<version>/   versioned application code
#   /opt/metixel/live      symlink → releases/<version>
#   systemd units point at /opt/metixel/live + /opt/metixel/data
#
# The script is idempotent (safe to re-run) and takes a full backup of the
# original install before moving anything.
#
# Usage: sudo bash scripts/migrate_to_atomic.sh [--version vX.Y.Z] [--no-restart] [--no-backup]
#   --version vX.Y.Z   Version label for the first release folder (default derives
#                      from the installed metixel.__version__).
#   --no-restart       Skip stopping/restarting services (for testing).
#   --no-backup        Skip the config/code backup. Used only by the self-migrate
#                      path (ota_install.sh), where the git checkout just reset to
#                      the new code and the old content is the same code — a backup
#                      would just copy the freshly-fetched (.clean) checkout.
#
# On success, prints the created release dir (RELEASE_DIR) to stdout so callers
# (e.g. ota_install.sh) can re-point the working repo at the migrated code.
set -euo pipefail

INSTALL_ROOT="${METIXEL_INSTALL_ROOT:-/opt/metixel}"
DATA_DIR="${INSTALL_ROOT}/data"
RELEASES_DIR="${INSTALL_ROOT}/releases"
LIVE_LINK="${INSTALL_ROOT}/live"

RESTART="yes"
DO_BACKUP="yes"

# ── arg parsing ─────────────────────────────────────────────────────────────
VERSION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            VERSION="$2"
            shift 2
            ;;
        --no-restart)
            RESTART="no"
            shift
            ;;
        --no-backup)
            DO_BACKUP="no"
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# ── root check ──────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)." >&2
    exit 1
fi
if [ ! -d "${INSTALL_ROOT}" ]; then
    echo "ERROR: ${INSTALL_ROOT} does not exist — nothing to migrate." >&2
    exit 1
fi

# If already migrated, complain and exit (idempotent-guard).
# Only treat as migrated when the `live` symlink is a valid release pointer.
# A stray/partial `data/` from an aborted migration must NOT count as migrated
# — otherwise this script bails on the next run and the device is stuck on a
# broken half-migrated state.
if [ -L "${LIVE_LINK}" ] && [ -d "$(readlink -f "${LIVE_LINK}" 2>/dev/null || true)" ]; then
    echo "Already migrated (${LIVE_LINK} → $(readlink "${LIVE_LINK}")). Nothing to do."
    exit 0
fi
# Re-entry on a partial migration: a data/ dir may exist but no live symlink.
# We proceed and REPAIR (idempotent) rather than bail.
if [ -d "${DATA_DIR}" ]; then
    echo "Re-running migration to repair a partial/aborted previous run."
fi

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   Metixel — migrate to atomic Blue/Green layout              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "Install root : ${INSTALL_ROOT}"
echo ""

# ── Derive version label ────────────────────────────────────────────────────
if [ -z "${VERSION}" ]; then
    if [ -f "${INSTALL_ROOT}/src/metixel/__init__.py" ]; then
        VERSION="$(PYTHONPATH="${INSTALL_ROOT}/src" python3 -c \
            'import metixel; print(metixel.__version__)' 2>/dev/null || true)"
    fi
fi
VERSION="${VERSION:-$(basename "$(readlink -f "${INSTALL_ROOT}")" 2>/dev/null || true)}"
VERSION="${VERSION:-v1.1.0}"
RELEASE_DIR="${RELEASES_DIR}/${VERSION}"

echo "First release folder: ${RELEASE_DIR}"

# Stop services (metadata, not data)
if [ "${RESTART}" = "yes" ]; then
    echo "[1/8] Stopping metixel services…"
    systemctl stop metixel-cage 2>/dev/null || true
    systemctl stop metixel-backend 2>/dev/null || true
    sleep 2
fi

# ── Backup (config + code, NOT media/cache) ────────────────────────────────
# A full `cp -a` of the install root would duplicate media + cache (potentially
# many GB on an SD card). Only the recoverable/manual config + git history +
# code are backed up — media is re-importable and cache is regenerable.
BACKUP_DIR="${INSTALL_ROOT}.atomic-backup.$(date +%s)"
if [ "${DO_BACKUP}" = "yes" ]; then
    echo "[2/8] Backing up configuration + install to ${BACKUP_DIR}…"
    mkdir -p "${BACKUP_DIR}"
    cp -a "${INSTALL_ROOT}/etc" "${BACKUP_DIR}/" 2>/dev/null || true
    if [ -d "${INSTALL_ROOT}/.git" ]; then
        cp -a "${INSTALL_ROOT}/.git" "${BACKUP_DIR}/" 2>/dev/null || true
    fi
    if [ -f "${INSTALL_ROOT}/requirements-pip.txt" ]; then
        cp -a "${INSTALL_ROOT}/requirements-pip.txt" "${BACKUP_DIR}/" 2>/dev/null || true
    fi
    if [ -f "${INSTALL_ROOT}/requirements-system.txt" ]; then
        cp -a "${INSTALL_ROOT}/requirements-system.txt" "${BACKUP_DIR}/" 2>/dev/null || true
    fi
else
    echo "[2/8] Skipping backup (--no-backup)."
fi

# ── Create the target layout ────────────────────────────────────────────────
# NOTE: data/{media,cache,logs} are NOT pre-created here — they must come from
# the monolithic move below, so the ENTIRE top-level folder (including any
# user-created custom subfolders) is preserved. Only empty scaffolding dirs
# that have no monolithic source are created now.
echo "[3/8] Creating /data, /releases, /live…"
mkdir -p "${DATA_DIR}/backups"
mkdir -p "${RELEASES_DIR}"

# ── Move persistent data → /data ────────────────────────────────────────────
# config.json AND logging.conf move into /data (data/etc for logging.conf).
# __main__.py resolves logging.conf as data_dir()/etc/logging.conf, so it must
# live under /data/etc — all persistent config lives under /data.
echo "[4/8] Moving persistent data (config, logs, media, cache) → /data…"
mkdir -p "${DATA_DIR}/etc"
# Idempotent moves: only move if the source still exists AND the target is not
# already present (re-entry on a partial migration must not fail or clobber).
if [ -e "${INSTALL_ROOT}/etc/config.json" ] && [ ! -e "${DATA_DIR}/config.json" ]; then
    mv "${INSTALL_ROOT}/etc/config.json" "${DATA_DIR}/config.json"
fi
if [ -e "${INSTALL_ROOT}/etc/logging.conf" ] && [ ! -e "${DATA_DIR}/etc/logging.conf" ]; then
    mv "${INSTALL_ROOT}/etc/logging.conf" "${DATA_DIR}/etc/logging.conf"
fi
# Move the ENTIRE media, logs and cache folders (not file-by-file) so every
# subfolder the user may have added to the watched local folders is preserved.
# Do NOT pre-create data/{media,cache,logs} — the `mv` of the whole top-level
# folder is atomic and preserves custom subfolders. If the data/ dir already
# exists (a re-entry/placeholder), fall back to a copy-merge so nothing is lost.
for item in media logs cache; do
    if [ -e "${INSTALL_ROOT}/${item}" ]; then
        if [ -e "${DATA_DIR}/${item}" ]; then
            # Both exist (re-entry over a placeholder/partial). Merge the
            # top-level contents into the data copy (no overwrite), then remove
            # the now-empty top-level dir so it isn't recreated as an orphan.
            cp -an "${INSTALL_ROOT}/${item}/." "${DATA_DIR}/${item}/" 2>/dev/null || true
            rm -rf "${INSTALL_ROOT}/${item}"
        else
            mv "${INSTALL_ROOT}/${item}" "${DATA_DIR}/"
        fi
    fi
done
# 'etc' may still hold other files — keep it with the code; config moved.
chown -R pi:pi "${DATA_DIR}" 2>/dev/null || true

# ── Move application code → /releases/<version> ─────────────────────────────
echo "[5/8] Moving application code → ${RELEASE_DIR}…"
mkdir -p "${RELEASE_DIR}"
# Move everything EXCEPT the dirs we already moved into /data (config, logs,
# media, cache, etc) — those belong to the data layer now.  'run' is transient.
# Idempotent on re-entry: skip a path if the release target already has it.
for entry in "${INSTALL_ROOT}"/.* "${INSTALL_ROOT}"/*; do
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
if [ -d "${INSTALL_ROOT}/.git" ] && [ ! -d "${RELEASE_DIR}/.git" ]; then
    mv "${INSTALL_ROOT}/.git" "${RELEASE_DIR}/" 2>/dev/null || true
fi
# Recreate an empty 'etc' for any code-side default templates (config in /data).
mkdir -p "${INSTALL_ROOT}/etc"
chown -R pi:pi "${RELEASE_DIR}" 2>/dev/null || true

# ── Create the live symlink ─────────────────────────────────────────────────
echo "[6/8] Creating /opt/metixel/live symlink…"
ln -sfn "${RELEASE_DIR}" "${LIVE_LINK}"
chown -h pi:pi "${LIVE_LINK}" 2>/dev/null || true
# OTA updates run as root via systemd-run — mark the release repo as safe for git.
git config --system --add safe.directory "${RELEASE_DIR}" 2>/dev/null || true

# ── Record the currently-managed packages (for future package removal) ──────
# The code (and its requirements files) have already moved into RELEASE_DIR,
# so read the manifests from there — NOT from INSTALL_ROOT (which is now empty).
echo "[7/8] Recording installed Metixel-managed packages…"
python3 - "${RELEASE_DIR}" "${DATA_DIR}" <<'PYEOF'
import json, os, sys

release_dir, data_dir = sys.argv[1], sys.argv[2]
installed = {"apt": [], "pip": []}

# Record the packages Metixel currently manages from the requirements files.
req_apt = os.path.join(release_dir, "requirements-system.txt")
if os.path.isfile(req_apt):
    with open(req_apt) as f:
        installed["apt"] = [ln.strip() for ln in f
                            if ln.strip() and not ln.lstrip().startswith("#")]

req_pip = os.path.join(release_dir, "requirements-pip.txt")
if os.path.isfile(req_pip):
    with open(req_pip) as f:
        names = []
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            name = ln.split(";", 1)[0].strip().split("[", 1)[0].strip()
            for ch in "=<>~! \t":
                name = name.split(ch, 1)[0].strip()
            if name:
                names.append(name)
        installed["pip"] = names

os.makedirs(data_dir, exist_ok=True)
with open(os.path.join(data_dir, "installed_packages.json"), "w") as f:
    json.dump(installed, f, indent=2)
print("  recorded", len(installed["apt"]), "apt and", len(installed["pip"]), "pip packages")
PYEOF

# ── Ensure the whole install root is pi-owned ──────────────────────────────
# On a clean install /opt/metixel is often root-owned (setup ran via sudo).
# The `pi` service must be able to create data subdirs (e.g. data/logs via
# ensure_data_dirs) and write logs/media/cache — so chown the entire root
# recursively at the end. This prevents the crash-loop:
#   PermissionError: /opt/metixel/data/logs (created by pi but data owned root).
chown -R pi:pi "${INSTALL_ROOT}" 2>/dev/null || true
chown -h pi:pi "${LIVE_LINK}" 2>/dev/null || true

# ── Install canonical systemd units (live + data paths) ────────────────────
# Copy the SHIPPED unit files from the release rather than sed-mutating the old
# ones. The shipped units are the authoritative Blue/Green definitions (correct
# WorkingDirectory / PYTHONPATH / ExecStart / ReadWritePaths); this is the same
# mechanism setup_trixie_metixel.sh uses, and it avoids the fragile exact-string
# sed that previously left some devices with a stale PYTHONPATH (crash-loop).
echo "[8/8] Installing canonical systemd unit files…"
for unit in metixel-backend.service metixel-cage.service; do
    if [ -f "${RELEASE_DIR}/systemd/${unit}" ]; then
        cp "${RELEASE_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}"
    fi
done

# metixel-frontend.service was the obsolete pre-cage launcher and is no longer
# shipped.  Some aging devices may still have a stale copy installed from an
# early release — stop and remove it so it can't linger or double-launch the
# frontend alongside cage.
if systemctl list-unit-files metixel-frontend.service 2>/dev/null | grep -q metixel-frontend; then
    systemctl stop metixel-frontend.service 2>/dev/null || true
    systemctl disable metixel-frontend.service 2>/dev/null || true
    rm -f /etc/systemd/system/metixel-frontend.service
    systemctl daemon-reload
    echo "  Removed obsolete metixel-frontend.service"
fi

systemctl daemon-reload
# Ensure the services are enabled so they survive reboot.
systemctl enable metixel-backend.service metixel-cage.service 2>/dev/null || true

# ── Update Samba share path → /data ─────────────────────────────────────────
# The migration moved media/ from ${INSTALL_ROOT}/media to ${DATA_DIR}/media.
# If a Samba share exists pointing at the OLD path, it would be broken after
# migration.  Rewrite any metixel share path to the new data location.
echo "[9/9] Updating Samba share path → ${DATA_DIR}/media…"
SMB_CONF="/etc/samba/smb.conf"
if [ -f "${SMB_CONF}" ]; then
    # Rewrite the old monolithic media path to the new data path.  Only touch
    # lines that reference the old install-root media location.
    if grep -q "path = ${INSTALL_ROOT}/media" "${SMB_CONF}" 2>/dev/null; then
        sed -i "s|path = ${INSTALL_ROOT}/media|path = ${DATA_DIR}/media|g" "${SMB_CONF}"
        echo "  Updated Samba share path to ${DATA_DIR}/media"
        # Restart smbd so the new path takes effect.
        systemctl restart smbd 2>/dev/null || true
    else
        echo "  No Samba share referencing ${INSTALL_ROOT}/media — nothing to update."
    fi
else
    echo "  No Samba config found (${SMB_CONF}) — skipping."
fi

# ── Finalise / restart ──────────────────────────────────────────────────────
echo ""
echo "Migration complete."
echo "  data   : ${DATA_DIR}"
echo "  release: ${RELEASE_DIR}"
echo "  live   : ${LIVE_LINK} → $(readlink "${LIVE_LINK}")"
if [ "${RESTART}" = "yes" ]; then
    echo "Restarting services…"
    systemctl enable metixel-backend metixel-cage 2>/dev/null || true
    systemctl start metixel-backend 2>/dev/null || true
    systemctl start metixel-cage 2>/dev/null || true
fi
if [ "${DO_BACKUP}" = "yes" ]; then
    echo "Original install backed up at: ${BACKUP_DIR}"
    echo "(Remove ${BACKUP_DIR} once you have verified the new layout.)"
fi

# ── Emit the release dir on stdout (captured by self-migrating callers) ─────
# ota_install.sh uses this to re-point the working repo at the moved code.
echo "MIGRATED_RELEASE_DIR=${RELEASE_DIR}"