#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# Metixel Photoframe — idempotent host configuration reconciliation.
#
# WHY THIS EXISTS
# ---------------
# Host configuration used to be applied by BOTH the fresh-install script and
# the one-time device fixups (scripts/fixups/*.sh).  Every change had to be
# written twice, and the two copies inevitably drifted — devices ended up
# running NEW code under OLD systemd
# units, with an unusable ddcutil cache, because the "install-time only" path
# was never re-run on upgrade.
#
# This script is the single source of truth for Metixel-managed host state.  It
# is called from the RELEASE being installed, on both fresh installs and
# upgrades, so there is exactly one definition of "what the host should look
# like" and no install/upgrade drift.
#
# DESIGN RULES
# ------------
#   1. Idempotent — safe to run any number of times; a converged host is a
#      silent no-op.  Never assume a fresh device.
#   2. Non-destructive — only writes Metixel-owned files, or Metixel-owned
#      VALUES inside shared files.  Never overwrites a user's own setting, and
#      never rewrites a shared config file wholesale.
#   3. Add-if-absent for shared files (smb.conf, /etc/default/hostapd).
#   4. Reports each change; says nothing when already converged.
#   5. --dry-run prints what WOULD change and modifies nothing.
#
# WHAT THIS SCRIPT DELIBERATELY DOES *NOT* DO
# -------------------------------------------
#   * /opt/metixel/data/config.json values — the user's file.  New keys are
#     added by the application via setdefault/defaults, never reset here.
#   * Interactive decisions (release channel, WiFi country) — setup only.
#   * Anything requiring a reboot to *validate* (it may write boot config, but
#     it reports REBOOT_REQUIRED rather than rebooting).
#
# OFFLINE MODE (--offline)
# ------------------------
# Provisioning tools (e.g. an image builder) need to converge a root filesystem
# that is NOT running: it is mounted/chrooted, with no PID 1, no logind, and a
# kernel that belongs to the BUILD host rather than the target device.  Two
# classes of step are impossible or actively dangerous there:
#
#   * impossible  — systemctl daemon-reload/restart/is-active, loginctl,
#                   netfilter-persistent (nothing is running to talk to);
#   * dangerous   — iptables, modprobe, rfkill, iw reg set (these would change
#                   the BUILD HOST's netfilter/modules/hardware, not the image).
#
# --offline re-expresses each of those as its PERSISTENT EQUIVALENT — the file
# that the running system would consume on next boot — so the resulting image
# converges to the same state without touching the build host:
#
#   iptables … REDIRECT        → /etc/iptables/rules.v4
#   loginctl enable-linger     → /var/lib/systemd/linger/<user>
#   modprobe i2c-dev           → /etc/modules-load.d/metixel-i2c.conf  (always)
#   iw reg set                 → /etc/modprobe.d/cfg80211.conf         (always)
#   rfkill unblock wifi        → /var/lib/systemd/rfkill/<ID_PATH>:wlan = 0
#
# Everything else in this script is already pure file/dir work and behaves
# identically offline.
#
# Usage:
#   sudo bash scripts/reconcile.sh [--dry-run] [--offline] [--unit-backup-dir DIR]
#
#   --dry-run   Print what would change; modify nothing.
#   --offline   Converge a non-running root filesystem (image build): skip
#               runtime-only calls and write their persistent equivalents.
#
# Exit codes: 0 = converged (or dry-run), 1 = a change could not be applied.

set -uo pipefail

INSTALL_ROOT="${METIXEL_INSTALL_ROOT:-/opt/metixel}"
DATA_DIR="${INSTALL_ROOT}/data"
LIVE_LINK="${INSTALL_ROOT}/live"

# Backup location for units replaced here.  Deliberately OUTSIDE the install
# root: /opt/metixel is recreated on a re-image, whereas /etc/systemd/system
# survives — and it is exactly the directory whose units a rollback restores.
UNIT_BACKUP_DIR="/etc/systemd/system/.metixel-backup"

DRY_RUN="no"
OFFLINE="no"
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY_RUN="yes" ;;
        --offline) OFFLINE="yes" ;;
        --unit-backup-dir=*) UNIT_BACKUP_DIR="${arg#*=}" ;;
        --unit-backup-dir)
            echo "ERROR: --unit-backup-dir requires a value (use --unit-backup-dir=DIR)" >&2
            exit 1
            ;;
        -h|--help)
            sed -n '2,72p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: ${arg}" >&2
            exit 1
            ;;
    esac
done
# Support both --unit-backup-dir=DIR and --unit-backup-dir DIR.
prev=""
for arg in "$@"; do
    if [ "${prev}" = "--unit-backup-dir" ]; then UNIT_BACKUP_DIR="${arg}"; fi
    prev="${arg}"
done

if [ "$(id -u)" -ne 0 ] && [ "${DRY_RUN}" = "no" ]; then
    echo "ERROR: this script must run as root (use sudo)" >&2
    exit 1
fi

# Root of the release whose copy of this script is running (…/releases/<ver>).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Sanity-guard the release root BEFORE anything destructive uses it.
# The unit logic below decides whether to install or REMOVE the cursor-hider
# unit by probing "${REPO}/src/metixel/__main__.py".  Run from somewhere that
# is not a release (e.g. /tmp) and REPO resolves to a directory with no source,
# which would make every release look like it "does not ship" the mode — and the
# script would delete a working unit.  Refuse to run in that case.
if [ ! -f "${REPO}/src/metixel/__main__.py" ] || [ ! -f "${REPO}/pyproject.toml" ]; then
    echo "ERROR: ${REPO} does not look like a Metixel release" >&2
    echo "       (expected src/metixel/__main__.py + pyproject.toml)" >&2
    echo "       Refusing to run: unit checks would misread this as 'mode not shipped'." >&2
    exit 1
fi

CHANGES=0
FAILURES=0
NEEDS_REBOOT="no"

# Appended to the summary line so a build log makes the mode obvious.
OFFLINE_SUFFIX=""
[ "${OFFLINE}" = "yes" ] && OFFLINE_SUFFIX=" (offline)"

# ── Output helpers ─────────────────────────────────────────────────────────
_say()  { printf '  %s\n' "$*"; }
_plus() { CHANGES=$((CHANGES + 1)); printf '  + %s\n' "$*"; }
_same() { printf '  = %s\n' "$*"; }
_warn() { printf '  ! %s\n' "$*" >&2; }
_fail() { FAILURES=$((FAILURES + 1)); printf '  ! FAILED: %s\n' "$*" >&2; }

# Echo the command instead of running it while in dry-run mode.
_run() {
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

# ── Offline (image-build) helpers ──────────────────────────────────────────
# $OFFLINE means: converge a NON-RUNNING root filesystem.  Runtime calls that
# would otherwise touch the BUILD host are replaced with their persistent
# equivalents, and calls that have no persistent form are skipped.

# True when the step should be executed (i.e. we are NOT offline).
# Usage: if _online; then … runtime-only step …; fi
_online() { [ "${OFFLINE}" != "yes" ]; }

# Report a runtime-only step that has no persistent equivalent, so an offline
# run is explicit about what it deliberately did not do.
_skip_offline() {
    printf '  ~ offline: skipped — %s\n' "$*"
}

# Merge the NAT PREROUTING redirect into /etc/iptables/rules.v4 (the file
# iptables-persistent replays at boot).  Written only when the rule is not
# already present, so repeated runs are no-ops.
#
# Non-destructive, matching this script's rule #2:
#   * no file            → write the stock tables (fresh image)
#   * file has the rule  → no-op
#   * file lacks it      → INSERT after the `*nat` header, so any other rules
#                          the device already had are preserved.  Only when
#                          there is no `*nat` table at all do we append one.
_iptables_redirect_offline() {
    local rules="/etc/iptables/rules.v4" rule
    rule="-A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080"
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] write %s with %s\n' "${rules}" "${rule}"
        CHANGES=$((CHANGES + 1))
        return 0
    fi
    if [ -f "${rules}" ] && grep -qF -- "${rule}" "${rules}"; then
        _same "iptables 80 → 8080 redirect (in ${rules})"
        return 0
    fi
    _run mkdir -p /etc/iptables
    if [ ! -f "${rules}" ]; then
        cat > "${rules}" <<'IPTEOF'
*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
-A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080
COMMIT
*filter
:INPUT ACCEPT [0:0]
:FORWARD ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
COMMIT
IPTEOF
        chmod 0644 "${rules}"
        _plus "wrote ${rules} (80 → 8080 redirect)"
        return 0
    fi
    # File exists without the rule — insert into the existing nat table.
    # The rule must come AFTER the chain-policy declarations (:PREROUTING …):
    # iptables-restore requires every chain to be declared before any rule that
    # targets it, so we insert immediately before the nat table's COMMIT.
    #
    # Pure bash (read/printf) on purpose: `awk` is not part of the essential
    # toolset and is not guaranteed in every environment this runs in (a
    # constrained provisioning chroot), whereas a read loop needs no external
    # command at all.
    if grep -q '^\*nat' "${rules}"; then
        local in_nat=0 inserted=0 line
        : > "${rules}.metixel-new"
        while IFS= read -r line || [ -n "${line}" ]; do
            case "${line}" in
                '*nat') in_nat=1 ;;
                'COMMIT')
                    if [ "${in_nat}" = "1" ] && [ "${inserted}" = "0" ]; then
                        printf '%s\n' "${rule}" >> "${rules}.metixel-new"
                        inserted=1
                    fi
                    in_nat=0
                    ;;
            esac
            printf '%s\n' "${line}" >> "${rules}.metixel-new"
        done < "${rules}"
        mv -f "${rules}.metixel-new" "${rules}" \
            && _plus "inserted 80 → 8080 redirect into ${rules} (existing rules preserved)"
    else
        cat >> "${rules}" <<'IPTEOF'

*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
-A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080
COMMIT
IPTEOF
        _plus "appended nat table with 80 → 8080 redirect to ${rules}"
    fi
}

# Enable systemd linger by writing the marker file directly.  This is exactly
# what `loginctl enable-linger <user>` does (systemd-logind reads this dir at
# boot), and it needs no running logind.
_linger_offline() {
    local user="$1" dir="/var/lib/systemd/linger"
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] write %s/%s (linger)\n' "${dir}" "${user}"
        CHANGES=$((CHANGES + 1))
        return 0
    fi
    _run mkdir -p "${dir}"
    if [ -f "${dir}/${user}" ]; then
        _same "linger enabled for ${user} (${dir}/${user})"
        return 0
    fi
    : > "${dir}/${user}"
    _plus "enabled linger for ${user} (wrote ${dir}/${user})"
}

# Write stdin to a file only when the content differs (idempotent + atomic).
# Usage: _ensure_file <path> <mode> <<'EOF' ... EOF
#
# Creates the parent directory when missing: some targets (/etc/modprobe.d,
# /etc/modules-load.d, /etc/NetworkManager/conf.d) are created by packages that
# may not have been installed yet on a fresh image, and `install` fails on a
# missing parent.  A failed write is REPORTED as a failure — without this it was
# silently reported as success (no `set -e`), so a broken write could ship.
_ensure_file() {
    local path="$1" mode="${2:-0644}"
    local tmp parent
    tmp="$(mktemp)"
    cat > "${tmp}"
    if [ -f "${path}" ] && cmp -s "${tmp}" "${path}"; then
        _same "${path} already current"
        rm -f "${tmp}"
        return 0
    fi
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] write %s (%s)\n' "${path}" "${mode}"
        CHANGES=$((CHANGES + 1))
        rm -f "${tmp}"
        return 0
    fi
    parent="$(dirname "${path}")"
    if [ ! -d "${parent}" ]; then
        mkdir -p "${parent}" || { _fail "could not create ${parent}"; rm -f "${tmp}"; return 1; }
    fi
    if ! install -m "${mode}" "${tmp}" "${path}"; then
        _fail "could not write ${path}"
        rm -f "${tmp}"
        return 1
    fi
    rm -f "${tmp}"
    _plus "wrote ${path}"
}

# Chown root-owned ancestors of <dir> up to (but never including) DATA_DIR.
#
# _ensure_dir chowns only the LEAF it is asked about, but `mkdir -p` creates the
# whole missing chain. If an INTERMEDIATE directory is missing, that
# intermediate is created by root and never chowned, so the pi-run backend
# cannot create anything inside it. That is exactly what broke the image build:
#   * data/media already existed (pi-owned), so _ensure_dir skipped it and
#     never descended into it;
#   * data/media/sync did not exist, so the mkdir -p for media/sync/immich
#     created `sync` as ROOT and chowned only `immich`;
#   * the Immich worker could then not create album_<id>/ or download an asset
#     into it, because pi cannot write inside a root-owned directory.
#
# Running this on EVERY _ensure_dir call (not only when creating) also HEALS an
# already-broken tree on the next reconcile instead of needing a fresh install.
#
# The walk goes UP and stops at the first ancestor that is not root-owned, or at
# DATA_DIR itself: /opt/metixel is root-owned by design and must stay that way.
# Non-recursive chowns only - this never walks a media library.
_fix_ancestors() {
    local dir="${1%/*}" owner="$2"
    while [ "${dir#"${DATA_DIR}"/}" != "${dir}" ]; do
        [ "$(stat -c '%u' "${dir}" 2>/dev/null)" = "0" ] || break
        chown "${owner}" "${dir}" 2>/dev/null || true
        dir="${dir%/*}"
    done
}

# Create a directory (and optionally fix its ownership) only when needed.
# Usage: _ensure_dir <path> [owner] [recursive]
#
# Ownership is fixed NON-recursively by default.  A recursive chown walks every
# entry, which is unacceptable on the media tree (a real library holds thousands
# of files on slow SD storage).  Pass "recursive" only for small directories
# where a root-created file inside is the actual failure mode we must repair
# (e.g. a root-owned metixel.log crash-looping the pi-run service).
_ensure_dir() {
    local path="$1" owner="${2:-}" recursive="${3:-}"
    local cur
    # Repair root-owned ancestors of ${path}. Called BOTH before and after the
    # mkdir below: beforehand it heals an already-broken tree, afterwards it
    # fixes the intermediate directories `mkdir -p` has just created as root.
    # See _fix_ancestors for why this is needed at all.
    if [ -n "${owner}" ] && [ "${DRY_RUN}" = "no" ]; then
        _fix_ancestors "${path}" "${owner}"
    fi
    if [ ! -d "${path}" ]; then
        _run mkdir -p "${path}"
        if [ -n "${owner}" ] && [ "${DRY_RUN}" = "no" ]; then
            _fix_ancestors "${path}" "${owner}"
            chown "${owner}" "${path}" 2>/dev/null \
                || _warn "could not chown ${path} → ${owner} (does the user exist yet?)"
        fi
        _plus "created ${path}"
        return 0
    fi
    [ -n "${owner}" ] || return 0
    cur="$(stat -c '%U:%G' "${path}" 2>/dev/null || echo '')"
    if [ "${cur}" = "${owner}" ]; then
        _same "${path} (${owner})"
        return 0
    fi
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] chown %s %s (was %s)\n' "${owner}" "${path}" "${cur}"
        CHANGES=$((CHANGES + 1))
        return 0
    fi
    if [ "${recursive}" = "recursive" ]; then
        chown -R "${owner}" "${path}" && _plus "chowned ${path} → ${owner} (recursive)" \
            || _warn "could not chown -R ${path} → ${owner}"
    else
        chown "${owner}" "${path}" && _plus "chowned ${path} → ${owner}" \
            || _warn "could not chown ${path} → ${owner}"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════
# 1. Directory layout (persistent data tree)
# ═══════════════════════════════════════════════════════════════════════════
# This is the SINGLE owner of the data tree — creation AND ownership.  It is
# deliberately not duplicated in the systemd unit (which previously created a
# `data/config` directory that does not exist by design) nor in the application
# (`ensure_data_dirs` was removed for the same reason).  The app runs as pi and
# cannot chown a root-owned directory, so ownership must be reconciled here as
# root.
#
# NOTE: there is no `data/config` — config.json lives directly at
# data/config.json.  There is also no `data/etc`: that directory existed only to
# hold logging.conf, which has been retired (logging is configured in code, one
# file per process under data/logs).  Do not recreate it.
#
# Ownership is fixed NON-recursively: a recursive chown over data/media would
# walk the entire library on every update.
echo "== Directory layout =="
# The root itself must be pi-owned: atomic config writes create a temp file
# directly in this directory before os.replace() into place.
_ensure_dir "${DATA_DIR}" "pi:pi"
for d in logs media media/my_media media/sync/immich cache backups; do
    _ensure_dir "${DATA_DIR}/${d}" "pi:pi"
done
# ddcutil's cache lives under data/cache (the backend unit points
# XDG_CACHE_HOME at it) — created with its parent loop above, but asserted here
# because the service cannot create it itself under ProtectHome/ProtectSystem.
_ensure_dir "${DATA_DIR}/cache/ddcutil" "pi:pi"
# Run dir for the IPC socket — the backend unit's RuntimeDirectory also covers
# this for the service, but manual/desktop runs need it too.
_ensure_dir /run/metixel

# ═══════════════════════════════════════════════════════════════════════════
# 2. systemd units (the thing that used to drift)
# ═══════════════════════════════════════════════════════════════════════════
# /etc/systemd/system is NOT part of the Blue/Green symlink swap, so units must
# be reconciled explicitly or an upgrade runs NEW code under OLD units.
echo "== systemd units =="
UNIT_SRC_DIR="${REPO}/systemd"
mkdir -p "${UNIT_BACKUP_DIR}" 2>/dev/null || true

_units_changed=0
for unit in metixel-backend.service metixel-cage.service; do
    src="${UNIT_SRC_DIR}/${unit}"
    dst="/etc/systemd/system/${unit}"
    if [ ! -f "${src}" ]; then
        _warn "${unit} not shipped by this release — leaving installed copy"
        continue
    fi
    if [ -f "${dst}" ] && cmp -s "${src}" "${dst}"; then
        _same "${unit}"
        continue
    fi
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] install %s → %s\n' "${src}" "${dst}"
        CHANGES=$((CHANGES + 1))
        continue
    fi
    [ -f "${dst}" ] && cp -a "${dst}" "${UNIT_BACKUP_DIR}/${unit}"
    install -m 0644 "${src}" "${dst}.metixel-new" && mv -f "${dst}.metixel-new" "${dst}"
    _plus "${unit} updated"
    _units_changed=1
    _run systemctl enable "${unit}" >/dev/null 2>&1
done

# Cursor-hider is only valid when the release ships the mode; a unit execing
# `--mode cursor-hider` against code lacking it would crash-loop forever.
if [ -f "${UNIT_SRC_DIR}/metixel-cursor-hider.service" ] \
   && grep -q "cursor-hider" "${REPO}/src/metixel/__main__.py"; then
    src="${UNIT_SRC_DIR}/metixel-cursor-hider.service"
    dst="/etc/systemd/system/metixel-cursor-hider.service"
    if [ -f "${dst}" ] && cmp -s "${src}" "${dst}"; then
        _same "metixel-cursor-hider.service"
    else
        if [ "${DRY_RUN}" = "yes" ]; then
            printf '      [dry-run] install %s\n' "${dst}"
            CHANGES=$((CHANGES + 1))
        else
            [ -f "${dst}" ] && cp -a "${dst}" "${UNIT_BACKUP_DIR}/"
            install -m 0644 "${src}" "${dst}.metixel-new" && mv -f "${dst}.metixel-new" "${dst}"
            _plus "metixel-cursor-hider.service updated"
            _units_changed=1
        fi
    fi
    _run systemctl enable metixel-cursor-hider.service >/dev/null 2>&1
else
    if [ -f /etc/systemd/system/metixel-cursor-hider.service ]; then
        if [ "${DRY_RUN}" = "yes" ]; then
            printf '      [dry-run] remove stale metixel-cursor-hider.service\n'
            CHANGES=$((CHANGES + 1))
        else
            cp -a /etc/systemd/system/metixel-cursor-hider.service "${UNIT_BACKUP_DIR}/" 2>/dev/null || true
            # --now also STOPS the unit, which needs a running systemd; offline
            # the symlink removal alone is the persistent change (the unit is
            # gone before the image ever boots).
            if _online; then
                systemctl disable --now metixel-cursor-hider.service >/dev/null 2>&1 || true
            else
                systemctl disable metixel-cursor-hider.service >/dev/null 2>&1 || true
            fi
            rm -f /etc/systemd/system/metixel-cursor-hider.service
            _plus "removed stale metixel-cursor-hider.service (mode not shipped)"
            _units_changed=1
        fi
    fi
fi

# metixel-frontend.service was the pre-cage launcher, retired in 1.2.0.
if [ -f /etc/systemd/system/metixel-frontend.service ]; then
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] remove obsolete metixel-frontend.service\n'
        CHANGES=$((CHANGES + 1))
    else
        # See the cursor-hider note above: --now needs running systemd.
        if _online; then
            systemctl disable --now metixel-frontend.service >/dev/null 2>&1 || true
        else
            systemctl disable metixel-frontend.service >/dev/null 2>&1 || true
        fi
        rm -f /etc/systemd/system/metixel-frontend.service
        _plus "removed obsolete metixel-frontend.service"
        _units_changed=1
    fi
fi

if [ "${_units_changed}" -eq 1 ] && [ "${DRY_RUN}" = "no" ]; then
    # daemon-reload only refreshes the RUNNING manager's view.  Offline there is
    # no manager; the units are read fresh when the image first boots.
    if _online; then
        systemctl daemon-reload && _say "systemd reloaded"
    else
        _say "systemd units changed (daemon-reload skipped offline)"
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# 3. I²C / ddcutil (monitor DDC/CI)
# ═══════════════════════════════════════════════════════════════════════════
echo "== I²C / ddcutil =="
# i2c-dev must be loaded for ddcutil to reach the monitor; persist it.
if [ "$(cat /etc/modules-load.d/metixel-i2c.conf 2>/dev/null | tr -d '\n')" = "i2c-dev" ]; then
    _same "/etc/modules-load.d/metixel-i2c.conf"
else
    _ensure_file /etc/modules-load.d/metixel-i2c.conf 0644 <<<'i2c-dev'
fi
# modprobe loads the module into the RUNNING kernel — the build host's, when
# offline.  The modules-load.d file above is the persistent equivalent and is
# already written, so the module loads on the target's next boot.
if _online; then
    _run modprobe i2c-dev >/dev/null 2>&1 || true
else
        _skip_offline "modprobe i2c-dev — persisted via /etc/modules-load.d/metixel-i2c.conf"
fi

# ddcutil persists its cache under $XDG_CACHE_HOME.  The backend unit sets that
# to a writable path inside the data dir (ProtectHome=yes makes /home
# read-only); create it here so it exists even before the service starts.
_ensure_dir "${DATA_DIR}/cache/ddcutil" "pi:pi" recursive

# A legacy cache under the pi user's home is unusable under ProtectHome=yes and
# only produces confusing permission warnings in the journal.
if [ -d /home/pi/.cache/ddcutil ]; then
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] remove unusable legacy cache /home/pi/.cache/ddcutil\n'
        CHANGES=$((CHANGES + 1))
    else
        rm -rf /home/pi/.cache/ddcutil
        _plus "removed unusable legacy cache /home/pi/.cache/ddcutil"
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# 4. Networking: WiFi power saving + 80→8080 redirect
# ═══════════════════════════════════════════════════════════════════════════
echo "== Networking =="
# Pi 3 WiFi is unreliable with power saving on (failed beacons, dropped
# connections) which breaks the captive-portal AP.  NetworkManager's default
# enables it.
_ensure_file /etc/NetworkManager/conf.d/wifi-powersave-off.conf 0644 <<'EOF'
[connection]
wifi.powersave = 2
EOF

# Redirect port 80 → 8080 so the dashboard is reachable without a port number
# and captive-portal detection works.  The Flask app binds 8080 as user pi.
#
# Online this is applied live via iptables and saved by netfilter-persistent.
# Offline iptables would edit the BUILD host's netfilter, so we write the same
# rule into iptables-persistent's rules file instead — the file the target
# replays at boot.
if _online; then
    if iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080 2>/dev/null; then
        _same "iptables 80 → 8080 redirect"
    else
        if [ "${DRY_RUN}" = "yes" ]; then
            printf '      [dry-run] add iptables 80 → 8080 redirect\n'
            CHANGES=$((CHANGES + 1))
        else
            if iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080; then
                _plus "added iptables 80 → 8080 redirect"
                netfilter-persistent save >/dev/null 2>&1 \
                    || _warn "could not persist iptables rule (netfilter-persistent)"
            else
                _fail "could not add iptables 80 → 8080 redirect"
            fi
        fi
    fi
else
    _iptables_redirect_offline
fi

# ═══════════════════════════════════════════════════════════════════════════
# 5. Samba share (media only)
# ═══════════════════════════════════════════════════════════════════════════
# smb.conf is a SHARED, user-editable file — only append Metixel-owned values,
# and only when absent.  Never rewrite the file.
echo "== Samba =="
SMB_CONF="/etc/samba/smb.conf"
if [ -f "${SMB_CONF}" ]; then
    _smb_edit() {
        local desc="$1" pattern="$2" sed_expr="$3"
        if grep -q "${pattern}" "${SMB_CONF}" 2>/dev/null; then
            _same "smb.conf: ${desc}"
            return 0
        fi
        if [ "${DRY_RUN}" = "yes" ]; then
            printf '      [dry-run] smb.conf: add %s\n' "${desc}"
            CHANGES=$((CHANGES + 1))
            return 0
        fi
        sed -i "${sed_expr}" "${SMB_CONF}" && _plus "smb.conf: ${desc}"
    }
    _smb_edit "load printers = no" "load printers = no" '/^\[global\]/a\   load printers = no'
    _smb_edit "disable spoolss = yes" "disable spoolss = yes" '/^\[global\]/a\   disable spoolss = yes'
    # Stop the system 'nobody' user getting an auto-share for /home.
    if [ -n "$(grep -A10 '^\[homes\]' "${SMB_CONF}" 2>/dev/null | grep 'invalid users')" ]; then
        _same "smb.conf: invalid users = nobody"
    elif grep -q '^\[homes\]' "${SMB_CONF}" 2>/dev/null; then
        if [ "${DRY_RUN}" = "yes" ]; then
            printf '      [dry-run] smb.conf: add invalid users = nobody\n'
            CHANGES=$((CHANGES + 1))
        else
            sed -i '/^\[homes\]/a\   invalid users = nobody' "${SMB_CONF}" \
                && _plus "smb.conf: invalid users = nobody"
        fi
    fi
    # The media share itself.  Appended once; never duplicated.
    if grep -q '\[metixel-media\]' "${SMB_CONF}" 2>/dev/null; then
        _same "smb.conf: [metixel-media] share"
    elif [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] smb.conf: append [metixel-media] share\n'
        CHANGES=$((CHANGES + 1))
    else
        tee -a "${SMB_CONF}" >/dev/null <<'SMBEOF'

[metixel-media]
   comment = Metixel Photoframe Media Share
   path = /opt/metixel/data/media
   browseable = yes
   read only = no
   guest ok = no
   valid users = pi
   create mask = 0664
   directory mask = 0775
   force user = pi
   force group = pi
SMBEOF
        _plus "smb.conf: appended [metixel-media] share"
    fi
else
    _warn "smb.conf not found — install samba (see requirements-system.txt)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# 6. Captive-portal AP (hostapd / dnsmasq)
# ═══════════════════════════════════════════════════════════════════════════
# hostapd.conf and dnsmasq.conf are written ONLY when absent.  An existing file
# may have been customised (e.g. a changed AP channel), and clobbering it on
# every update would silently break a working device.  Structural changes to
# these files therefore require a deliberate migration, not this script.
echo "== Captive portal =="
if [ ! -f /etc/hostapd/hostapd.conf ]; then
    _run mkdir -p /etc/hostapd
    _ensure_file /etc/hostapd/hostapd.conf 0600 <<'EOF'
interface=wlan0
driver=nl80211
ssid=Metixel-Setup
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=0
EOF
else
    _same "/etc/hostapd/hostapd.conf (preserved)"
fi

# /etc/default/hostapd is owned by the distro package.  Only *uncomment* the
# DAEMON_CONF line when it is still the stock commented default, so a user's
# own value is never overwritten.
if [ -f /etc/default/hostapd ]; then
    if grep -q '^DAEMON_CONF=' /etc/default/hostapd 2>/dev/null; then
        _same "/etc/default/hostapd DAEMON_CONF"
    elif grep -q '^#DAEMON_CONF=""' /etc/default/hostapd 2>/dev/null; then
        if [ "${DRY_RUN}" = "yes" ]; then
            printf '      [dry-run] enable DAEMON_CONF in /etc/default/hostapd\n'
            CHANGES=$((CHANGES + 1))
        else
            sed -i 's|^#DAEMON_CONF=""|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd \
                && _plus "enabled DAEMON_CONF in /etc/default/hostapd"
        fi
    else
        _warn "/etc/default/hostapd has no DAEMON_CONF line — leaving untouched"
    fi
fi

if [ ! -f /etc/dnsmasq.conf ]; then
    _ensure_file /etc/dnsmasq.conf 0644 <<'EOF'
interface=wlan0
dhcp-range=192.168.42.10,192.168.42.100,12h
dhcp-option=3,192.168.42.1
dhcp-option=6,192.168.42.1
address=/#/192.168.42.1
no-resolv
EOF
else
    _same "/etc/dnsmasq.conf (preserved)"
fi

# The backend's NetworkMonitor starts/stops these explicitly, so they must not
# auto-start.  Ensure they are unmasked + disabled.
#
# `systemctl unmask`/`disable` are symlink operations and work offline.  The
# state is persistent, so the target boots with them disabled either way.
if [ "${DRY_RUN}" = "no" ]; then
    systemctl unmask hostapd dnsmasq >/dev/null 2>&1 || true
    if systemctl is-enabled hostapd >/dev/null 2>&1; then
        systemctl disable hostapd >/dev/null 2>&1 && _plus "disabled hostapd auto-start"
    else
        _same "hostapd auto-start disabled"
    fi
    if systemctl is-enabled dnsmasq >/dev/null 2>&1; then
        systemctl disable dnsmasq >/dev/null 2>&1 && _plus "disabled dnsmasq auto-start"
    else
        _same "dnsmasq auto-start disabled"
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# 7. Session / boot integration
# ═══════════════════════════════════════════════════════════════════════════
echo "== Session =="
# cage needs XDG_RUNTIME_DIR (/run/user/1000) at boot even with no login.
#
# Online, `loginctl enable-linger` does this via logind.  Offline there is no
# logind to talk to, so we write the marker file logind itself reads
# (/var/lib/systemd/linger/<user>) — byte-for-byte the same end state.
if [ "$(loginctl show-user pi -p Linger --value 2>/dev/null)" = "yes" ]; then
    _same "linger enabled for pi"
else
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] loginctl enable-linger pi\n'
        CHANGES=$((CHANGES + 1))
    elif _online; then
        systemctl enable user@1000.service >/dev/null 2>&1 || true
        if _run loginctl enable-linger pi >/dev/null 2>&1; then
            _plus "enabled linger for pi"
        else
            _fail "could not enable linger for pi"
        fi
    else
        # systemd-logind starts on boot anyway; only the linger marker matters.
        systemctl enable user@1000.service >/dev/null 2>&1 || true
        _linger_offline pi
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# 8. Config-driven host state (WiFi regulatory domain, radio, Samba)
# ═══════════════════════════════════════════════════════════════════════════
# The desired value comes from the device's OWN configuration, read here rather
# than passed in by an installer.  That removes the ordering constraint: the
# reconciler does not need the application to have started, and an OTA can
# re-assert these values on a device that is already running.
#
# Source precedence:
#   1. $DATA_DIR/config.json        — authoritative once the app has written it
#   2. $DATA_DIR/init.json          — installer answers, if not yet consumed
#
# Reading init.json is READ-ONLY: the application owns consuming it (it renames
# the file to *.applied and merges the values into config.json).  If this script
# renamed it, the answers would never reach config.json at all.
echo "== Host state (from config) =="

# Extract a dotted path from a JSON file, printing nothing when absent.
_json_get() {
    local file="$1" path="$2"
    [ -f "${file}" ] || return 0
    python3 - "${file}" "${path}" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        node = json.load(fh)
except Exception:
    sys.exit(0)
for part in sys.argv[2].split("."):
    if isinstance(node, dict) and part in node:
        node = node[part]
    else:
        sys.exit(0)
if node is None or isinstance(node, (dict, list)):
    sys.exit(0)
print(node)
PY
}

# Returns the first non-empty value for a dotted path, config.json first.
_desired() {
    local path="$1"
    local value
    value="$(_json_get "${DATA_DIR}/config.json" "${path}")"
    if [ -z "${value}" ]; then
        value="$(_json_get "${DATA_DIR}/init.json" "${path}")"
    fi
    printf '%s' "${value}"
}

# ── WiFi regulatory domain ─────────────────────────────────────────────────
# Two-letter ISO country code (e.g. AU, US, GB), chosen at install or in the
# web UI.  Convergent: re-asserted whenever it drifts, which is what makes it
# survive a re-image or a manual config edit.
WIFI_COUNTRY="$(_desired network.wifi_country)"
if [ -n "${WIFI_COUNTRY}" ]; then
    if [ "${#WIFI_COUNTRY}" -ne 2 ]; then
        _warn "ignoring invalid wifi_country '${WIFI_COUNTRY}' (expected 2 letters)"
    else
        # Runtime: affects the radio immediately (no reboot).  `iw reg set`
        # programs the BUILD host's radio when offline, so it is skipped — the
        # modprobe.d file below is the persistent equivalent the target uses.
        if command -v iw >/dev/null 2>&1; then
            if [ "${DRY_RUN}" = "yes" ]; then
                printf '      [dry-run] iw reg set %s\n' "${WIFI_COUNTRY}"
            elif _online; then
                iw reg set "${WIFI_COUNTRY}" 2>/dev/null \
                    && _plus "set WiFi regulatory domain to ${WIFI_COUNTRY}" \
                    || _warn "iw reg set ${WIFI_COUNTRY} failed"
            else
                _skip_offline "iw reg set ${WIFI_COUNTRY} — persisted via /etc/modprobe.d/cfg80211.conf"
            fi
        fi
        # Persistence: cfg80211 module parameter (takes effect on the next boot).
        CFG80211="/etc/modprobe.d/cfg80211.conf"
        want_regdom="options cfg80211 ieee80211_regdom=${WIFI_COUNTRY}"
        if [ "$(cat "${CFG80211}" 2>/dev/null | tr -d '\n')" = "${want_regdom}" ]; then
            _same "cfg80211 regdom already ${WIFI_COUNTRY}"
        else
            _ensure_file "${CFG80211}" 0644 <<< "${want_regdom}"
        fi
    fi
else
    _say "no network.wifi_country set — skipping regulatory domain"
fi

# ── Radio enablement ───────────────────────────────────────────────────────
# Raspberry Pi Imager can disable WiFi at the OS level when the user skips WiFi
# configuration during imaging.  That leaves a rfkill SOFT block on the wlan
# phy (visible as `nmcli radio` → WIFI-HW=enabled but WIFI=disabled), which
# also makes NetworkManager report wlan0 as "unmanaged".  The result is a
# device that boots with no WiFi and no AP — the exact failure this step
# exists to prevent.  Unblocking is idempotent and harmless when the radio is
# already up, so it is safely convergent.
#
# TWO traps this block handles, both of which silently defeated it before:
#
#   1. PATH.  The `rfkill` binary is package-installed at /usr/sbin/rfkill, and
#      a non-login shell (how update.sh/systemd invoke this) has no /usr/sbin on
#      PATH.  A `command -v rfkill` guard therefore FAILED on a perfectly
#      healthy install, the whole block was skipped, and it printed nothing at
#      all — not even an offline notice.  Resolve the binary explicitly instead.
#
#   2. OFFLINE.  `rfkill unblock` acts on the RUNNING kernel, so on an image
#      build it would touch the BUILD HOST's radio.  The kernel state is not
#      persistable, but the POLICY is: systemd-rfkill keeps the per-device soft
#      block in /var/lib/systemd/rfkill/<device-path>:wlan (1 = blocked,
#      0 = unblocked) and replays it at boot.  Offline we write that file, so a
#      pi-gen image ships unblocked instead of merely warning that it could not
#      help.
RFKILL_BIN=""
for _candidate in /usr/sbin/rfkill /sbin/rfkill /usr/bin/rfkill /bin/rfkill; do
    if [ -x "${_candidate}" ]; then
        RFKILL_BIN="${_candidate}"
        break
    fi
done
# Fall back to PATH for unusual layouts (e.g. a desktop dev machine).
if [ -z "${RFKILL_BIN}" ]; then
    RFKILL_BIN="$(command -v rfkill 2>/dev/null || true)"
fi

# Directory where systemd-rfkill persists per-device soft-block state.  The
# filename is derived from the device's sysfs path with '/' → ':' (e.g.
# platform-1001100000.mmc:wlan — the onboard SDIO WiFi on a Pi).
RFKILL_STATE_DIR="/var/lib/systemd/rfkill"

if [ "${DRY_RUN}" = "yes" ]; then
    printf '      [dry-run] rfkill unblock wifi/wlan + nmcli radio wifi on\n'
elif _online; then
    if [ -z "${RFKILL_BIN}" ]; then
        # Not fatal: a device with no rfkill radio simply has nothing to do.
        _warn "rfkill binary not found (looked in /usr/sbin, /sbin, /usr/bin, /bin, PATH) — radio left as-is"
    else
        # Unblock BOTH type names: `wifi` is an alias that some builds resolve
        # only to the wlan type, while `wlan` is the raw type in sysfs.  Doing
        # both means the step converges on every rfkill version.
        _run "${RFKILL_BIN}" unblock wifi >/dev/null 2>&1 || true
        _run "${RFKILL_BIN}" unblock wlan >/dev/null 2>&1 || true

        # Clearing the rfkill block does NOT by itself re-enable the NetworkManager
        # radio or re-adopt a device that was marked unmanaged while blocked.
        # Both are needed for the radio to actually be usable.
        if command -v nmcli >/dev/null 2>&1; then
            _run nmcli radio wifi on >/dev/null 2>&1 || true
            # wlan0 is the only WiFi interface Metixel supports; naming it keeps
            # this a no-op on Ethernet-only devices (which have no wlan0).
            if ip link show wlan0 >/dev/null 2>&1; then
                _run nmcli device set wlan0 managed yes >/dev/null 2>&1 || true
            fi
        fi
        _same "wifi radio unblocked + enabled"
    fi
else
    # ── Offline: write the persistent form systemd-rfkill replays at boot ──
    # The filename is NOT derived from the sysfs path.  systemd-rfkill uses the
    # udev ID_PATH property, ':'-escaped, plus the rfkill type
    # (src/rfkill/rfkill.c: determine_state_file() → cescape(ID_PATH) + ":" + type):
    #
    #   ID_PATH=platform-1001100000.mmc  +  type=wlan
    #     → /var/lib/systemd/rfkill/platform-1001100000.mmc:wlan
    #
    # Using the sysfs path instead yields a long, wrong name
    # (platform:axi:1001100000.mmc:mmc_host:…:rfkill1:wlan) that systemd never
    # reads — the unblock would appear to succeed and silently do nothing.
    # udevadm is the only reliable source of ID_PATH.  If it is unavailable we
    # report rather than guess, because a wrong filename writes a dead file.
    _offline_target=""
    _offline_why=""
    if ! command -v udevadm >/dev/null 2>&1; then
        _offline_why="udevadm not available to resolve ID_PATH"
    elif [ ! -d /sys/class/rfkill ]; then
        _offline_why="no rfkill devices present"
    else
        for _rf in /sys/class/rfkill/*; do
            [ -e "${_rf}" ] || continue
            [ "$(cat "${_rf}/type" 2>/dev/null)" = "wlan" ] || continue
            _id_path="$(udevadm info --query=property --path="${_rf}" 2>/dev/null \
                | sed -n 's/^ID_PATH=//p')"
            if [ -z "${_id_path}" ]; then
                _offline_why="wlan device has no ID_PATH"
                continue
            fi
            # systemd cescape()s the property.  ID_PATH contains no ':' in
            # practice (it is '-' separated), so this is a no-op on a Pi, but
            # keep the escaping so the name matches systemd exactly if it ever
            # does contain one.
            _escaped="$(printf '%s' "${_id_path}" | sed 's/:/\\x3a/g')"
            _offline_target="${RFKILL_STATE_DIR}/${_escaped}:wlan"
        done
    fi

    if [ -n "${_offline_target}" ]; then
        if [ -f "${_offline_target}" ] && [ "$(cat "${_offline_target}" 2>/dev/null)" = "0" ]; then
            _same "rfkill state already unblocked ($(basename "${_offline_target}"))"
        elif mkdir -p "${RFKILL_STATE_DIR}" \
            && printf '0' > "${_offline_target}"; then
            _plus "rfkill state unblocked ($(basename "${_offline_target}"))"
        else
            _warn "could not write ${_offline_target} — radio may come up blocked"
        fi
    else
        _skip_offline "rfkill — ${_offline_why:-no wlan device found to unblock}"
    fi
fi

# ── Samba service + share credentials ──────────────────────────────────────
# The share DEFINITION is reconciled in §5.  Here we ensure the service is
# enabled and running, and that the `pi` user has a passdb entry — the web UI's
# device-password feature keeps /etc/shadow and Samba in sync, so on an existing
# device the credential already exists and this is a no-op.
if [ -f /etc/samba/smb.conf ] || command -v smbd >/dev/null 2>&1; then
    if [ "${DRY_RUN}" = "yes" ]; then
        printf '      [dry-run] ensure smbd enabled + running\n'
    else
        if systemctl is-enabled smbd >/dev/null 2>&1; then
            _same "smbd enabled"
        else
            systemctl enable smbd >/dev/null 2>&1 && _plus "enabled smbd"
        fi
        # `restart` needs a running systemd; offline the enable above is the
        # persistent change and the target starts smbd on its next boot.
        if _online; then
            if systemctl is-active --quiet smbd; then
                _same "smbd running"
            else
                systemctl restart smbd >/dev/null 2>&1 && _plus "started smbd"
            fi
        else
            _skip_offline "smbd restart — smbd starts at boot"
        fi
    fi
    # Only seed the credential when Samba has NO entry for pi, so a password the
    # user set via the web UI is never reset.  `pdbedit -L` is the cheap check.
    if command -v pdbedit >/dev/null 2>&1; then
        if pdbedit -L 2>/dev/null | grep -q '^pi:'; then
            _same "samba account for pi exists"
        else
            if [ "${DRY_RUN}" = "yes" ]; then
                printf '      [dry-run] create samba account for pi (default password)\n'
                CHANGES=$((CHANGES + 1))
            else
                # Default credential for a fresh device; change it via the web
                # UI (System → Security), which keeps shadow + Samba in sync.
                # smbpasswd writes the passdb directly and needs no running
                # smbd, so it works offline.  Bounded by a timeout because it
                # runs under QEMU emulation during an image build — a hang there
                # would stall the whole build rather than fail it.
                smb_out=""
                if [ "${OFFLINE}" = "yes" ]; then
                    smb_out="$(printf 'raspberry\nraspberry\n' \
                        | timeout 60 smbpasswd -a -s pi 2>&1)" || smb_out=""
                else
                    smb_out="$(printf 'raspberry\nraspberry\n' \
                        | smbpasswd -a -s pi 2>&1)" || smb_out=""
                fi
                if pdbedit -L 2>/dev/null | grep -q '^pi:'; then
                    _plus "created samba account for pi (default password)"
                else
                    _warn "could not create samba account for pi${smb_out:+ (${smb_out})}"
                fi
            fi
        fi
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# 9. Boot config — DELIBERATELY NOT HANDLED HERE
# ═══════════════════════════════════════════════════════════════════════════
# /boot/firmware/config.txt is excluded from reconciliation on purpose:
#   * it is the DEVICE's file — re-asserting gpu_mem on every update would
#     silently override a value the user deliberately chose; and
#   * a change only takes effect after a REBOOT, so an unrelated update would
#     schedule a behaviour change that manifests later.
# It is applied at provisioning and by the one-time v1.2.1-gpu-mem.sh fixup,
# both of which call the shared scripts/configure_boot.sh.

# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════
echo ""
if [ "${DRY_RUN}" = "yes" ]; then
    echo "=== reconcile.sh dry-run: ${CHANGES} change(s) would be applied ==="
    exit 0
fi
if [ "${FAILURES}" -gt 0 ]; then
    echo "=== reconcile.sh finished with ${FAILURES} failure(s) (${CHANGES} change(s) applied) ==="
    exit 1
fi
if [ "${CHANGES}" -eq 0 ]; then
    echo "=== reconcile.sh${OFFLINE_SUFFIX}: host already converged (no changes) ==="
else
    echo "=== reconcile.sh${OFFLINE_SUFFIX}: ${CHANGES} change(s) applied ==="
fi
if [ "${NEEDS_REBOOT}" = "yes" ]; then
    echo "REBOOT_REQUIRED: boot config changed; reboot to apply"
fi
exit 0
