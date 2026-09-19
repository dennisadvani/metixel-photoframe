#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# Runner for _spike_sw_render_fds.py — the libmpv SOFTWARE render-API leak probe.
#
# Lints the spike, copies it to the Pi, and runs it there under a transient
# systemd unit.  The unit is required rather than optional: an SSH session does
# not carry the `video`/`render` supplementary groups, so without them any
# hardware-decode path fails to open its device and the run silently measures
# software decode instead — exactly the class of mix-up that makes a spike
# result untrustworthy.
#
# Unlike the Qt spikes this does NOT stop `metixel-cage`: the probe imports no Qt
# and opens no window (the software render API writes into host memory), so the
# compositor is irrelevant to it and the running frame keeps working.
#
# Configuration is via environment variables so nothing has to survive shell
# quoting on the way through ssh:
#
#   HWDEC   hwdec mode            (default: drm-copy — the Pi 5's copy-back path)
#   SIZE    target buffer WxH     (default: 1280x800)
#   SECS    how long to draw for  (default: 16)
#   FPS     target draw rate      (default: 30)
#
# Usage:
#   scripts/dev/_run_sw_spike.sh [pi-host] [video-path-on-pi]

set -euo pipefail

PI_HOST="${1:-192.168.222.122}"
VIDEO="${2:-/opt/metixel/data/cache/videos/2cd3ac6ece47670f.mp4}"

HWDEC="${HWDEC:-drm-copy}"
SIZE="${SIZE:-1280x800}"
SECS="${SECS:-16}"
FPS="${FPS:-30}"
# Buffer format.  rgb0 (4 B/px) is the baseline every measurement used; rgb565
# (2 B/px) is the write-bandwidth reducer and matches the GL_RGB565 texture the
# production backend is meant to use.
FORMAT="${FORMAT:-rgb0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PI_USER="${PI_USER:-pi}"
REMOTE="/tmp/_spike_sw_render_fds.py"
TARGET="${PI_USER}@${PI_HOST}"

echo "== 1/4 lint =="
"${REPO_ROOT}/.venv/bin/python" -m ruff check "${SCRIPT_DIR}/_spike_sw_render_fds.py"
"${REPO_ROOT}/.venv/bin/python" -m ruff format --check "${SCRIPT_DIR}/_spike_sw_render_fds.py"

echo "== 2/4 copy =="
scp -o BatchMode=yes -o ConnectTimeout=10 -q \
    "${SCRIPT_DIR}/_spike_sw_render_fds.py" "${TARGET}:${REMOTE}"
echo "  copied to ${REMOTE}"

echo "== 3/4 run =="
# The remote command is a single-quoted heredoc-free string; no local heredoc is
# piped into ssh, which is known to wedge the local terminal.  It is additionally
# bounded by `timeout`: a transient unit that wedges in `deactivating` (observed
# with a cage-based probe) leaves `systemd-run --wait` blocking for ever, and
# without this the local shell would never get its prompt back.
timeout 180 ssh -o BatchMode=yes -o ConnectTimeout=15 "${TARGET}" "
set -u
echo '--- gate: does the shipped python-mpv know the sw_* render params? ---'
python3 -c 'import mpv; print(sorted(mpv.MpvRenderParam.TYPES))'
echo '--- gate: is the probe file the one we just sent? ---'
sha256sum ${REMOTE} | cut -c1-16
echo '--- gate: does mpv report any libmpv/sw render support? ---'
mpv --version 2>/dev/null | head -1
echo
echo '--- spike: api_type=sw hwdec=${HWDEC} size=${SIZE} format=${FORMAT} ---'
sudo -n systemctl reset-failed metixel-swspike 2>/dev/null || true
sudo -n systemd-run --unit=metixel-swspike --collect --pipe --wait \
  --uid=${PI_USER} --gid=${PI_USER} \
  --property=SupplementaryGroups='video render input tty' \
  --property=WorkingDirectory=/tmp \
  /usr/bin/python3 ${REMOTE} --video '${VIDEO}' \
      --hwdec '${HWDEC}' --size '${SIZE}' --seconds '${SECS}' --fps '${FPS}' \
      --format '${FORMAT}' \
      --dump-frame /tmp/sw_spike_frame.png
echo
echo '--- probe still alive after the run? ---'
sudo -n systemctl is-active metixel-cage metixel-backend || true
"
SW_RC=$?
[ "$SW_RC" -eq 124 ] && echo "  !! spike timed out (wedged remote unit) -- cleaning up" && \
  ssh -o BatchMode=yes -o ConnectTimeout=10 "${TARGET}" \
    "sudo -n systemctl stop --no-block metixel-swspike 2>/dev/null; sudo -n systemctl reset-failed metixel-swspike 2>/dev/null" || true

echo
echo "== 4/4 fetch the dumped frame so it can be LOOKED at =="
# Deliberately a LOCAL command, outside the ssh block: run inside it, the Pi
# would try to copy from itself and fail (there is no ssh agent on the Pi).
scp -o BatchMode=yes -o ConnectTimeout=10 -q \
    "${TARGET}:/tmp/sw_spike_frame.png" /tmp/sw_spike_frame.png \
  && ls -la /tmp/sw_spike_frame.png \
  || echo "  (no frame was dumped)"
