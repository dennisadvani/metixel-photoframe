#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# Push the working tree's changed source files straight onto a frame, for
# testing a branch before it is committed or released.
#
# WHY THIS EXISTS
# ---------------
# The real deployment path (scripts/update.sh) clones a tagged release from
# GitHub, so it can only install work that has been committed AND pushed.
# While iterating on a branch locally that is too slow a loop — and pushing to
# GitHub just to test is worse.  The services import straight from the live
# release's tree (metixel-backend.service sets
# PYTHONPATH=/opt/metixel/live/src and the package is installed with
# `pip install -e`), so copying changed files into /opt/metixel/live and
# restarting the backend is enough to run them.
#
# WHAT IT DOES NOT DO
# -------------------
# This is NOT an upgrade: it does not touch systemd units, the venv, apt
# packages, or /opt/metixel/data.  It only overwrites files inside the current
# release directory.  The next real OTA update installs a fresh clone into a new
# release directory, which silently reverts everything this script did — that is
# the intended way to undo it.
#
# Only files the frame runs are sent — everything this branch changes under
# src/ and scripts/ relative to BASE (the release the frame is running, v1.2.6
# by default), plus anything still uncommitted, minus scripts/dev/.  The set therefore stays correct as the branch
# evolves, whether or not the work is committed yet.
#
# PASSWORDS
# ---------
# Every remote step shares ONE ssh connection (ControlMaster multiplexing), and
# the privileged half runs in a single `sudo bash` on the far end, so the
# password is asked for once per run instead of once per command.  Run
# `scripts/dev/sync_to_pi.sh --setup-keys` once to install an ssh key and stop
# being asked at all.
#
# Usage:
#     scripts/dev/sync_to_pi.sh                 # 192.168.86.237, restart backend
#     PI=metixel scripts/dev/sync_to_pi.sh      # another host
#     BASE=v1.2.5 scripts/dev/sync_to_pi.sh     # frame is on an older release
#     scripts/dev/sync_to_pi.sh --list          # show what would be sent
#     scripts/dev/sync_to_pi.sh --no-restart    # copy only
#     scripts/dev/sync_to_pi.sh --setup-keys    # install an ssh key, then exit

set -euo pipefail

PI="${PI:-192.168.86.237}"
#: The release the frame is running — the diff base for "what to send".
BASE="${BASE:-v1.2.6}"
PI_USER="${PI_USER:-pi}"
LIVE="${LIVE:-/opt/metixel/live}"
STAGE="/tmp/metixel-dev-sync"

LIST_ONLY=0
RESTART=1
SETUP_KEYS=0
for arg in "$@"; do
    case "$arg" in
        --list) LIST_ONLY=1 ;;
        --no-restart) RESTART=0 ;;
        --setup-keys) SETUP_KEYS=1 ;;
        -h | --help)
            sed -n '5,34p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

# ── One ssh connection for the whole run ─────────────────────────────────
# Without this, each ssh/rsync authenticates separately and password auth
# prompts once per command.  A ControlMaster socket is opened once (the single
# prompt), every later command rides it, and the trap closes it on the way out.
CONTROL_SOCK="${TMPDIR:-/tmp}/metixel-sync-$$.sock"
SSH_OPTS=(-o "ControlPath=${CONTROL_SOCK}")
MASTER_UP=0

_close_master() {
    if [ "$MASTER_UP" -eq 1 ]; then
        ssh "${SSH_OPTS[@]}" -O exit "${PI_USER}@${PI}" 2>/dev/null || true
        MASTER_UP=0
    fi
}
trap _close_master EXIT INT TERM

_open_master() {
    echo "→ Connecting to ${PI_USER}@${PI} (one password for the whole run)…"
    if ssh "${SSH_OPTS[@]}" -o ControlMaster=yes -o ControlPersist=120 \
        -fN "${PI_USER}@${PI}"; then
        MASTER_UP=1
    else
        # Multiplexing unavailable (or auth failed) — carry on with plain ssh
        # so the script still works, just with more prompts.
        echo "   (could not open a shared connection — expect a prompt per step)" >&2
        SSH_OPTS=()
    fi
}

if [ "$SETUP_KEYS" -eq 1 ]; then
    # One password now, none afterwards.  Only touches ~/.ssh on both ends.
    [ -f "${HOME}/.ssh/id_ed25519" ] || ssh-keygen -t ed25519 -N "" -f "${HOME}/.ssh/id_ed25519"
    ssh-copy-id -i "${HOME}/.ssh/id_ed25519.pub" "${PI_USER}@${PI}"
    echo "Key installed — future runs should not ask for a password."
    exit 0
fi

cd "$(dirname "$0")/../.."

git rev-parse --verify --quiet "$BASE" >/dev/null ||
    {
        echo "BASE ref '$BASE' not found — fetch the tag, or set BASE=<ref>." >&2
        exit 1
    }

# What this branch changes versus the running release, plus anything still
# uncommitted.  `git status --porcelain` prints "XY path"; the sed keeps the
# path (none of these paths contain spaces).
#
# scripts/dev/ is excluded: it is tooling that runs on the developer's machine
# (this script included), never on the frame.
mapfile -t FILES < <(
    {
        git diff --name-only "${BASE}...HEAD" -- src scripts
        git status --porcelain -- src scripts | sed 's/^...//'
    } | grep -v '^scripts/dev/' | sort -u
)

if [ "${#FILES[@]}" -eq 0 ]; then
    echo "Nothing changed under src/ or scripts/ — nothing to send."
    exit 0
fi

echo "Files to send to ${PI_USER}@${PI}:${LIVE}/ :"
for f in "${FILES[@]}"; do
    [ -e "$f" ] && echo "  $f" || echo "  $f (deleted locally — NOT removed on the frame)"
done

if [ "$LIST_ONLY" -eq 1 ]; then
    exit 0
fi

# Only send files that still exist; a local delete is deliberately not mirrored
# (removing files from a release is what a real update is for).
EXISTING=()
for f in "${FILES[@]}"; do
    [ -e "$f" ] && EXISTING+=("$f")
done

echo
_open_master

echo "→ Staging in ${STAGE} on the frame…"
ssh "${SSH_OPTS[@]}" "${PI_USER}@${PI}" "rm -rf ${STAGE} && mkdir -p ${STAGE}"

# --relative keeps each file's path under the staging dir.  -e reuses the
# shared connection rather than opening (and authenticating) a second one.
rsync -az --relative --human-readable \
    -e "ssh ${SSH_OPTS[*]}" \
    "${EXISTING[@]}" "${PI_USER}@${PI}:${STAGE}/"

# Both privileged steps in ONE sudo so it is asked for at most once (and not
# at all where pi has passwordless sudo).  `sudo -p` labels the prompt, since
# an unexplained "[sudo] password" halfway through a script is confusing.
REMOTE_CMD="sudo -p '[sudo on ${PI}] password for %u: ' bash -s"
REMOTE_SCRIPT="set -e
rsync -a ${STAGE}/ ${LIVE}/
rm -rf ${STAGE}"
if [ "$RESTART" -eq 1 ]; then
    REMOTE_SCRIPT="${REMOTE_SCRIPT}
systemctl restart metixel-backend.service"
    echo "→ Copying into ${LIVE} and restarting metixel-backend…"
else
    echo "→ Copying into ${LIVE}…"
fi

printf '%s\n' "$REMOTE_SCRIPT" | ssh "${SSH_OPTS[@]}" -t "${PI_USER}@${PI}" "$REMOTE_CMD"

_close_master

if [ "$RESTART" -eq 1 ]; then
    sleep 3
    echo "→ Health:"
    curl -sS --max-time 10 "http://${PI}:8080/api/health" |
        python3 -c 'import json,sys; d=json.load(sys.stdin); print("   healthy:", d.get("healthy"), "| unhealthy:", d.get("unhealthy_checks"))' ||
        echo "   (could not read /api/health yet — give it a few seconds)"
else
    echo "→ Skipped the restart; run: sudo systemctl restart metixel-backend.service"
fi

echo
echo "Done.  Open http://${PI}/#sources and hard-refresh the browser (the JS"
echo "cache-buster changed, but a stale service worker/cache can still bite)."
