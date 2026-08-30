# PySide6 + mpv Slideshow Prototype

A standalone prototype that combines **PySide6 (Qt6)** and **mpv** to build an
image + video slideshow, so you can test performance and capabilities on a
Raspberry Pi. This is a candidate alternative to the Chromium kiosk frontend.

**Status: WORKING on Pi 5 under cage (Wayland).** Video renders correctly,
the virtual matte works for images and videos, and the Qt UI overlay renders
on top of mpv video.

**Pi 5 and Pi 3 use the SAME graphics stack: cage/Wayland + v4l2m2m.** For
consistency across the fleet, both platforms run the identical
cage/Wayland + PySide6 + mpv (v4l2m2m) stack. The eglfs + drmprime zero-copy
path was proven on the Pi 5 but is **not used** — see the "Graphics stack
decision" section below.

## What it does

- Fullscreen window, **blue** background (the virtual matte).
- **Images** rendered with `object-fit: contain`-style letterboxing, centred
  at 70% of the screen on a blue matte.
- **Videos** played by mpv embedded into the same widget, also centred at 70%
  on a blue matte.
- **Crossfade** between items via Qt opacity animation.
- **UI overlay**: clock (top-right), caption (bottom), notification toast
  (periodic), all transparent and click-through.
- **Performance measurement**: FPS counter, CPU% and memory sampled from
  `/proc`, printed every 5s and written to a JSON file.

## Requirements

```bash
# System (on the Pi)
sudo apt install mpv libmpv2 cage

# Python
pip install -r scripts/pyside6_mpv_prototype/requirements.txt
```

## Run (cage / Wayland — the working path)

```bash
cage -- python scripts/pyside6_mpv_prototype/slideshow.py \
    --media data/media/sample_media
```

The app must run under **cage** (the Wayland compositor) with
`QT_QPA_PLATFORM=wayland`. This is the same compositor metixel uses, so the
prototype runs in the real deployment environment.

Options:

| Flag | Default | Description |
|---|---|---|
| `--media` | `data/media/sample_media` | Directory of images + videos |
| `--image-duration` | `5` | Seconds each image is shown |
| `--video-max` | `15` | Max seconds a video plays before advancing |
| `--crossfade` | `1.0` | Crossfade duration in seconds |
| `--fullscreen` | `true` | Start fullscreen |
| `--hide-cursor` | `true` | Hide the mouse cursor |
| `--out` | `benchmark.json` | Where to write FPS/CPU/mem results |
| `--duration` | `0` | Auto-quit after N seconds (0 = run forever) |
| `--screenshot` | — | Capture the window to a PNG after 3s, then quit |

## How to interpret the output

Every 5 seconds the app prints a line like:

```
[bench] fps=24.0 cpu=15.9% mem=32.3% (avail 1358MB / 2GB)
```

- **fps** — rendered frame rate. For images this is the Qt repaint rate; for
  videos it's mpv's `estimated-vf-fps`. Target ≥30fps.
- **cpu** — total system CPU utilisation (sampled from `/proc/stat`).
- **mem** — memory used as a % of total, plus available MB.

The same data is appended to the JSON file (`--out`) for later analysis.

## Benchmark results (Pi 5, cage, 1080p video)

| Metric | Video (1080p) | Images |
|---|---|---|
| FPS | **24.0** | — |
| CPU | **15.9%** | ~0.7% |
| Memory | 32% used | 31% used |

The video decodes at full 24fps with modest CPU. Images are essentially free
(~0.7% CPU). This is a strong result — far better than the Chromium kiosk on
the same hardware.

## Virtual matte verification

The virtual matte (`object-fit: contain`) was verified on the Pi 5 by
capturing screenshots (`--screenshot`) and analysing the bars. The matte is
**blue** (`QColor(0,0,255)`) and the media is centred at **70%** of the
screen (`FIT_FRACTION = 0.70`):

| Media | Result |
|---|---|
| Video (16:9) | Centred at 70%, blue matte border |
| 3:2 image | Centred at 70%, blue matte border |
| 16:9 image | Centred at 70%, blue matte border |

The matte works identically for images and videos. The matte colour and fit
fraction are configurable via `ImageWidget.MATTE_COLOR` and
`ImageWidget.FIT_FRACTION`.

## mpv integration approach (the working recipe)

This prototype embeds mpv video via **python-mpv's `MpvRenderContext`** (the
libmpv OpenGL render API), compositing mpv's output as a texture inside a
`QOpenGLWidget`. This is the same approach used by
[booru-viewer](https://github.com/pxlwh/booru-viewer) and works natively on
Wayland (no XWayland needed).

### Critical: the LC_NUMERIC locale fix

Qt's `QApplication` constructor resets `LC_NUMERIC` to the system locale
(e.g. `en_GB.UTF-8`), which breaks libmpv's `mpv_create()` — it returns NULL
and crashes. **You must reset the locale AFTER creating the QApplication:**

```python
app = QApplication(sys.argv)
locale.setlocale(locale.LC_NUMERIC, "C")  # AFTER app creation!
```

Setting it before app creation does not survive Qt's init.

### Key requirements for the render API

1. **`vo="libmpv"`** + python-mpv `MpvRenderContext` (not hand-rolled ctypes).
2. **`ensure_gl_init()` BEFORE `play()`** — otherwise mpv deselects the video
   track ("Video: no video").
3. **`fbo: self.defaultFramebufferObject()`** in `paintGL` — NOT `0`.
   `QOpenGLWidget` uses a texture-backed FBO; passing `0` renders off-screen
   (black screen).
4. **`_frame_ready` Signal** from the mpv thread → `self.update()` on the main
   thread to trigger repaints.
5. Run under `cage` with `QT_QPA_PLATFORM=wayland`.

### Crossfade: use QGraphicsOpacityEffect, NOT windowOpacity

The Wayland plugin does **not** support window opacity ("This plugin does not
support setting window opacity"). Animating `windowOpacity` leaves the window
black. Use a `QGraphicsOpacityEffect` on the image widget instead:

```python
self._image_fade_effect = QGraphicsOpacityEffect(self.image_widget)
self.image_widget.setGraphicsEffect(self._image_fade_effect)
self._fade = QPropertyAnimation(self._image_fade_effect, b"opacity", self)
```

Without this fix, images show as black screens (the video still plays because
mpv renders directly).

### Video matte: render mpv full-screen, then paint the matte border

To apply the matte to videos (blue background, video centred at 70%), render
mpv directly into the widget's default FBO at full size (mpv letterboxes the
video itself), then paint the blue matte border over it with `QPainter`:

```python
def paintGL(self):
    # Render mpv full-screen into the widget's default FBO.
    self._ctx.render(
        opengl_fbo={
            "fbo": self.defaultFramebufferObject(),
            "w": w, "h": h, "internal_format": 0,
        },
        flip_y=True,
    )
    self._ctx.report_swap()

    # Paint the blue matte border, leaving only the centred 70% rect.
    painter = QPainter(self)
    painter.fillRect(0, 0, w, dy, ImageWidget.MATTE_COLOR)          # top
    painter.fillRect(0, dy + dh, w, h - dy - dh, ImageWidget.MATTE_COLOR)  # bottom
    painter.fillRect(0, dy, dx, dh, ImageWidget.MATTE_COLOR)        # left
    painter.fillRect(dx + dw, dy, w - dx - dw, dh, ImageWidget.MATTE_COLOR)  # right
    painter.end()
```

**Why not a separate FBO + `glBlitFramebuffer`?** That approach segfaults on
the Pi. Blitting between a depth-attached FBO and the widget's default FBO
crashes. Rendering mpv full-screen and painting the matte border over it is
robust and avoids the FBO entirely.

**Also disable mpv's OSD** (`osd_level=0`) — otherwise mpv's on-screen text
("Video --vid=1", volume, etc.) is baked into the video frame and appears
behind the matte/images.

### Why not the `wid` embedding approach?

The `wid` embedding (passing a Qt window ID to mpv) only works under **X11**,
not Wayland. Under cage it cannot work. The render API approach works on both
X11 and Wayland, so it is the correct choice for metixel (which uses cage).

Under **eglfs**, `wid` embedding also fails, but for a different reason: Qt's
eglfs already holds the DRM master, so mpv's `vo=gpu`/`hwdec=drm` cannot
acquire a second one (`Failed to acquire DRM master: Permission denied`).
The render API approach (`vo=libmpv`) is again the correct choice — it
renders into Qt's existing GL context and gets zero-copy via
`hwdec=drm` + `gpu_hwdec_interop=drmprime`.

## Graceful degradation

- If mpv fails to play a video, it's skipped and the slideshow continues.
- If a video can't be decoded, a warning is logged and it moves on.
- A missing media directory is handled gracefully (shows a message).

---

# Graphics stack decision: cage/Wayland + v4l2m2m on BOTH Pi 5 and Pi 3

For **consistency across the fleet**, both the Pi 5 and the Pi 3 run the
**same** graphics stack: **cage/Wayland + PySide6 + mpv (v4l2m2m)**. A single
code path is simpler to maintain and test than two platform-specific
rendering stacks.

## Why not eglfs + drmprime?

An eglfs + drmprime zero-copy path was prototyped and proven on the Pi 5
(V3D GPU, ~8–14% CPU), but it **fails on the Pi 3** (VC4 GPU). The Pi 3's
VC4 GL 2.1 driver lacks the `GL_OES_EGL_image` dmabuf interop extension that
drmprime requires, so it falls back to software decode (85–94% CPU):

```
[mpv:libmpv_render/drmprime] drmprime hwdec requires at least one dmabuf interop backend.
[mpv:vd] Using software decoding.
```

Since the Pi 3 cannot use it, and consistency is preferred, both platforms
use the cage/Wayland + v4l2m2m path. The eglfs experiment files have been
removed.

## The Pi 3 bottleneck is memory bandwidth, not CPU

Benchmarking the Pi 3 (cage/Wayland + v4l2m2m) across a resolution gradient
at 8 Mbps showed that **bitrate is irrelevant** — the cost is purely
resolution-driven, and the real limit is the Pi 3's **memory bandwidth**
(shared between CPU and GPU on the VC4 SoC), not CPU utilisation:

| Resolution | Pixels | FPS | CPU | Smooth? |
|---|---|---|---|---|
| 720p (1280×720) | 0.92M | 24.0 | ~31% | ✅ |
| 810p (1440×810) | 1.17M | 24.0 | ~36% | ✅ |
| 900p (1600×900) | 1.44M | 24.0 | ~40% | ❌ choppy |
| 990p (1760×990) | 1.74M | 24.0 | ~41% | ❌ choppy |
| 1080p (1920×1080) | 2.07M | 24.0 | ~36% | ❌ choppy |

**810p is the Pi 3's smooth threshold.** Anything above it is choppy, even
though CPU stays under ~41% — the memory bandwidth for the copy-back + GL
upload saturates.

## The chosen direction

1. **cage/Wayland** on both Pi 5 and Pi 3 (not eglfs).
2. **Hide the cursor with ydotool** at a **0.2s delay** after cage starts
   (see `scripts/wayland_cursor/` — the off-screen move works reliably with
   that delay).
3. **Use PySide6 + mpv** (the `slideshow.py` render-API approach) on both.
4. **For the Pi 3, transcode videos down to 720p** in the metixel Phase 2
   (OPTIMISE) pipeline. 720p plays smoothly at ~31% CPU with plenty of
   headroom. The Pi 5 can play 1080p natively (V3D GPU, no memory-bandwidth
   limit), but runs the same stack for consistency.

## Files

| File | Purpose |
|---|---|
| `slideshow.py` | The working prototype (cage/Wayland, python-mpv render API, blue matte) |
| `requirements.txt` | Python dependencies |
| `analyze_matte.py` | Analyse screenshots to verify the virtual matte |
| `capture_matte.sh` | Capture matte screenshots on the Pi |
| `dev/` | Diagnostic scripts used during development (check_*, test_*, run_*) |

## mpv option gotchas (learned the hard way)

| Option | Correct value | Wrong value that fails |
|---|---|---|
| `gpu_hwdec_interop` | `drmprime` | `drmprime-drm` (invalid) |
| `hwdec` | `drm` (uses drmprime interop) | `drmprime` (invalid) |
| `display-fps-override` | `30.0` | `display-fps` (option doesn't exist) |
| `vd-lavc-skipidct` | omit | `0` (invalid value) |
| `video-sync-max-audio-change` | `6.0` (float) | `6` (int, wrong format) |