#!/bin/bash
# Measure the honest cost of the new blur: CPU across many slides, and RSS.
PID=$(pgrep -f "mode frontend" | head -1)
echo "pid=$PID  start=$(ps -o lstart= -p $PID)"

echo
echo "=== forcing many slides (3 s + 2 s crossfade) to sample the per-slide blur cost ==="
python3 - <<'PY'
import json, urllib.request
body = json.dumps({
    "image_duration_seconds": 3, "transition_duration_ms": 2000,
    "transition_style": "crossfade", "fit_mode": "contain",
    "smart_cover": True, "shuffle": True,
    "ambient_strategy": "blur", "ambient_color": "#1717d3",
    "ambient_blur_radius": 24, "ambient_darken": 0.35,
    "matte_color": [20, 20, 20],
}).encode()
req = urllib.request.Request("http://127.0.0.1:8080/api/config/slideshow",
                             data=body, method="PUT",
                             headers={"Content-Type": "application/json"})
urllib.request.urlopen(req)
PY

sleep 6
python3 - "$PID" <<'PY'
import os, sys, time
pid = sys.argv[1]
CLK = os.sysconf("SC_CLK_TCK")

def cpu():
    with open(f"/proc/{pid}/stat") as fh:
        p = fh.read().split()
    return (int(p[13]) + int(p[14])) / CLK

samples, prev = [], cpu()
for _ in range(34):
    time.sleep(1.0)
    now = cpu()
    samples.append(now - prev)
    prev = now

active = [s for s in samples if s > 0.05]
print(f"   per-second cores: {[round(s, 2) for s in samples]}")
print(f"   peak {max(samples):.2f} cores, mean while active "
      f"{(sum(active)/len(active) if active else 0):.2f} cores")
PY

echo
echo "=== RSS across many slides (must not climb) ==="
for i in 1 2 3 4; do
    echo "  VmRSS=$(grep VmRSS /proc/$PID/status | awk '{print $2}') kB  VmHWM=$(grep VmHWM /proc/$PID/status | awk '{print $2}') kB"
    sleep 9
done

echo
echo "=== errors? ==="
sudo tail -30 /opt/metixel/data/logs/metixel-frontend.log | grep -iE 'error|traceback|exception' || echo "  none"
