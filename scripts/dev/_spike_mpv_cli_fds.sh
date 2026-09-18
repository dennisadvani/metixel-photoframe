#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# SPIKE — does plain mpv leak sync_file descriptors WITHOUT Qt in the picture?
#
# scripts/dev/_spike_mpv_fd_options.py showed the frontend leaks exactly one
# DMA-BUF ``sync_file`` descriptor per ``MpvRenderContext.render()`` call, with
# ``sync_file == paints + 1`` in every leaking run.  That localised the leak to
# mpv's draw call, but it still ran inside Qt's GL context and through the
# libmpv render API.
#
# This script removes Qt and the render API entirely: it runs the ``mpv`` binary
# with its own window and its own swapchain, and counts the fds of the mpv
# process from the outside.
#
#   case        what it exercises
#   drm         mpv's native DRM/KMS output, no compositor, no Wayland at all
#   gpu         mpv owning a Wayland surface via EGL (cage compositor)
#   gpu-next    the libplacebo vo path via EGL
#
# If these leak too, the fences are created by mpv's own present path on this
# platform (mpv 0.40.0 + libplacebo 7.349.0 + Mesa 26.2.1) and neither Qt nor the
# render API is implicated — a much smaller bug report, and a fix we do not own.
# If they are clean, the leak needs the render API specifically.
#
# Run as the session user with the video/render groups, e.g. via systemd-run:
#   sudo systemd-run --uid=pi --gid=pi \
#     --property=SupplementaryGroups="video render input tty" \
#     --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
#     --pipe --wait --collect /bin/bash /tmp/_spike_mpv_cli_fds.sh

set -u

MP4=${MP4:-/opt/metixel/data/cache/videos/2cd3ac6ece47670f.mp4}
SAMPLES=${SAMPLES:-6}
INTERVAL=${INTERVAL:-2}
#: Let mpv reach steady state before the first sample.
STARTUP=${STARTUP:-5}

echo "### mpv $(mpv --version 2>/dev/null | head -1)  file=$(basename "$MP4")"

poll_fds() {
    local pid="$1" label="$2" i total sync
    for ((i = 1; i <= SAMPLES; i++)); do
        sleep "$INTERVAL"
        total=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
        sync=$(ls -l "/proc/$pid/fd" 2>/dev/null | grep -c sync_file)
        echo "  $label t+$((i * INTERVAL))s fd=$total sync_file=$sync"
    done
}

mpv_pid() {
    pgrep -x mpv | tail -1
}

#: mpv with its own window, hosted by a cage compositor we start ourselves.
case_caged() {
    local name="$1"
    shift
    echo "=== case=$name (cage/wayland) ==="
    cage -d -- mpv --no-config --really-quiet --msg-level=all=no \
        --loop-file=inf "$@" "$MP4" >/dev/null 2>&1 &
    local cage_pid=$!
    sleep "$STARTUP"
    local pid
    pid=$(mpv_pid)
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

#: mpv driving DRM/KMS directly — no compositor, no Wayland, no EGL-on-Wayland.
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
    else
        echo "  mpv pid=$pid"
        poll_fds "$pid" "$name"
    fi
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    sleep 1
}

case_direct drm --vo=gpu --gpu-context=drm
case_caged gpu --vo=gpu --gpu-context=wayland
case_caged gpu_next --vo=gpu-next --gpu-context=wayland

echo "### done"
