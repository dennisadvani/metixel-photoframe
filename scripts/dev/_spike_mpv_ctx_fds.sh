#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# SPIKE — does the fence leak live in libplacebo's render core, or in the
# EGL-on-Wayland presentation path?
#
# Established so far (all on the Pi 5, mpv 0.40.0 / libplacebo 7.349.0 / Mesa 26.2.1):
#
#   mpv --vo=gpu-next --gpu-context=wayland   LEAKS  ~+60/s
#   mpv --vo=gpu      --gpu-context=wayland   clean
#   mpv --vo=gpu      --gpu-context=drm       clean
#
# So gpu-next leaks and gpu does not, over the SAME compositor.  That leaves two
# explanations, and the next measurement separates them:
#
#   A) libplacebo's render core allocates a fence per frame, whatever it draws to
#      -> gpu-next leaks on DRM too.
#   B) libplacebo's use of the EGL/Wayland swapchain path does
#      -> gpu-next is clean on DRM, and the culprit is the Wayland WSI
#         (i.e. Mesa's Wayland EGL, e.g. explicit sync / dmabuf feedback).
#
# Blame matters here: (A) is a libplacebo bug, (B) is a Mesa-Wayland bug, and the
# report goes to a different project either way.
#
# This runs the three cases back to back with the same sampling method.  The two
# DRM cases run with NO compositor and no cage; the Wayland case runs under cage.
# metixel-cage MUST be stopped first (the DRM cases need DRM master).
#
# Run as the session user with the video/render groups, e.g.:
#   sudo systemd-run --uid=pi --gid=pi \
#     --property=SupplementaryGroups="video render input tty" \
#     --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
#     --pipe --wait --collect /bin/bash /tmp/_spike_mpv_ctx_fds.sh

set -u

MP4=${MP4:-/opt/metixel/data/cache/videos/2cd3ac6ece47670f.mp4}
SAMPLES=${SAMPLES:-5}
INTERVAL=${INTERVAL:-2}
#: Let playback reach steady state before the first sample.
STARTUP=${STARTUP:-5}

echo "### mpv $(mpv --version 2>/dev/null | head -1)  file=$(basename "$MP4")"

poll_fds() {
    local pid="$1" label="$2" i total sync
    for ((i = 1; i <= SAMPLES; i++)); do
        sleep "$INTERVAL"
        total=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
        sync=$(ls -l "/proc/$pid/fd" 2>/dev/null | grep -c sync_file)
        echo "  $label t+$((STARTUP + i * INTERVAL))s fd=$total sync_file=$sync"
    done
}

#: mpv owning the DRM device directly — no compositor, no Wayland.
case_direct() {
    local name="$1"
    shift
    echo "=== case=$name (no compositor) ==="
    mpv --no-config --really-quiet --msg-level=all=no \
        --loop-file=inf "$@" "$MP4" >/dev/null 2>&1 &
    local pid=$!
    sleep "$STARTUP"
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "  mpv exited early (context unavailable) — case skipped"
        wait "$pid" 2>/dev/null
        return
    fi
    echo "  mpv pid=$pid"
    poll_fds "$pid" "$name"
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    sleep 1
}

#: mpv hosted by a cage compositor it starts itself.
case_caged() {
    local name="$1"
    shift
    echo "=== case=$name (cage/wayland) ==="
    cage -d -- mpv --no-config --really-quiet --msg-level=all=no \
        --loop-file=inf "$@" "$MP4" >/dev/null 2>&1 &
    local cage_pid=$!
    sleep "$STARTUP"
    local pid
    pid=$(pgrep -x mpv | tail -1)
    if [ -z "$pid" ]; then
        echo "  no mpv process found — case did not start"
    else
        echo "  mpv pid=$pid"
        poll_fds "$pid" "$name"
    fi
    kill "$cage_pid" 2>/dev/null
    pkill -x mpv 2>/dev/null
    wait "$cage_pid" 2>/dev/null
    sleep 1
}

case_direct gpunext_drm --vo=gpu-next --gpu-context=drm
case_direct gpu_drm --vo=gpu --gpu-context=drm
case_caged gpunext_wayland --vo=gpu-next --gpu-context=wayland

echo "### done"
