#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# Runner for _probe_qml_stack.py — run this ON the Pi (or via the dev machine).
#
# The probe opens a Qt Quick window and asks `grim` for the composited output, so
# it needs its OWN compositor: the running frame would otherwise own the display
# and the capture would come back showing the slideshow instead of the probe.
# This therefore stops metixel-cage, runs each case under a fresh cage, then
# restores the service exactly as it was found.
#
# cage is launched through a transient systemd unit rather than directly, because
# metixel-cage.service grants `pi` the supplementary groups cage and the video
# decode paths need (video render input tty) and an ssh session does not have
# them.  Without them a hardware path fails to open its device and the run
# silently measures the software fallback — the exact mix-up that makes a spike
# result untrustworthy.
#
# Configuration is via environment variables so nothing has to survive shell
# quoting:
#
#   CASES   which cases to run      (default: "qml video")
#   SECS    seconds per case        (default: 10)
#   SHOT    screenshot path override; EMPTY skips the capture entirely
#   HWDEV   QT_FFMPEG_DECODING_HW_DEVICE_TYPES value; unset = leave Qt's default
#   VIDEO   video path on the Pi    (default: first .mp4 found)
#   IMAGE   image path on the Pi    (default: first .jpg found)
#   IMAGE2  second image for the crossfade case (default: the SECOND .jpg found,
#           so a crossfade blends two different slides rather than one with itself)
#   QSG     QSG_RHI_BACKEND         (unset = Qt's default)
#
# Usage (from the dev machine):
#   scp scripts/dev/_probe_qml_stack.py scripts/dev/_run_qml_probe.sh \
#       pi@<frame>:/tmp/metixel-qmlprobe/
#   ssh pi@<frame> 'bash /tmp/metixel-qmlprobe/_run_qml_probe.sh'
#   scp -r pi@<frame>:/tmp/metixel-qmlprobe/out ./
set -uo pipefail

SPIKE_DIR="${SPIKE_DIR:-/tmp/metixel-qmlprobe}"
OUT="${OUT:-$SPIKE_DIR/out}"
PY="${PY:-/usr/bin/python3}"
CAGE="${CAGE:-/usr/bin/cage}"
CASES="${CASES:-qml video}"
SECS="${SECS:-10}"
MEDIA_ROOT="${MEDIA_ROOT:-/opt/metixel/data/media}"

# Deliberately explicit, and echoed below: the probe's numbers are only
# meaningful alongside the inputs that produced them.
VIDEO="${VIDEO:-$(find "$MEDIA_ROOT" -type f -iname '*.mp4' 2>/dev/null | head -1)}"
IMAGE="${IMAGE:-$(find "$MEDIA_ROOT" -type f -iname '*.jpg' 2>/dev/null | head -1)}"
IMAGE2="${IMAGE2:-$(find "$MEDIA_ROOT" -type f -iname '*.jpg' 2>/dev/null | sed -n 2p)}"

mkdir -p "$OUT"

echo "=== Qt Quick feasibility probe ==="
echo "  probe      : $SPIKE_DIR/_probe_qml_stack.py"
echo "  cases      : $CASES  (${SECS}s each)"
echo "  video      : ${VIDEO:-<none>}"
echo "  image      : ${IMAGE:-<none>}"
echo "  image2     : ${IMAGE2:-<none>}"
echo "  HWDEV      : ${HWDEV:-<unset — Qt default>}"
echo "  QSG_RHI    : ${QSG:-<unset — Qt default>}"
echo

if [[ ! -f "$SPIKE_DIR/_probe_qml_stack.py" ]]; then
    echo "ERROR: $SPIKE_DIR/_probe_qml_stack.py not found." >&2
    exit 1
fi

echo "=== probe file identity (so we know the right code ran) ==="
sha256sum "$SPIKE_DIR/_probe_qml_stack.py" | cut -c1-16
echo "=== Qt versions as the units will see them ==="
"$PY" -c 'import PySide6; from PySide6.QtCore import qVersion; print("PySide6", PySide6.__version__, "| Qt", qVersion())' 2>&1 | tail -2

# Remember whether the frame was running, so it is restored exactly as found.
CAGE_WAS_ACTIVE="no"
if systemctl is-active --quiet metixel-cage; then
    CAGE_WAS_ACTIVE="yes"
fi

restore() {
    if [[ "$CAGE_WAS_ACTIVE" == "yes" ]]; then
        echo
        echo "=== restoring metixel-cage ==="
        sudo -n systemctl start metixel-cage || echo "WARNING: could not restart metixel-cage" >&2
    fi
}
trap restore EXIT

echo
echo "=== stopping metixel-cage so the probe can own the display ==="
sudo -n systemctl stop metixel-cage || echo "WARNING: could not stop metixel-cage" >&2
sleep 2

# The output geometry matters: a 1920x1200 probe window on a smaller output would
# be clipped, and the screenshot would be misread as a rendering failure.
echo
echo "=== outputs (before the probe) ==="
wlr-randr 2>/dev/null || echo "  (wlr-randr unavailable)"

for case in $CASES; do
    echo
    echo "=== running case: $case ==="
    args=(--case "$case" --outdir "$OUT" --seconds "$SECS")
    # The screenshot is the visual control, but `grim` BLOCKS the render thread
    # for ~0.5 s, and that shows up as one enormous swap interval in the pacing
    # stats (measured: 490 ms, which alone accounted for a 58.5-vs-60.1 fps
    # shortfall).  SHOT= (empty) runs without it so the pacing figure is clean.
    if [[ -z "${SHOT+x}" ]]; then
        args+=(--shot "$OUT/${case}.png")
    elif [[ -n "$SHOT" ]]; then
        args+=(--shot "$SHOT")
    fi
    [[ -n "$VIDEO" ]] && args+=(--video "$VIDEO")
    [[ -n "$IMAGE" ]] && args+=(--image "$IMAGE")
    [[ -n "$IMAGE2" ]] && args+=(--image2 "$IMAGE2")

    setenv=(--setenv=PYTHONPATH=/opt/metixel/live/src
            --setenv=XDG_RUNTIME_DIR=/run/user/1000
            --setenv=PYTHONUNBUFFERED=1
            --setenv=QT_QPA_PLATFORM=wayland
            # Surfaces the FFmpeg backend's own hw-accel decisions line by line,
            # so a software-decode verdict can be corroborated rather than taken
            # on trust from handleType alone.
            --setenv=QT_LOGGING_RULES=qt.multimedia.ffmpeg*=true)
    [[ -n "${HWDEV:-}" ]] && setenv+=(--setenv="QT_FFMPEG_DECODING_HW_DEVICE_TYPES=$HWDEV")
    [[ -n "${QSG:-}" ]] && setenv+=(--setenv="QSG_RHI_BACKEND=$QSG")

    timeout 150 sudo -n systemd-run \
        --unit="metixel-qmlprobe-$case" \
        --collect --pipe --wait \
        --uid=pi --gid=pi \
        --property=SupplementaryGroups="video render input tty" \
        --property=WorkingDirectory="$SPIKE_DIR" \
        "${setenv[@]}" \
        "$CAGE" -d -- "$PY" "$SPIKE_DIR/_probe_qml_stack.py" "${args[@]}"
    rc=$?
    if [[ $rc -eq 124 ]]; then
        echo "  !! case timed out (wedged unit) — cleaning up"
        sudo -n systemctl stop --no-block "metixel-qmlprobe-$case" 2>/dev/null || true
        sudo -n systemctl reset-failed "metixel-qmlprobe-$case" 2>/dev/null || true
    elif [[ $rc -ne 0 ]]; then
        echo "  (unit exited $rc)"
    fi
done

echo
echo "=== SUMMARY ==="
"$PY" - "$OUT" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
for case in ("qml", "video", "crossfade"):
    path = out / f"{case}.json"
    if not path.exists():
        print(f"{case:6s} (no report written)")
        continue
    d = json.loads(path.read_text())
    print(f"--- {case} ---")
    if d.get("fatal"):
        print(f"  FATAL: {d['fatal']}")
    print(f"  qml_loaded={d.get('qml_loaded')}  api={d.get('graphics_api')}")
    scr = d.get("screen") or {}
    if scr:
        print(f"  screen={scr.get('geometry')} refresh={scr.get('refresh_rate')}Hz "
              f"dpr={scr.get('device_pixel_ratio')}")
    sw = d.get("swaps") or {}
    iv = (sw or {}).get("intervals")
    if iv:
        print(f"  swaps={sw['count']} fps={sw.get('fps')} "
              f"p50={iv['p50_ms']}ms p95={iv['p95_ms']}ms max={iv['max_ms']}ms")
        if "vsync_locked_pct" in sw:
            print(f"  vsync_locked={sw['vsync_locked_pct']}%  dropped={sw['dropped']} "
                  f"({sw['dropped_pct']}%)  vsync={sw.get('vsync_ms')}ms")
    vid = d.get("video")
    if vid:
        print(f"  video frames={vid.get('frames_delivered')} "
              f"handles={vid.get('handle_types')}")
        print(f"  VERDICT: {vid.get('verdict')}")
        vi = (vid.get("interval") or {}).get("intervals")
        if vi:
            print(f"  video p50={vi['p50_ms']}ms p95={vi['p95_ms']}ms max={vi['max_ms']}ms")
        if vid.get("error_string"):
            print(f"  error={vid['error_string'][:120]}")
    cpu = d.get("cpu") or {}
    if cpu:
        print(f"  cpu={cpu.get('cpu_percent')}% over {cpu.get('wall_s')}s")
    cap = d.get("capture") or {}
    if cap:
        print(f"  screenshot={'ok' if cap.get('ok') else 'FAILED'} {cap.get('bytes','')} bytes")
    if d.get("qml_warnings"):
        print(f"  qml_warnings={d['qml_warnings'][:3]}")
    print()
PY
