#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
# =============================================================================
# Metixel Chromium Kiosk Prototype — On-Pi benchmark harness.
#
# Copies the prototype to a target Pi, starts the stdlib server, launches
# chromium in kiosk mode fullscreen, samples /proc CPU/mem while the
# slideshow runs, then collects the FPS results.
#
# Usage:
#   bash scripts/chromium_prototype/run_on_pi.sh --user pi --ip 192.168.222.122
#
# The prototype is copied to /tmp/metixel-chromium-proto on the Pi. The
# kiosk points at the server on the Pi itself (http://localhost:8000).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_DIR="/tmp/metixel-chromium-proto"
REMOTE_MEDIA="/opt/metixel/data/media/sample_media"   # already on the Pi
DURATION_SEC="${DURATION_SEC:-60}"        # how long to run the kiosk
PORT="${PORT:-8000}"

# ── Parse args ────────────────────────────────────────────────────────────
USER="pi"
IP=""
IDENTITY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) USER="$2"; shift 2 ;;
        --ip)   IP="$2";   shift 2 ;;
        --host) IP="$2";   shift 2 ;;
        --identity) IDENTITY="$2"; shift 2 ;;
        --duration) DURATION_SEC="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$IP" ]]; then
    echo "ERROR: --ip is required" >&2
    exit 1
fi

TARGET="${USER}@${IP}"
# Use an explicit identity file if provided (e.g. ~/.ssh/id_ed25519). This
# matters when the target IP is not in ~/.ssh/config, so ssh would otherwise
# fall back to default key discovery and prompt for a password.
SSH_OPTS=""
if [[ -n "$IDENTITY" ]]; then
    SSH_OPTS="-i ${IDENTITY}"
fi
SSH="ssh ${SSH_OPTS} ${TARGET}"
SCP="scp ${SSH_OPTS} ${TARGET}:"

echo "=== Metixel Chromium Kiosk Prototype — On-Pi benchmark ==="
echo "Target: ${TARGET}"
echo "Remote dir: ${REMOTE_DIR}"
echo "Media: ${REMOTE_MEDIA}"
echo "Duration: ${DURATION_SEC}s"

# ── 1. Copy the prototype to the Pi ───────────────────────────────────────
echo ""
echo "==> Copying prototype to ${TARGET}:${REMOTE_DIR}"
${SSH} "rm -rf ${REMOTE_DIR} && mkdir -p ${REMOTE_DIR}"
${SCP} "${SCRIPT_DIR}/server.py" "${SCRIPT_DIR}/index.html" \
    "${SCRIPT_DIR}/style.css" "${SCRIPT_DIR}/app.js" \
    "${SCRIPT_DIR}/benchmark.js" "${SCRIPT_DIR}/sampler.py" \
    "${TARGET}:${REMOTE_DIR}/"

# ── 2. Check chromium is installed ────────────────────────────────────────
echo ""
echo "==> Checking chromium on the Pi"
if ! ${SSH} "command -v chromium-browser || command -v chromium"; then
    echo "ERROR: chromium not found on the Pi." >&2
    echo "Install it first, e.g.:  sudo apt install chromium" >&2
    exit 1
fi

# ── 3. Start the server on the Pi ─────────────────────────────────────────
echo ""
echo "==> Starting prototype server on the Pi (port ${PORT})"
${SSH} "pkill -f 'server.py' 2>/dev/null || true; \
    cd ${REMOTE_DIR} && \
    nohup python3 server.py --port ${PORT} --media ${REMOTE_MEDIA} \
    > ${REMOTE_DIR}/server.log 2>&1 &"
sleep 2

# ── 4. Start the /proc sampler in the background ──────────────────────────
echo ""
echo "==> Starting /proc sampler (${DURATION_SEC}s)"
${SSH} "cd ${REMOTE_DIR} && \
    nohup python3 sampler.py --duration ${DURATION_SEC} \
    --out ${REMOTE_DIR}/cpu_mem.json > ${REMOTE_DIR}/sampler.log 2>&1 &"

# ── 5. Launch chromium kiosk ──────────────────────────────────────────────
echo ""
echo "==> Launching chromium kiosk (${DURATION_SEC}s)"
# --kiosk: fullscreen kiosk mode
# --noerrdialogs / --disable-infobars: no dialogs
# --autoplay-policy=no-user-gesture-required: allow <video> to autoplay muted
# --disable-session-crashed-bubble: no crash bubble
# --check-for-update-interval=31536000: don't nag about updates
${SSH} "cd ${REMOTE_DIR} && \
    timeout ${DURATION_SEC} chromium-browser --kiosk \
    --noerrdialogs --disable-infobars \
    --autoplay-policy=no-user-gesture-required \
    --disable-session-crashed-bubble \
    --check-for-update-interval=31536000 \
    --disable-features=Translate \
    http://localhost:${PORT}/ \
    > ${REMOTE_DIR}/chromium.log 2>&1 || true"

echo ""
echo "==> Kiosk run complete (${DURATION_SEC}s)"

# ── 6. Collect results ────────────────────────────────────────────────────
echo ""
echo "==> Collecting results"
mkdir -p "${SCRIPT_DIR}/out"
${SCP} "${TARGET}:${REMOTE_DIR}/cpu_mem.json" "${SCRIPT_DIR}/out/cpu_mem.json"
${SCP} "${TARGET}:${REMOTE_DIR}/benchmark_results.json" \
    "${SCRIPT_DIR}/out/benchmark_results.json" 2>/dev/null || true

echo ""
echo "==> Done. Results in ${SCRIPT_DIR}/out/"
echo "    - out/cpu_mem.json          (CPU/mem samples)"
echo "    - out/benchmark_results.json (FPS samples)"
echo ""
echo "    Summarise with:"
echo "    python scripts/chromium_prototype/measure.py \\"
echo "        --cpu-mem scripts/chromium_prototype/out/cpu_mem.json \\"
echo "        --fps scripts/chromium_prototype/out/benchmark_results.json"