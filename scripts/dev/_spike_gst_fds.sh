#!/usr/bin/env bash
# Local orchestrator for the GStreamer fd-leak spike.
#
# Each case runs one gst-launch pipeline inside its own `cage` compositor so the
# GL elements get a real Wayland/EGL context, and samples sync_file descriptors
# against runtime.  sync_file is the class libplacebo leaks one of per render,
# so it is the like-for-like comparison against the mpv GL result.
#
# Cases:
#   gl_roundtrip : videotestsrc -> glupload -> glcolorconvert -> gldownload
#                  (infinite source; the GL fence path under sustained load)
#   gl_sink      : videotestsrc -> glupload -> glcolorconvert -> glimagesink
#                  (same, but presenting to the surface like the real UI)
#   hwdec_linear : the Pi HW HEVC decoder forced to linear output, to see
#                  whether it can be made to negotiate at all
#   hwdec_dmabuf : the HW decoder's native tiled output straight into glupload
#
# Usage: bash scripts/dev/_spike_gst_fds.sh
set -uo pipefail

PI="${PI:-pi@192.168.222.122}"
VIDEO="${VIDEO:-/opt/metixel/data/cache/videos/2cd3ac6ece47670f.mp4}"
SECS="${SECS:-24}"
OUT="${OUT:-/tmp/gst_fds.txt}"
REMOTE=/tmp/_spike_gst_fds_remote.sh

# The frontend's own surface size, so the GL paths do production-sized work.
W=1920
H=1200
FPS=30

: >"$OUT"

run_case() {
	local name="$1"
	shift
	echo
	echo "########## GST spike: $name ##########"
	# cage needs XDG_RUNTIME_DIR to create its Wayland socket; the real
	# metixel-cage unit gets it from the systemd user session, which a
	# transient --uid unit does not inherit.  The groups mirror
	# _run_sw_spike.sh (an ssh session lacks video/render/input on its own).
	ssh -o BatchMode=yes -o ConnectTimeout=20 "$PI" \
		"sudo -n systemd-run --quiet --unit=metixel-gstspike --collect --pipe --wait \
		 --uid=pi --gid=pi \
		 --property=SupplementaryGroups='video render input tty' \
		 --property=Environment=XDG_RUNTIME_DIR=/run/user/1000 \
		 --property=WorkingDirectory=/tmp \
		 /usr/bin/cage -d -- /bin/bash $REMOTE $SECS $*"
}

{
	echo "########## uploading remote spike ##########"
	scp -q -o BatchMode=yes -o ConnectTimeout=15 \
		scripts/dev/_spike_gst_fds_remote.sh "$PI:$REMOTE" &&
		echo "uploaded -> $REMOTE"

	run_case gl_roundtrip \
		videotestsrc \
		"!" "video/x-raw,format=NV12,width=$W,height=$H,framerate=$FPS/1" \
		"!" glupload "!" glcolorconvert "!" gldownload "!" fakesink sync=false

	run_case gl_sink \
		videotestsrc \
		"!" "video/x-raw,format=NV12,width=$W,height=$H,framerate=$FPS/1" \
		"!" glupload "!" glcolorconvert "!" glimagesink sync=false

	run_case hwdec_linear \
		filesrc "location=$VIDEO" "!" qtdemux "!" h265parse \
		"!" v4l2slh265dec capture-io-mode=mmap \
		"!" "video/x-raw,format=NV12" "!" videoconvert "!" fakesink sync=false

	run_case hwdec_dmabuf \
		filesrc "location=$VIDEO" "!" qtdemux "!" h265parse \
		"!" v4l2slh265dec \
		"!" glupload "!" glcolorconvert "!" glimagesink sync=false
} >>"$OUT" 2>&1

grep -nE '^####|post-warmup|^t=|still running|sync_file growth|fd total|VERDICT|caps = video|ERROR|WARNING' "$OUT" |
	cut -c1-165
