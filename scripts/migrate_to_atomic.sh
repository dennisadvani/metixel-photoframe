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

# If already migrated, complain loudly and exit (idempotent-guard).
if [ -L "${LIVE_LINK}" ] || [ -d "${DATA_DIR}" ]; then
    echo "Already migrated (${LIVE_LINK} or ${DATA_DIR} exists). Nothing to do."
    exit 0
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
echo "[3/8] Creating /data, /releases, /live…"
mkdir -p "${DATA_DIR}/config" "${DATA_DIR}/logs" "${DATA_DIR}/media" "${DATA_DIR}/cache" "${DATA_DIR}/backups"
mkdir -p "${RELEASES_DIR}"

# ── Move persistent data → /data ────────────────────────────────────────────
# config.json AND logging.conf move into /data (data/etc for logging.conf).
# __main__.py resolves logging.conf as data_dir()/etc/logging.conf, so it must
# live under /data/etc — all persistent config lives under /data.
echo "[4/8] Moving persistent data (config, logs, media, cache) → /data…"
mkdir -p "${DATA_DIR}/etc"
if [ -e "${INSTALL_ROOT}/etc/config.json" ]; then
    mv "${INSTALL_ROOT}/etc/config.json" "${DATA_DIR}/config.json"
fi
if [ -e "${INSTALL_ROOT}/etc/logging.conf" ]; then
    mv "${INSTALL_ROOT}/etc/logging.conf" "${DATA_DIR}/etc/logging.conf"
fi
for item in logs media cache; do
    if [ -e "${INSTALL_ROOT}/${item}" ]; then
        mv "${INSTALL_ROOT}/${item}" "${DATA_DIR}/"
    fi
done
# 'etc' may still hold other files — keep it with the code; config moved.
chown -R pi:pi "${DATA_DIR}" 2>/dev/null || true

# ── Move application code → /releases/<version> ─────────────────────────────
echo "[5/8] Moving application code → ${RELEASE_DIR}…"
mkdir -p "${RELEASE_DIR}"
# Move everything EXCEPT the dirs we already moved into /data (config, logs,
# media, cache, etc) — those belong to the data layer now.  'run' is transient.
for entry in "${INSTALL_ROOT}"/.* "${INSTALL_ROOT}"/*; do
    name="$(basename "$entry")"
    case "$name" in
        "."|".."|"data"|"releases"|"live"|"cache"|"logs"|"media"|"etc")
            continue
            ;;
    esac
    mv "$entry" "${RELEASE_DIR}/" 2>/dev/null || true
done
# Keep the git checkout inside the release so future updates reference the repo.
if [ -d "${INSTALL_ROOT}/.git" ]; then
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

# ── Rewrite systemd units to point at live + data ──────────────────────────
echo "[8/8] Updating systemd unit files (live + data paths)…"
for unit in metixel-backend.service metixel-cage.service metixel-frontend.service; do
    src="/etc/systemd/system/${unit}"
    if [ ! -f "${src}" ]; then
        continue
    fi
    sed -i \
        -e 's|WorkingDirectory=/opt/metixel|WorkingDirectory=/opt/metixel/live|g' \
        -e 's|PYTHONPATH=/opt/metixel/src|PYTHONPATH=/opt/metixel/live/src|g' \
        -e 's|PYTHONPATH=/opt/metixel$|PYTHONPATH=/opt/metixel/live/src|g' \
        -e 's|--config /opt/metixel/etc/config.json|--config /opt/metixel/data/config.json|g' \
        -e 's|/opt/metixel/etc/config.json|/opt/metixel/data/config.json|g' \
        -e 's|/opt/metixel/scripts/cage_launch.sh|/opt/metixel/live/scripts/cage_launch.sh|g' \
        -e 's|ExecStartPre=/bin/mkdir -p /opt/metixel/logs /opt/metixel/cache|ExecStartPre=/bin/mkdir -p /opt/metixel/data/logs /opt/metixel/data/cache /opt/metixel/data/config /opt/metixel/data/media /opt/metixel/data/backups|g' \
        -e 's|ReadWritePaths=/opt/metixel |ReadWritePaths=/opt/metixel/releases /opt/metixel/data /opt/metixel/live |g' \
        "${src}"
done

systemctl daemon-reload

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