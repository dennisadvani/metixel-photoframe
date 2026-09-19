#!/usr/bin/env bash
# Local orchestrator: quiet the Pi, then sweep render-loop fps with the SW spike.
#
# Purpose: separate the *fixed* per-frame cost (decode + copy) from the
# *render/convert* cost. The SW spike's CPU is measured with time.process_time()
# in the spike process only, so any metixel process still running only perturbs
# it through scheduling/cache pressure -- but on a 4-core Pi that is enough to
# matter, so we stop the frame services first.
#
# Usage: bash scripts/dev/_run_sw_fps_sweep.sh
set -uo pipefail

PI="${PI:-pi@192.168.222.122}"
OUT="${OUT:-/tmp/sw_fps_sweep.txt}"
SIZE="${SIZE:-1280x800}"
HWDEC="${HWDEC:-drm-copy}"
SECS="${SECS:-16}"
FPS_LIST="${FPS_LIST:-30 15}"

log() { echo "$*" | tee -a "$OUT"; }

: >"$OUT"

log "########## 1/3 quiet the Pi (stop app services) ##########"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$PI" \
	"sudo -n systemctl stop metixel-cage metixel-backend metixel-cursor-hider; \
	 sleep 2; \
	 echo 'active-state (expect all inactive):'; systemctl is-active metixel-backend metixel-cage metixel-cursor-hider; \
	 echo 'leftover procs:'; pgrep -af 'python3 -m metixel|/usr/bin/cage' || echo '  none'; \
	 echo 'loadavg:'; cat /proc/loadavg" >>"$OUT" 2>&1

log ""
log "########## 2/3 settle (expect 1-min load to fall toward 0) ##########"
sleep 15
ssh -o BatchMode=yes -o ConnectTimeout=15 "$PI" "cat /proc/loadavg" >>"$OUT" 2>&1

for f in $FPS_LIST; do
	log ""
	log "########## 3/3 SW spike @ fps=$f  size=$SIZE  hwdec=$HWDEC ##########"
	FPS="$f" SIZE="$SIZE" HWDEC="$HWDEC" SECS="$SECS" \
		bash scripts/dev/_run_sw_spike.sh >>"$OUT" 2>&1
done

log ""
log "########## done -> $OUT ##########"
grep -nE 'active-state|inactive|leftover|none|loadavg|^[0-9.]+ |^####|REQUESTED|target fps|frames|sync_file|cpu |cpu per frame|VERDICT' "$OUT" | tail -60
