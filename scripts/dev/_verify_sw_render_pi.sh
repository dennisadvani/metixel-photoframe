#!/usr/bin/env bash
# Verify the software render path on the Pi under REAL slideshow playback.
#
# The old failure was `OSError: [Errno 24] Too many open files` after ~27 s of
# CUMULATIVE video playback (fd 1020 / limit 1024, ~28-30 sync_file/s).  This
# runs for well past that with video enabled and samples the descriptor classes,
# so a passing result means the leak is gone rather than merely delayed.
#
# The screenshot hash is the "is it actually rendering" control: a clean fd count
# is meaningless if nothing is being drawn.
#
# TWO traps this script exists to avoid, both of which produced a meaningless
# "0 descriptors, clean" result on its first run:
#   1. Enabling video via the API is a pipeline-affecting save, so the BACKEND
#      RESTARTS THE FRONTEND.  The pid must therefore be re-resolved every
#      sample, or every read is of a dead process and returns nothing.
#   2. A missing /proc/<pid>/fd reads as zero, not as an error.  The count is
#      reported as NA when the pid is gone so it cannot masquerade as clean.
#
# Run:  ssh pi@host bash -s < scripts/dev/_verify_sw_render_pi.sh
#       ENABLE=1 ...  to also turn video playback on (triggers a restart)
set -uo pipefail

# `mode frontend` would match the remote shell running this script itself.
FE_PATTERN='mode front[e]nd'
SAMPLES="${SAMPLES:-12}"
INTERVAL="${INTERVAL:-10}"
ENABLE="${ENABLE:-0}"

current_pid() { pgrep -f "$FE_PATTERN" | head -1; }

# All readers report "NA" when the process is gone, so a vanished pid can never
# be mistaken for a zero-descriptor, leak-free process.
fd_listing() { sudo -n ls -l /proc/"$1"/fd 2>/dev/null; }
fd_total() { local n; n=$(fd_listing "$1" | wc -l); [ "$n" -eq 0 ] && echo NA || echo "$n"; }
fd_count() {
	local n
	n=$(fd_listing "$1" | grep -c "$2")
	if [ "$(fd_total "$1")" = NA ]; then echo NA; else echo "$n"; fi
}
rss_kb() {
	local n
	n=$(sudo -n awk '/VmRSS/{print $2}' /proc/"$1"/status 2>/dev/null)
	echo "${n:-NA}"
}

pid=$(current_pid)
echo "frontend pid : ${pid:-NONE}"
if [ -z "$pid" ]; then
	echo "VERDICT: frontend not running"
	exit 1
fi
echo "fd limit     : $(sudo -n awk '/Max open files/{print $4}' /proc/"$pid"/limits | head -1)"

if [ "$ENABLE" = "1" ]; then
	echo
	echo "--- enabling video playback (this RESTARTS the frontend) ---"
	curl -s --max-time 10 -X PUT -H 'Content-Type: application/json' \
		-d '{"playback_enabled": true}' \
		http://127.0.0.1:8080/api/config/video >/dev/null
	sleep 12
	pid=$(current_pid)
	echo "frontend pid after restart: ${pid:-NONE}"
fi

echo
echo -n "video config : "
curl -s --max-time 5 http://127.0.0.1:8080/api/config/video |
	python3 -c 'import json,sys; d=json.load(sys.stdin); print({k: d.get(k) for k in ("playback_enabled","max_duration_seconds")})' 2>/dev/null || echo "(unreadable)"

echo
printf '%-7s %6s %6s %9s %10s %8s %14s\n' time pid fd sync_file dmabuf rss_kb shot_hash
base_pid="$pid"
base_sync=$(fd_count "$pid" sync_file)
prev_pid="$pid"
last_hash=""
for i in $(seq 1 "$SAMPLES"); do
	sleep "$INTERVAL"
	pid=$(current_pid)
	if [ "$pid" != "$prev_pid" ]; then
		echo "  !! frontend restarted (pid $prev_pid -> $pid) -- baselines reset"
		base_sync=$(fd_count "$pid" sync_file)
		prev_pid="$pid"
	fi
	shot="/tmp/sw_verify_$i.png"
	XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 \
		grim "$shot" >/dev/null 2>&1 || true
	hash=$(md5sum "$shot" 2>/dev/null | cut -c1-12)
	printf '%-7s %6s %6s %9s %10s %8s %14s\n' \
		"$((i * INTERVAL))s" "${pid:-NA}" "$(fd_total "$pid")" \
		"$(fd_count "$pid" sync_file)" "$(fd_count "$pid" dmabuf)" \
		"$(rss_kb "$pid")" "${hash:-none}"
	if [ "$hash" = "$last_hash" ]; then
		echo "  (same screenshot as previous sample)"
	fi
	last_hash="$hash"
done

end_sync=$(fd_count "$pid" sync_file)
echo
echo "start pid=$base_pid sync_file=$base_sync  ->  end pid=$pid sync_file=$end_sync"
echo "--- video events in the frontend log (last 10) ---"
sudo -n grep -aE "Video playback (started|ended)" \
	/opt/metixel/data/logs/metixel-frontend.log 2>/dev/null | tail -10 |
	cut -c1-140

if [ "$base_sync" = NA ] || [ "$end_sync" = NA ]; then
	echo "VERDICT: INCONCLUSIVE — descriptors were unreadable"
elif [ "$base_pid" != "$pid" ]; then
	echo "VERDICT: INCONCLUSIVE — the frontend restarted mid-run"
elif [ "$end_sync" -gt 20 ]; then
	echo "VERDICT: STILL LEAKS (sync_file $base_sync -> $end_sync)"
else
	echo "VERDICT: clean (sync_file $base_sync -> $end_sync over $((SAMPLES * INTERVAL))s)"
fi
