# Metixel Photoframe — Features

A comprehensive list of every feature in Metixel Photoframe, organized by subsystem.

---

## Display & Rendering

- **Hardware-accelerated OpenGL ES 2.0** rendering via pi3d + Mesa EGL
- **Automatic native resolution detection** — set `width: 0` in config, pi3d detects the display
- **Two-texture ping-pong GPU pipeline** — active slot displayed while inactive slot preloads the next image
- **Smooth transitions** — crossfade, fade-through-black, or instant cut; configurable duration (default 2500ms)
- **Configurable fit modes** — contain, cover, fill, with smart-cover for opposite-orientation images
- **Matte bars** — configurable RGB color for letterboxing/pillarboxing
- **Boot screen** — animated Metixel logo with rotating spinner; smooth 0.8s ease-out fade to first slide
- **30 FPS render loop** — capped for Pi Zero 2 W (512MB RAM) compatibility
- **Display sleep scheduler** — configurable on/off times (e.g. off at 22:00, on at 07:00)
- **Display power control** — DRM DPMS via sysfs on KMS, or `vcgencmd display_power` on legacy

---

## Media Pipeline (4-Phase)

```
Phase 1: WATCH   → FolderWatcher gathers metadata only (type, dims, codec)
Phase 2: OPTIMISE → OptimisationQueue thresholds + processes (images first, then videos)
Phase 3: QUEUE   → Slideshow playlist (ready-to-play items only)
Phase 4: SYNC    → Immich downloads to media/sync/immich/ (picked up by Phase 1)
```

- **Background processing** — optimisation runs in a daemon thread, decoupled from rendering
- **Content-hash deduplication** — identical files (by hash) are never processed twice
- **Adaptive CPU throttling** — sleep duration scales with `/proc/loadavg` to keep the Pi responsive during batch processing
- **Atomic playlist writes** — temp file + `os.replace()` prevents corruption on power loss
- **Playlist hot-reload** — backend adds items → frontend picks them up incrementally without restarting the slideshow

---

## Image Support

| Feature | Detail |
|---|---|
| **Formats** | JPEG, PNG, BMP, GIF, WebP |
| **Auto-resize** | Images exceeding display resolution are resized down during OPTIMISE |
| **Thumbnails** | 320px thumbnails generated during processing; shown in the web UI media browser |
| **EXIF auto-rotation** | Orientation tag respected, image rotated before display |
| **OOM guard** | Uncached originals > 8 MB are skipped — backend provides a cached (resized) version |
| **GPU-friendly** | Textures use `GL_RGB565` (2 bytes/pixel) when possible to save VRAM |

---

## Video Support

| Feature | Detail |
|---|---|
| **Playback** | VLC with hardware-accelerated H.264 decode on Pi 2/3 |
| **Transcoding** | ffmpeg converts non-H.264 or oversized videos during OPTIMISE; CRF-based quality control |
| **Pre-extracted frames** | First frame (`.1.frame`) and last frame (`.2.frame`) JPEGs cached during OPTIMISE — frontend never runs ffmpeg |
| **Non-blocking state machine** | VLC plays on top of the slideshow; frame swaps underneath are invisible |
| **Last-frame swap** | VLC's window is covered by a cached last-frame JPEG at 80% of video duration for a seamless transition |
| **Guardrails** | Max duration filter, transcoding enabled/disabled toggle, playback enabled/disabled master switch |
| **Pi Zero 2 W** | Untested — image-only; no video, optimisation, or transcoding |

---

## Media Sources

| Source | How it works |
|---|---|
| **Local folder** | Watch paths configured in the Web UI — new files auto-detected by polling |
| **Network share** | Mount SMB/NFS shares, add mount point as a watch path |
| **Immich** | Configure server URL + API key — albums, favorites, and people auto-sync on configurable interval |
| **USB drive** | Add the USB mount point as a watch path |

---

## Web Dashboard (port 8080)

- **Vanilla JS SPA** — under 200 KB, no React/Angular dependency
- **Configuration** — all settings exposed with per-section save buttons
- **Media library** — thumbnail grid browser with video duration badges and transcode status
- **Immich management** — album selection, sync status, manual sync trigger
- **System logs** — live log viewer with configurable log level (DEBUG / INFO / WARNING / ERROR / NONE)
- **Network** — Wi-Fi scanning, connection management, AP mode status
- **Display settings** — resolution info, sleep schedule
- **Updates** — OTA update check and apply from GitHub releases (stable, beta, dev channels)
- **Captive portal** — PIN-gated Wi-Fi setup page served when AP fallback is active
- **Auto-reconnect** — page reloads automatically when the backend restarts

---

## System & Reliability

| Feature | Detail |
|---|---|
| **Process manager** | Two systemd services: `metixel-backend` (Flask + processing) and `metixel-cage` (display renderer under cage + XWayland) |
| **Atomic config** | Config never written directly — temp file + `os.replace()` prevents corruption on power loss |
| **Config hot-reload** | mtime polling detects file changes; both backend and frontend reload without restart |
| **Graceful degradation** | Never crash, never show a traceback — log errors and continue with available media |
| **Quiet boot** | No kernel messages, no login prompt — display goes straight to the boot screen |
| **Log rotation** | 5 log files, configurable log level applied to both processes |
| **Samba shares** | Production: media folder only (`metixel-media`); Dev: full project tree (`metixel`) |

---

## IPC & Control

- **Unix domain socket** (SOCK_DGRAM) for backend → frontend real-time commands
- **Commands**: next, prev, pause, resume, switch_album, power_on/off, show/dismiss message
- **Frontend → backend** HTTP signal for slideshow-started (defers network checks until slideshow is running)
- **MQTT client** — Home Assistant integration (publish state, receive next/prev/pause commands)
- **HDMI-CEC** — respond to TV remote play/pause/stop events
- **IR remote** — configurable via LIRC

---

## Networking

- **Wi-Fi scanning + connection** via nmcli (NetworkManager)
- **AP fallback mode** — when no Wi-Fi is configured, starts a captive portal hotspot with PIN-gated security
- **Auto-deactivation** — AP mode stops automatically when a real network connection is established
- **Network monitor deferral** — AP fallback countdown waits until the slideshow has started to avoid CPU contention during initial media processing
- **Connectivity check** — verifies non-AP IP address before serving the dashboard

---

## Memory & Performance

| Constraint | Strategy |
|---|---|
| **Pi Zero 2 W (512MB)** | Untested — image-only; no video, optimisation, or transcoding |
| **GPU textures** | `GL_RGB565` (2 bytes/pixel) preferred over `GL_RGBA`; max 3 GPU-resident textures at any time |
| **CPU memory** | `Texture(free_after_load=True)` releases CPU-side array after GPU upload |
| **Batch processing** | Process media one file at a time in a subprocess that exits to release memory |
| **Garbage collection** | `gc.collect()` called after large image processing |
| **Swap** | 904MB swap file required on all models |
| **Adaptive yield** | Sleep between image optimisations scales with 1-minute load average (0.05s → 1.0s) |

---

## Display Protection

- **Pixel shifting** — configurable ±2px cyclic offset every N minutes to prevent burn-in
- **Sleep hours** — screen automatically dims or turns off during configured sleep window
- **Screensaver fallback** — black screen with dimmed logo when no media is queued
- **Deep sleep** — `vcgencmd display_power 0` (legacy) or DRM DPMS (KMS) for complete display shutdown

---

## Dev & Testing

| Tool | Purpose |
|---|---|
| **TkBackend** | tkinter-based software renderer for desktop development (no Pi hardware needed) |
| **Pi3dBackend** | Production backend for Raspberry Pi (Mesa EGL via cage/XWayland) |
| **WaylandBackend** | Future backend for Phase 2 (PyOpenGL + EGL on Wayland/DRM) |
| **Backend auto-detection** | Factory in `metixel.display` selects the correct backend at runtime |
| **pytest** | Test suite with coverage reporting |
| **ruff** | Fast Python linter |
| **mypy** | Static type checking |
| **Dev Samba share** | Full project tree shared over SMB for VS Code remote editing |
