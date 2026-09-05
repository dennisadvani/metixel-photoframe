# Chromium Kiosk Frontend Prototype

A **standalone, zero-dependency** prototype to evaluate replacing the pi3d/VLC
frontend with a **Chromium kiosk** (HTML/CSS/JS) frontend for Metixel v2.

The goal is to prove that a single web engine can deliver everything the frame
needs — **virtual matte** for images *and* videos, **screen-based UIs**, and
**notifications** — while still playing video smoothly on Raspberry Pi 3 and
above. The critical unknown is **Pi 3 hardware h264 decode inside Chromium**
(VideoCore IV). This prototype measures it so you can decide whether a single
engine is viable or whether Pi 3 needs a fallback.

This prototype is **completely separate from the metixel app** — it uses only
the Python standard library and a plain Chromium browser. No Flask, no pi3d,
no VLC, no metixel imports.

---

## What it demonstrates

| Feature | How it's done |
|---|---|
| **Virtual matte** | CSS `object-fit: contain` on a black background — identical for `<img>` and `<video>` |
| **Crossfade** | CSS `opacity` transition (GPU-accelerated) |
| **Screen-based UI** | HTML/CSS/DOM overlay layer |
| **Notifications** | CSS toast/notification elements |
| **Video playback** | `<video>` element → Chromium's decoder |
| **FPS measurement** | `requestAnimationFrame` counter, posted to `/api/benchmark` |

---

## Layout

```
scripts/chromium_prototype/
├── README.md          # This file
├── server.py          # stdlib http.server — serves the kiosk page + media + /api/benchmark
├── index.html         # Kiosk page (black bg, matte container, UI + notification layers)
├── style.css          # Matte, crossfade, notification/toast, UI overlay styles
├── app.js             # Slideshow loop, matte, crossfade, notification demo, FPS counter
├── benchmark.js       # rAF FPS measurement + POST to /api/benchmark
├── run_on_pi.sh       # Copy to Pi, launch chromium kiosk, sample /proc CPU/mem
├── sampler.py         # On-Pi /proc CPU/mem sampler (run alongside the kiosk)
└── measure.py         # Local collector/aggregator of FPS + CPU/mem results
```

---

## Quick start (desktop)

```bash
# 1. Serve the prototype locally (defaults to data/media/sample_media)
python scripts/chromium_prototype/server.py

# 2. Open in a browser
#    http://localhost:8000
```

You should see the sample images crossfading with a black matte, a small FPS
counter, and a periodic notification toast. The video plays when the playlist
reaches it.

---

## On-Pi benchmark

```bash
# Copy the prototype to the Pi and run the kiosk benchmark
bash scripts/chromium_prototype/run_on_pi.sh --user pi --ip 192.168.222.122
```

This:
1. Copies the prototype to `/tmp/metixel-chromium-proto` on the Pi.
2. Starts the stdlib server on the Pi.
3. Launches `chromium-browser --kiosk` fullscreen.
4. Samples `/proc` CPU/mem while the slideshow runs.
5. Collects the FPS results posted to `/api/benchmark`.

Run it on a Pi 4/5 (baseline) and a Pi 3 (video-decode risk) and compare.

---

## What to measure

- **Images-only FPS** — should be ≥30fps on all models.
- **Video FPS** — the 1920×1080 24fps sample. Compare Pi 3 vs Pi 4/5.
- **CPU / memory** during playback (sampled from `/proc`).
- **Matte correctness** — video and images both letterboxed to the same matte.

The Pi 3 video result decides the engine strategy:

- **Pi 3 video plays smoothly** → single Chromium engine for all models.
- **Pi 3 video stutters** → decide between a hybrid (VLC for video on Pi 3
  only) or images-only on Pi 3 (matching the existing Zero 2 W policy).

---

## Notes

- Audio is **muted** by design — no PulseAudio/PipeWire dependency.
- Transitions are **crossfade only** (CSS opacity). Ken Burns / WebGL are out
  of scope for this prototype.
- The server is stdlib-only so the prototype stays decoupled from metixel.
  The real v2 frontend will be served by the existing Flask backend on `:8080`.