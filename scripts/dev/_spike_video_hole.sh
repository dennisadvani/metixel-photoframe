#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# PHASE-0 spike runner — run this ON the Pi.
#
# The spike needs its OWN compositor: it builds a throwaway widget tree and asks
# grim for the composited output, which the running frame would otherwise own.
# So this stops metixel-cage, runs each case under a fresh cage, then restores
# the service.
#
# It launches cage through a transient systemd unit rather than running it
# directly, because metixel-cage.service grants `pi` the supplementary groups
# cage needs (video render input tty) and an SSH session does not have them.
# The transient unit mirrors the service's setup, so DRM access works the same
# way it does in production.
#
# Usage (from the dev machine):
#   scp scripts/dev/_spike_video_hole.py scripts/dev/_spike_video_hole.sh \
#       pi@<frame>:/tmp/metixel-spike/
#   ssh pi@<frame> 'bash /tmp/metixel-spike/_spike_video_hole.sh'
#   scp -r pi@<frame>:/tmp/metixel-spike/out ./
#
# Cases can be narrowed for a quick single answer, e.g.:
#   CASES="solid glwidget" bash /tmp/metixel-spike/_spike_video_hole.sh
set -uo pipefail

SPIKE_DIR="${SPIKE_DIR:-/tmp/metixel-spike}"
OUT="${OUT:-$SPIKE_DIR/out}"
SRC="${SRC:-/opt/metixel/live/src}"
PY="${PY:-/usr/bin/python3}"
CAGE="${CAGE:-/usr/bin/cage}"
VIDEO="${VIDEO:-}"
CASES="${CASES:-solid glwidget mpv_idle mpv_play}"

mkdir -p "$OUT"

echo "=== Phase 0 spike: partial artwork hole over a sibling ==="
echo "  script dir : $SPIKE_DIR"
echo "  output dir : $OUT"
echo "  cases      : $CASES"
echo

if [[ ! -f "$SPIKE_DIR/_spike_video_hole.py" ]]; then
    echo "ERROR: $SPIKE_DIR/_spike_video_hole.py not found." >&2
    exit 1
fi

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

echo "=== stopping metixel-cage so the spike can own the display ==="
sudo -n systemctl stop metixel-cage || echo "WARNING: could not stop metixel-cage" >&2
# Give the compositor a moment to release the DRM master.
sleep 2

for case in $CASES; do
    echo
    echo "=== running case: $case ==="
    args=(--case "$case" --outdir "$OUT")
    [[ -n "$VIDEO" ]] && args+=(--video "$VIDEO")

    # shellcheck disable=SC2086
    sudo -n systemd-run \
        --unit="metixel-spike-$case" \
        --collect --pipe --wait \
        --uid=pi --gid=pi \
        --property=SupplementaryGroups="video render input tty" \
        --property=WorkingDirectory="$SPIKE_DIR" \
        --setenv=PYTHONPATH="$SRC" \
        --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
        --setenv=PYTHONUNBUFFERED=1 \
        --setenv=QT_QPA_PLATFORM=wayland \
        "$CAGE" -d -- "$PY" "$SPIKE_DIR/_spike_video_hole.py" "${args[@]}"
    rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "  (unit exited $rc — see the case output above)"
    fi
done

echo
echo "=== SUMMARY ==="
"$PY" - "$OUT" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
order = ["solid", "glwidget", "mpv_idle", "mpv_play"]
rows = []
for case in order:
    path = out / f"{case}.json"
    if not path.exists():
        rows.append((case, "(no report)", "", "", False))
        continue
    data = json.loads(path.read_text())
    if not data.get("capture_ok"):
        rows.append((case, "CAPTURE FAILED", data.get("capture_error", "")[:40], "", False))
        continue
    expected = data.get("expected")
    if expected is None:
        mark = "info"
    elif data.get("verdict_ok"):
        mark = "PASS"
    else:
        mark = "FAIL"
    rows.append(
        (
            case,
            data.get("symptom", "?"),
            f"hole underlay={data.get('hole_underlay_hits')} "
            f"black={data.get('hole_black_hits')} "
            f"overlay={data.get('hole_overlay_hits')}",
            f"surround={data.get('surround_overlay_hits')} stddev={data.get('hole_stddev')}",
            mark,
        )
    )

width = max(len(r[0]) for r in rows)
print(f"  {'case':<{width}}  {'mark':<5}  {'symptom':<22}  detail")
print(f"  {'-' * width}  {'-' * 5}  {'-' * 22}  {'-' * 44}")
for case, symptom, detail, extra, mark in rows:
    print(f"  {case:<{width}}  {mark:<5}  {symptom:<22}  {detail}")
    if extra:
        print(f"  {'':<{width}}  {'':<5}  {'':<22}  {extra}")

print()
fundamental = [r for r in rows if r[0] in ("solid", "glwidget")]
if fundamental and all(r[4] == "PASS" for r in fundamental):
    print("  VERDICT: a painted partial hole WORKS over raster and GL siblings.")
    print("           The planned canvas-as-compositor architecture is viable.")
    mpv_play = next((r for r in rows if r[0] == "mpv_play"), None)
    if mpv_play is not None:
        print(f"           mpv over the hole: {mpv_play[1]}")
elif any(r[1] == "hole_black" for r in fundamental):
    print("  VERDICT: the partial hole renders BLACK — STOP and reconsider the")
    print("           compositing architecture (see the phase plan).")
else:
    print("  VERDICT: inconclusive — see the per-case JSON in", out)
PY

echo
echo "PNGs and JSON reports are in: $OUT"
