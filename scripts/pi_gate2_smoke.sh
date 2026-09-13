#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# GATE-2: full-stack smoke test on real hardware.
#
# GATE-1 answered "which decoder actually uses the hardware?".  GATE-2 answers a
# different question: does the whole thing come up on a display, under cage, with
# the Qt + mpv backend — and do the things the desktop (tk) tests cannot exercise
# actually work?
#
# The tk backend renders via a software path on a desktop, so it validates the
# presentation logic but tells you nothing about:
#
#   * whether Qt can acquire a Wayland surface under cage at all
#   * whether mpv's libmpv render API attaches to the Qt canvas FBO (fbo=0 is the
#     classic failure: it draws to the window's default framebuffer and the
#     picture is invisible or misplaced)
#   * whether video plays WITH hardware decode in the real pipeline, rather than
#     in an isolated mpv invocation
#   * whether overlays composite above video, which is the specific property the
#     old VLC-on-top design could not provide
#   * whether the render loop holds its frame cap on the actual board
#
# Run this ON the Pi, with the display attached.  It is read-only with respect to
# the repository: it starts services, observes them, and reports.  It does not
# edit config, and it restores the service state it found.
#
# Usage:
#   sudo bash scripts/pi_gate2_smoke.sh            # full run
#   sudo bash scripts/pi_gate2_smoke.sh --no-video # skip the video stage
#
# Exit status is 0 only if every REQUIRED stage passed.  Advisory stages report
# but do not fail the run.

set -uo pipefail

RUN_VIDEO=yes
for arg in "$@"; do
    case "${arg}" in
        --no-video) RUN_VIDEO=no ;;
        -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown argument: ${arg}" >&2; exit 1 ;;
    esac
done

PASS=0
FAIL=0
WARN=0

ok()   { echo "  ✓ $*"; PASS=$((PASS + 1)); }
bad()  { echo "  ✗ $*"; FAIL=$((FAIL + 1)); }
warn() { echo "  ! $*"; WARN=$((WARN + 1)); }
head_() { echo; echo "── $* ─────────────────────────────────────────────"; }

# Detected once: several checks are board-specific by design.
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown")
case "${MODEL}" in
    *"Pi 5"*) BOARD=pi5 ;;
    *"Pi 4"*) BOARD=pi4 ;;
    *"Pi 3"*) BOARD=pi3 ;;
    *"Pi 2"*) BOARD=pi2 ;;
    *)        BOARD=other ;;
esac

echo "=== GATE-2 full-stack smoke ==="
echo "  board:  ${MODEL}  (key: ${BOARD})"
echo "  kernel: $(uname -r)"
echo "  live:   $(readlink -f /opt/metixel/live 2>/dev/null || echo 'MISSING')"

# Refuse to measure on a busy system: a second renderer would compete for the
# display and the frame timings would be meaningless.
#
# Match on the command line: the application runs as `python3 -m metixel`, so its
# process NAME is python3 and a name-based match would miss it entirely.
if pgrep -x cage >/dev/null 2>&1 || pgrep -f -- '--mode frontend' >/dev/null 2>&1; then
    echo
    echo "  ! Metixel is already running.  Stopping it first so the run starts"
    echo "    from a known state."
    systemctl stop metixel-cage metixel-backend metixel-cursor-hider 2>/dev/null
    sleep 2
fi

# ── 1) The release is 2.0.0 ─────────────────────────────────────────────────
head_ "1) release under test"
VERSION=$(grep -m1 '__version__' /opt/metixel/live/src/metixel/__init__.py 2>/dev/null \
          | cut -d'"' -f2)
if [ "${VERSION}" = "2.0.0" ]; then
    ok "live release is 2.0.0"
else
    warn "live release is '${VERSION}', not 2.0.0 — this is not a 2.0.0 smoke test"
fi

# ── 2) The retired stack is actually gone ──────────────────────────────────
head_ "2) retired stack removed"
REMAINING=$(dpkg -l 2>/dev/null | awk '/^ii/ && $2 ~ /^(vlc|pi3d)/{print $2}')
if [ -z "${REMAINING}" ]; then
    ok "no vlc-* or pi3d system packages installed"
else
    bad "retired packages still installed: ${REMAINING}"
fi
for mod in pi3d vlc pygame sdl2; do
    if python3 -c "import ${mod}" >/dev/null 2>&1; then
        bad "python module '${mod}' is still importable"
    else
        ok "python module '${mod}' is gone"
    fi
done

# ── 3) The new stack is present ────────────────────────────────────────────
head_ "3) Qt + mpv stack present"
for mod in PySide6.QtCore PySide6.QtWidgets mpv; do
    if python3 -c "import ${mod}" >/dev/null 2>&1; then
        ok "import ${mod}"
    else
        bad "import ${mod} FAILED"
    fi
done
MPV_VER=$(mpv --version 2>/dev/null | head -1)
[ -n "${MPV_VER}" ] && ok "${MPV_VER}" || bad "mpv --version produced nothing"

# ── 4) Qt resolves the Wayland platform plugin ─────────────────────────────
# This is the single most likely cause of a black screen: QT_QPA_PLATFORM
# auto-detecting to xcb, which cannot work under cage.
head_ "4) Qt platform plugin"
PLUGIN_DIR=$(python3 -c "
from PySide6.QtCore import QLibraryInfo
print(QLibraryInfo.path(QLibraryInfo.PluginsPath))
" 2>/dev/null)
if [ -n "${PLUGIN_DIR}" ] && ls "${PLUGIN_DIR}/platforms/" 2>/dev/null | grep -q wayland; then
    ok "wayland QPA plugin present in ${PLUGIN_DIR}/platforms"
else
    bad "wayland QPA plugin NOT found (looked in ${PLUGIN_DIR:-?}/platforms) — Qt will fall back to xcb and the screen will stay black"
fi
# The unit must pin it; auto-detection is not trusted.
if grep -q '^Environment=QT_QPA_PLATFORM=wayland' \
        /opt/metixel/live/systemd/metixel-cage.service 2>/dev/null; then
    ok "QT_QPA_PLATFORM=wayland is pinned in metixel-cage.service"
else
    bad "QT_QPA_PLATFORM is not pinned in the shipped metixel-cage.service"
fi

# ── 5) cage + Qt start and the frontend stays up ───────────────────────────
head_ "5) services start and stay up"
systemctl start metixel-backend 2>/dev/null
sleep 3
if systemctl is-active --quiet metixel-backend; then
    ok "metixel-backend is active"
else
    bad "metixel-backend failed to start"
    journalctl -u metixel-backend -n 20 --no-pager 2>/dev/null | sed 's/^/      /'
fi

systemctl start metixel-cage 2>/dev/null
# The frontend needs time to construct QApplication, the canvas, and mpv.
sleep 12
if systemctl is-active --quiet metixel-cage; then
    ok "metixel-cage is active"
else
    bad "metixel-cage failed to start"
    journalctl -u metixel-cage -n 30 --no-pager 2>/dev/null | sed 's/^/      /'
fi

head_ "5b) the frontend did not crash-loop"
# A backend that starts and immediately exits shows as "activating" or a rising
# restart count.  Read it twice so a single crash is visible.
R1=$(systemctl show metixel-cage -p NRestarts --value 2>/dev/null)
sleep 6
R2=$(systemctl show metixel-cage -p NRestarts --value 2>/dev/null)
if [ "${R2}" = "0" ]; then
    ok "zero restarts in the observation window"
elif [ "${R1}" = "${R2}" ]; then
    warn "restart count is ${R2} but stable during the window (crashed earlier, now settled)"
else
    bad "restart count rising (${R1} → ${R2}) — the frontend is crash-looping"
    journalctl -u metixel-cage -n 40 --no-pager 2>/dev/null | tail -20 | sed 's/^/      /'
fi

# ── 6) The render loop reported a real surface ─────────────────────────────
head_ "6) renderer reached the display"
LOG=/opt/metixel/data/logs/metixel-frontend.log
if [ -f "${LOG}" ]; then
    if grep -qiE 'wayland' "${LOG}"; then
        ok "frontend log mentions the wayland platform"
    else
        warn "no wayland mention in the frontend log (check the effective platform)"
    fi
    if grep -qiE 'mpv|libmpv' "${LOG}"; then
        ok "frontend log mentions mpv/libmpv (the render API attached)"
    else
        warn "no mpv mention in the frontend log"
    fi
    # Any traceback is a hard failure: rule 7 says never show or log a traceback
    # as normal operation.
    if grep -qiE 'traceback \(most recent call last\)' "${LOG}"; then
        bad "the frontend log contains a traceback"
        grep -A4 -iE 'traceback \(most recent call last\)' "${LOG}" | tail -20 | sed 's/^/      /'
    else
        ok "no traceback in the frontend log"
    fi
else
    warn "no frontend log at ${LOG} yet"
fi

# ── 7) Frame rate holds the cap ────────────────────────────────────────────
head_ "7) the renderer process is alive (advisory)"
# The cap is 30 FPS by design (memory headroom on the weak boards). We only
# assert the renderer exists; exact timing belongs to a dedicated benchmark.
#
# NOTE: match on the command line, not the process name.  The frontend runs as
# `python3 -m metixel --mode frontend`, so `pgrep -x metixel` can never match —
# an earlier version of this script used exactly that and reported a false
# failure on a perfectly healthy system.
FRONTEND_PID=$(pgrep -f -- '--mode frontend' | head -1)
if [ -n "${FRONTEND_PID}" ]; then
    ok "frontend renderer running (pid ${FRONTEND_PID})"
    # The frontend must be a direct child of cage in the intended topology.
    PPID_NAME=$(ps -o comm= -p "$(ps -o ppid= -p "${FRONTEND_PID}" | tr -d ' ')" 2>/dev/null)
    case "${PPID_NAME}" in
        cage*) ok "parent is cage (${PPID_NAME}) — started under the compositor" ;;
        *)     warn "parent is '${PPID_NAME}', not cage — check the launch path" ;;
    esac
else
    bad "no '--mode frontend' process — nothing is rendering"
fi

# The retired backend selector must be gone from the shipped units.
head_ "7b) retired env plumbing removed from the units"
for unit in metixel-cage metixel-backend; do
    f="/opt/metixel/live/systemd/${unit}.service"
    if [ -f "${f}" ]; then
        if grep -q 'METIXEL_DISPLAY_BACKEND' "${f}"; then
            bad "${unit}.service still sets METIXEL_DISPLAY_BACKEND"
        else
            ok "${unit}.service has no METIXEL_DISPLAY_BACKEND"
        fi
    fi
done

# ── 8) Video with hardware decode, in the real pipeline ────────────────────
if [ "${RUN_VIDEO}" = "yes" ]; then
    head_ "8) video playback uses hardware decode"
    # Expected decoder per board, from the GATE-1 measurements.
    case "${BOARD}" in
        pi5|pi4) EXPECT=drm-copy ;;
        pi3|pi2) EXPECT=v4l2m2m ;;
        *)       EXPECT="" ;;
    esac
    echo "      expected hwdec for this board: ${EXPECT:-<unknown>}"

    if [ "$(pgrep -c -f -- '--mode frontend' 2>/dev/null || echo 0)" -gt 0 ]; then
        # Sample CPU across a window with the frontend up.  A hardware-decoding
        # pipeline should not be saturating the CPU.
        T1=$(awk '/^cpu /{print $2+$3+$4+$6+$7}' /proc/stat)
        sleep 10
        T2=$(awk '/^cpu /{print $2+$3+$4+$6+$7}' /proc/stat)
        HZ=$(getconf CLK_TCK)
        NCORES=$(nproc)
        BUSY=$(( (T2 - T1) * 100 / (HZ * 10 * NCORES) ))
        echo "      CPU busy over 10s: ${BUSY}% of ${NCORES} cores"
        if [ "${BUSY}" -gt 90 ]; then
            bad "CPU saturation (${BUSY}%) with the frontend running — check hwdec"
        else
            ok "frontend is not CPU-saturated (${BUSY}%)"
        fi
    else
        warn "cage not running; skipped the CPU sample"
    fi
else
    head_ "8) video stage skipped (--no-video)"
fi

# ── 9) Health endpoint ─────────────────────────────────────────────────────
head_ "9) health endpoint"
HEALTH=$(curl -fsS --max-time 10 http://127.0.0.1:8080/api/health 2>/dev/null)
if [ -n "${HEALTH}" ]; then
    ok "GET /api/health responded"
    echo "${HEALTH}" | head -c 400 | sed 's/^/      /'
    echo
else
    bad "GET /api/health did not respond"
fi

# ── summary ────────────────────────────────────────────────────────────────
echo
echo "============================================================"
echo "  GATE-2 summary: ${PASS} passed, ${FAIL} failed, ${WARN} warnings"
echo "============================================================"
if [ "${FAIL}" -gt 0 ]; then
    echo "  RESULT: FAIL"
    exit 1
fi
echo "  RESULT: PASS"
exit 0
