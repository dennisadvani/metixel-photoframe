# Chromium Kiosk Prototype — Benchmark Results

Date: 2026-08-30
Hardware tested: Raspberry Pi 4/5 and Raspberry Pi 3 Model B

This document records the measured performance of the Chromium kiosk frontend
prototype on two Raspberry Pi models. The goal was to determine whether a
**single Chromium engine** can replace the pi3d/VLC frontend for Metixel v2 —
delivering virtual matte, screen-based UIs, and notifications while playing
video smoothly.

---

## Test setup

- **Prototype**: `scripts/chromium_prototype/` — standalone stdlib HTTP server
  + HTML/CSS/JS kiosk page (virtual matte via `object-fit: contain`, crossfade
  via CSS opacity, FPS counter via `requestAnimationFrame`).
- **Display stack**: Xorg (modesetting driver) + Chromium `--kiosk
  --ozone-platform=x11` + `unclutter` (cursor hidden).
- **Media**: identical set on both boards for a fair comparison —
  1 × 1920×1080 24fps H.264 video + 7 × 1920×1080 JPEG images.
- **Measurement**: FPS reported by the page to `/api/benchmark` every 5s;
  CPU/mem sampled from `/proc` every 1s for 60s.

---

## Results

### Raspberry Pi 4/5 (192.168.222.122, 2GB RAM)

| Metric | Value |
|---|---|
| **FPS** | mean **59.6**, median 59.9, min 55, max 60 (32 samples) |
| **CPU** | mean **6.3%**, max 24.2% |
| **Memory** | 54.7% used, ~930 MB available of 2 GB |

**Verdict: Excellent.** Smooth 60 FPS with very light CPU load and comfortable
memory headroom. Hardware acceleration active. The Chromium kiosk is fully
viable on Pi 4/5.

### Raspberry Pi 3 Model B (192.168.222.230, 875 MB RAM)

| Metric | Value |
|---|---|
| **FPS** | mean **35.0**, median 45.5, **min 0.1**, max 60 (23 samples) |
| **CPU** | mean **28.4%**, max 85.9% |
| **Memory** | 70.2% used, only ~260 MB available of 875 MB |
| **System state** | load average 3.80, 42% I/O wait, 212 MB swap used |

**Verdict: Not viable.** FPS is highly erratic — it reaches 60 during simple
image display but **drops to 0.1–3 FPS during video decode**. The system
thrashes on swap and I/O wait, and Chromium's main process alone consumes ~40%
CPU. The Pi 3's VideoCore IV GPU and 875 MB RAM are insufficient for a full
browser-based renderer with video.

---

## Conclusion

| Model | Chromium kiosk viable? | Notes |
|---|---|---|
| **Pi 4/5** | ✅ Yes | Smooth 60 FPS, light CPU, comfortable memory |
| **Pi 3 Model B** | ❌ No | Erratic FPS (0.1–3 during video), swapping, high CPU |

**A single Chromium engine is NOT viable for the Pi 3.** The Pi 3 needs a
fallback strategy:

1. **Hybrid** — Chromium for Pi 4/5, keep VLC/pi3d for Pi 3 video (two engines).
2. **Images-only on Pi 3** — Chromium kiosk for images, skip video on Pi 3
   (matches the existing Pi Zero 2 W policy).
3. **Drop Pi 3** — focus on Pi 4/5 (the recommended platform is Pi 5).

Given the preference for a **single engine**, options 2 or 3 are the realistic
paths forward.

---

## Raw data

- Pi 4/5: `out/benchmark_results.json`, `out/cpu_mem.json`
- Pi 3: `out/pi3/benchmark_results.json`, `out/pi3/cpu_mem.json`

Re-run the summary with:

```bash
python scripts/chromium_prototype/measure.py \
    --cpu-mem scripts/chromium_prototype/out/cpu_mem.json \
    --fps scripts/chromium_prototype/out/benchmark_results.json

python scripts/chromium_prototype/measure.py \
    --cpu-mem scripts/chromium_prototype/out/pi3/cpu_mem.json \
    --fps scripts/chromium_prototype/out/pi3/benchmark_results.json
```