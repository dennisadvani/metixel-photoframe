#!/usr/bin/env bash
# Force-clean the stuck GStreamer spike unit on the Pi.
#
# Why SIGKILL: `systemctl stop metixel-gstspike` itself hung (the remote
# orchestrator returned rc=124), because cage was wedged inside DRM teardown
# after its gst-launch client died.  A normal stop path cannot make progress
# against that, so kill the processes directly and reset the unit.
set -uo pipefail

echo "--- before ---"
pgrep -af 'gst-launch-1.0|cage -d' || echo "  (no matching processes)"
systemctl is-active metixel-gstspike 2>/dev/null || true

echo "--- killing ---"
sudo -n pkill -9 -f 'gst-launch-1.0' 2>/dev/null && echo "  killed gst-launch" || echo "  no gst-launch"
sudo -n pkill -9 -f 'cage -d' 2>/dev/null && echo "  killed cage" || echo "  no cage"

# Non-blocking: never let cleanup itself become the thing that hangs.
sudo -n systemctl stop --no-block metixel-gstspike 2>/dev/null
sleep 2
sudo -n systemctl reset-failed metixel-gstspike 2>/dev/null
sudo -n systemctl reset-failed 'metixel-gst*' 2>/dev/null

echo "--- after ---"
pgrep -af 'gst-launch-1.0|cage -d' || echo "  (no matching processes)"
printf 'gstspike unit: '
systemctl is-active metixel-gstspike 2>/dev/null || echo inactive
echo "--- load ---"
cat /proc/loadavg
echo "CLEAN_DONE"
