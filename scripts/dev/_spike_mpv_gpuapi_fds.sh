#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# SPIKE — does the fence leak follow libplacebo's GL BACKEND specifically?
#
# Established (Pi 5, mpv 0.40.0 / libplacebo 7.349.0 / Mesa 26.2.1):
#
#   --vo=gpu-next --gpu-api=opengl --gpu-context=drm   LEAKS  ~+60/s
#   --vo=gpu-next --gpu-api=opengl --gpu-context=wayland LEAKS ~+60/s
#   --vo=gpu      (either context)                     clean
#
# The DRM case has no compositor, so the leak is in libplacebo's render core, not
# Wayland's WSI.  This narrows it once more: libplacebo has separate GL and Vulkan
# backends behind one API.  If Vulkan is clean here, the loss is a GL-backend bug
# (sharper upstream report).  If Vulkan also leaks, it is backend-independent.
#
# NOTE: this is a DIAGNOSTIC, not a fix candidate for metixel.  Production uses the
# libmpv render API type "opengl" drawing into Qt's FBO, so a Vulkan gpu-api cannot
# be substituted without abandoning the embedded-canvas design.
#
# Also worth knowing: it may simply fail.  Pi 5's V3D Vulkan (v3dv) may not expose
# what libplacebo requires, in which case the case exits early and the workaround is
# a non-starter on this hardware regardless of the leak.
#
# Run as the session user with the video/render groups, e.g.:
#   sudo systemd-run --uid=pi --gid=pi \
#     --property=SupplementaryGroups="video render input tty" \
#     --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
#     --pipe --wait --collect /bin/bash /tmp/_spike_mpv_gpuapi_fds.sh

set -u

MP4=${MP4:-/opt/metixel/data/cache/videos/2cd3ac6ece47670f.mp4}
SAMPLES=${SAMPLES:-5}
INTERVAL=${INTERVAL:-2}
STARTUP=${STARTUP:-6}

echo "### mpv $(mpv --version 2>/dev/null | head -1)  file=$(basename "$MP4")"
echo "### gpu-api choices:"
mpv --no-config --gpu-api=help 2>&1 | head -12

poll_fds() {
    local pid="$1" label="$2" i total sync
    for ((i = 1; i <= SAMPLES; i++)); do
        sleep "$INTERVAL"
        total=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
        sync=$(ls -l "/proc/$pid/fd" 2>/dev/null | grep -c sync_file)
        echo "  $label t+$((STARTUP + i * INTERVAL))s fd=$total sync_file=$sync"
    done
}

#: mpv driving DRM directly — no compositor, no Wayland WSI in the picture.
case_direct() {
    local label="$1"
    shift
    echo "=== $label ==="
    mpv --no-config --really-quiet --msg-level=all=no \
        --loop-file=inf --vo=gpu-next --gpu-context=drm "$@" "$MP4" \
        >/tmp/_gpuapi_err.txt 2>&1 &
    local pid=$!
    sleep "$STARTUP"
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "  mpv exited early — status $(wait "$pid" 2>/dev/null; echo $?)"
        echo "  stderr (last 6):"
        tail -6 /tmp/_gpuapi_err.txt | sed 's/^/    /'
        return
    fi
    echo "  mpv pid=$pid"
    poll_fds "$pid" "$label"
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    sleep 1
}

#: A caged case, so the Wayland WSI actually exists.  ``vulkan`` has no DRM
#: context at all (see the --gpu-api=help output), so this is the only way to
#: exercise a Vulkan swapchain on this box.
case_caged() {
    local label="$1"
    shift
    echo "=== $label ==="
    cage -d -- mpv --no-config --msg-level=all=warn \
        --loop-file=inf --vo=gpu-next "$@" "$MP4" >/tmp/_gpuapi_err.txt 2>&1 &
    local cage_pid=$!
    sleep "$STARTUP"
    local pid
    pid=$(pgrep -x mpv | tail -1)
    if [ -z "$pid" ]; then
        echo "  no mpv process — stderr (last 8):"
        tail -8 /tmp/_gpuapi_err.txt | sed 's/^/    /'
    else
        echo "  mpv pid=$pid"
        poll_fds "$pid" "$label"
    fi
    kill "$cage_pid" 2>/dev/null
    pkill -x mpv 2>/dev/null
    wait "$cage_pid" 2>/dev/null
    echo "  stderr (last 4):"
    tail -4 /tmp/_gpuapi_err.txt | sed 's/^/    /'
    sleep 1
}

case_direct "opengl_drm (control - expect LEAK)" --gpu-api=opengl
case_caged "opengl_wayland (control - expect LEAK)" --gpu-api=opengl
case_caged "vulkan_waylandvk (the proposed workaround)" --gpu-api=vulkan

echo "### done"
