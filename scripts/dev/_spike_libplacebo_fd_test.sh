#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# SPIKE — measure the sync_file leak against a SPECIFICALLY chosen libplacebo.
#
# The leak (libplacebo `gl_poll_callbacks` never calling `gl->DeleteSync` on the
# success path — upstream c93aa13) has to be tested against a library we control,
# and the Debian system library and a locally built one have the SAME SONAME
# (`libplacebo.so.349`).  That makes it dangerously easy to believe you are
# testing your build while actually testing Debian's.  So this script always
# reports the resolved path from /proc/<pid>/maps and refuses to guess.
#
# Usage:
#   script.sh <label> system                  # Debian's /usr/lib libplacebo
#   script.sh <label> /path/to/build/src      # a locally built one
#
# Run as the session user with the video/render groups, e.g.:
#   sudo systemd-run --uid=pi --gid=pi \
#     --property=SupplementaryGroups="video render input tty" \
#     --setenv=XDG_RUNTIME_DIR=/run/user/1000 \
#     --pipe --wait --collect /bin/bash /tmp/_spike_libplacebo_fd_test.sh label system

set -u

LABEL=${1:?usage: $0 <label> <system|libdir>}
LIBSEL=${2:?usage: $0 <label> <system|libdir>}

MP4=${MP4:-/opt/metixel/data/cache/videos/2cd3ac6ece47670f.mp4}
SAMPLES=${SAMPLES:-5}
INTERVAL=${INTERVAL:-2}
STARTUP=${STARTUP:-6}

if [ "$LIBSEL" = "system" ]; then
    PRELOAD=""
    EXPECT="Debian system"
else
    PRELOAD=$LIBSEL
    EXPECT="built: $LIBSEL"
fi

echo "### case=$LABEL  expecting=$EXPECT"

cage -d -- env LD_LIBRARY_PATH="$PRELOAD" \
    mpv --no-config --really-quiet --msg-level=all=no \
    --loop-file=inf --vo=gpu-next --gpu-context=wayland "$MP4" \
    >/tmp/_lp_test_err.txt 2>&1 &
cage_pid=$!
sleep "$STARTUP"

pid=$(pgrep -x mpv | tail -1)
if [ -z "$pid" ]; then
    echo "  no mpv process — stderr:"
    tail -6 /tmp/_lp_test_err.txt | sed 's/^/    /'
else
    # THE important line: which libplacebo is actually loaded?
    mapped=$(grep -o '[^ ]*libplacebo[^ ]*' "/proc/$pid/maps" 2>/dev/null | sort -u | head -2)
    echo "  mpv pid=$pid"
    echo "  libplacebo mapped: ${mapped:-<none>}"
    for ((i = 1; i <= SAMPLES; i++)); do
        sleep "$INTERVAL"
        total=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
        sync=$(ls -l "/proc/$pid/fd" 2>/dev/null | grep -c sync_file)
        echo "  $LABEL t+$((STARTUP + i * INTERVAL))s fd=$total sync_file=$sync"
    done
fi

kill "$cage_pid" 2>/dev/null
pkill -x mpv 2>/dev/null
wait "$cage_pid" 2>/dev/null
echo "### done"
