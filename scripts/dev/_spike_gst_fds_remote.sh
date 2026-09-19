#!/usr/bin/env bash
# Remote half of the GStreamer fd-leak spike. Runs one pipeline under cage and
# counts sync_file descriptors (the fd class libplacebo leaks one of per render).
#
# Usage: rem.sh <seconds> <gst-launch args...>
set -uo pipefail

SECS="${1:?usage: rem.sh <seconds> <pipeline...>}"
shift
GST=/usr/bin/gst-launch-1.0

fd_total() { ls /proc/"$1"/fd 2>/dev/null | wc -l; }
fd_sync() { ls -l /proc/"$1"/fd 2>/dev/null | grep -c sync_file; }

echo "### pipeline: $GST $*"

# shellcheck disable=SC2086
$GST -v "$@" >/tmp/gst_stderr.log 2>&1 &
PID=$!

sleep 4
if ! kill -0 "$PID" 2>/dev/null; then
	echo "!!! pipeline exited within warm-up (or never started)"
	grep -iE 'ERROR|WARNING|does not|no element' /tmp/gst_stderr.log | head -8
	echo "!!! VERDICT: did-not-run"
	exit 1
fi

BASE_SYNC=$(fd_sync "$PID")
BASE_TOTAL=$(fd_total "$PID")
echo "post-warmup: fd=$BASE_TOTAL  sync_file=$BASE_SYNC"

T0=$(date +%s)
while kill -0 "$PID" 2>/dev/null; do
	ELAPSED=$(( $(date +%s) - T0 ))
	[ "$ELAPSED" -ge "$SECS" ] && break
	printf 't=%3ss fd=%4s sync_file=%4s\n' \
		"$ELAPSED" "$(fd_total "$PID")" "$(fd_sync "$PID")"
	sleep 2
done

ALIVE=no
kill -0 "$PID" 2>/dev/null && ALIVE=yes
END_SYNC=$(fd_sync "$PID")
END_TOTAL=$(fd_total "$PID")
WALL=$(( $(date +%s) - T0 ))
[ "$WALL" -lt 1 ] && WALL=1

kill -TERM "$PID" 2>/dev/null
wait "$PID" 2>/dev/null

GROWTH=$((END_SYNC - BASE_SYNC))
echo "still running at end : $ALIVE"
echo "sync_file growth     : $GROWTH over ${WALL}s"
echo "fd total             : $BASE_TOTAL -> $END_TOTAL"
echo "negotiated caps:"
grep -E 'caps = video/x-raw' /tmp/gst_stderr.log | tail -3 | cut -c1-170
if [ "$ALIVE" != yes ]; then
	echo "VERDICT: inconclusive (source ended -- window too short)"
elif [ "$GROWTH" -gt 10 ]; then
	echo "VERDICT: LEAKS ($GROWTH sync_file in ${WALL}s)"
else
	echo "VERDICT: clean ($GROWTH sync_file in ${WALL}s)"
fi
