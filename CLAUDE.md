# CLAUDE.md — Metixel Photoframe Project Instructions for AI Assistants

## Project Summary

**Metixel Photoframe** is a custom, open-source Digital Photo Frame operating system and application suite targeting:
- **Phase 1:** Raspberry Pi 2, 3, Zero 2 W, and Pi 5 (512MB–8GB RAM, Mesa/DRM on Trixie). Pi 5 is the recommended platform at 2GB+.
- **Phase 2:** Raspberry Pi 4 and non-Pi SBCs like the Radxa Zero 3W (Mesa/DRM/Wayland)

The application is written in **Python 3** with a display backend abstraction that isolates the rendering layer (pi3d on Phase 1, PyOpenGL on Phase 2) from the presentation logic. The `Pi3dBackend` works on any Pi with pi3d installed — it runs under cage (minimal Wayland compositor) + XWayland on Trixie/Bookworm.

### Media Pipeline (4-Phase)

```
Phase 1: WATCH   → FolderWatcher gathers metadata only (type, dims, codec)
Phase 2: OPTIMISE → OptimisationQueue thresholds + processes (images first, then videos)
Phase 3: QUEUE   → Slideshow playlist (ready-to-play items only)
Phase 4: SYNC    → Immich downloads to media/sync/immich/ (picked up by Phase 1)
```

**Thumbnails and video frames** are generated ONLY by the backend processors (`image.py` / `video.py`). The frontend (`engine.py`) does NOT generate thumbnails or extract video frames — it never runs ffmpeg/ffprobe.

## Core Rules (ALWAYS follow these)

1. **Consult the architecture first.** Before proposing ANY code change, read `ARCHITECTURE.md` in the repository root. If you've lost context, run:
   ```bash
   cat ARCHITECTURE.md
   ```
   This file contains the complete system design, component relationships, and implementation roadmap.

2. **Respect the display backend abstraction.** Never import `pi3d` directly in presentation or widget code. Always use `metixel.display.backend.DisplayBackend` — the factory in `metixel.display.__init__` auto-detects the hardware and returns the correct backend. The only file that may import pi3d is `metixel/display/dispmanx_backend.py`.

3. **Respect the 4-phase media pipeline.** Media flows through four distinct phases:
   - **Phase 1 (Watch):** `FolderWatcher` gathers metadata only — file type, dimensions, video codec. Does NOT process, resize, or transcode. Pushes `MediaItem` stubs to the `OptimisationQueue`.
   - **Phase 2 (Optimise):** `OptimisationQueue` (background thread in `BackendDaemon`) routes ALL media through the appropriate processor. Images exceeding display resolution are resized. **All videos** go through `VideoProcessor.process()` — even H.264 videos within resolution limits — because first-frame (``.1.frame``) and last-frame (``.2.frame``) JPEG extraction is always required. The processor skips the expensive transcode step for videos already in optimal format. Image optimisation runs before video optimisation.
   - **Phase 3 (Queue):** `StateManager` maintains the slideshow playlist. `PresentationEngine` reads from it. Only ready-to-play items are in the playlist.
   - **Phase 4 (Sync):** `ImmichSyncer` downloads to `media/sync/immich/` (default). This directory is a watch path — Phase 1 picks up new files on the next poll.

4. **Thumbnails and video frames belong to the backend.** `ImageProcessor` and `VideoProcessor` generate thumbnails, first frames, and last frames during optimisation. The frontend (`engine.py`) does NOT generate thumbnails, extract video frames, or run ffmpeg/ffprobe. All frame generation was removed from the presentation engine — it only loads pre-generated cache files referenced by `MediaItem.first_frame_path` and `MediaItem.last_frame_path`.

5. **Phase 1 hardware varies in capability.** The Pi Zero 2 W has 512MB RAM; Pi 2/3 have 1GB. Always:
   - Use `Texture(free_after_load=True)` to release CPU memory after GPU upload
   - Prefer `GL_RGB565` (2 bytes/pixel) over `GL_RGBA` (4 bytes/pixel) for GPU textures
   - Limit GPU-resident textures to 3 maximum at any time (current, next, blend)
   - Process media one file at a time in a subprocess that exits to release memory
   - Call `gc.collect()` after large image processing
   - Cap the render loop at 30 FPS (not 60)
   - The Pi Zero 2 W (512MB) is the tightest constraint — test there for memory regressions

6. **Display protection is mandatory.** All screens can burn in. Implement:
   - Configurable pixel shifting (±2px cyclic offset every N minutes)
   - Screen dimming during "sleep" hours
   - A screensaver fallback if no media is queued
   - Use `vcgencmd display_power 0` for deep sleep (legacy) or DRM DPMS via sysfs on KMS

7. **Graceful degradation is required.** If:
   - The GPU memory is too low (<64MB) → log warning, fallback to static images only
   - Hardware video decode unavailable → skip video files, log warning
   - Immich server unreachable → continue slideshow with cached media, retry with exponential backoff
   - Network down → continue with local media only
   - Never crash. Never show a traceback on screen. Log errors and continue.

8. **Configuration is atomic.** Never write `config.json` directly. Always write to a temp file and use `os.replace()` to atomically swap. The frontend watches for `inotify IN_MODIFY` events.

9. **Systemd is the process manager.** Two services: `metixel-backend.service` and `metixel-cage.service` (on Trixie, the frontend runs under cage). The frontend depends on the backend. Do not propose init.d scripts or cron-based startup.

10. **The web UI is served from the backend process** on port 8080. It's a lightweight SPA (vanilla JS). No React/Angular — keep the bundle under 200KB.

11. **Test on desktop first.** The `dev_backend.py` (pygame-based) allows running the entire stack on a development machine without Pi hardware. Always test there before targeting ARM.

12. **Phase 1 vs Phase 2 awareness.** When writing code:
    - Check `metixel.display.__init__.detect_backend()` to know which pipeline is active
    - Phase 1: pi3d runs under cage + XWayland on Trixie (Mesa EGL)
    - Phase 2: uses Mesa EGL, Wayland compositor, or DRM/KMS directly
    - `/opt/vc/` paths only exist on legacy Bullseye; never assume them on Trixie

## Build & Run Commands

```bash
# Install dependencies (Phase 1 — Trixie)
pip install -r requirements-phase1.txt

# Run the backend daemon (development)
python -m metixel --mode backend --config etc/config.json

# Run the frontend renderer (development, uses dev_backend on desktop)
python -m metixel --mode frontend --config etc/config.json

# Run the frontend under cage (Trixie/Pi hardware)
cage -- python3 -m metixel --mode frontend --config etc/config.json

# Run tests
python -m pytest tests/ -v

# Build Phase 1 OS image
sudo bash scripts/build_phase1.sh

# Build Phase 2 OS image
sudo bash scripts/build_phase2.sh

# Lint
ruff check metixel/

# Type check
mypy metixel/
```

## Key File Locations

| File | Purpose |
|---|---|
| `ARCHITECTURE.md` | **READ THIS FIRST** — complete system design |
| `metixel/display/backend.py` | DisplayBackend ABC — the interface everything renders through |
| `metixel/display/dispmanx_backend.py` | Phase 1 pi3d implementation |
| `metixel/display/wayland_backend.py` | Phase 2 PyOpenGL implementation (future) |
| `metixel/display/dev_backend.py` | Desktop dev: pygame-based software renderer |
| `metixel/display/__init__.py` | Backend auto-detection factory |
| `metixel/backend/state.py` | Atomic config read/write + change notification + playlist management |
| `metixel/backend/daemon.py` | Main daemon — starts all background threads including OptimisationQueue |
| `metixel/backend/processing/optimisation_queue.py` | 4-phase pipeline orchestrator: classifies, thresholds, optimises, queues |
| `metixel/backend/processing/image.py` | Image resize + thumbnail generation + `needs_optimisation()` threshold check |
| `metixel/backend/processing/video.py` | ffmpeg transcode + thumbnail + first/last frame extraction + `needs_optimisation()` codec/resolution check |
| `metixel/backend/sync/folder_watcher.py` | Phase 1 WATCH: metadata-only scanning, pushes to OptimisationQueue |
| `metixel/backend/sync/immich.py` | Phase 4 SYNC: Immich API client, downloads to `media/sync/immich/` |
| `metixel/frontend/presentation/engine.py` | Slideshow logic (platform-agnostic) — does NOT generate thumbnails, extract frames, or run ffmpeg/ffprobe |
| `metixel/shared/config.py` | Config schema, validation, defaults (includes `image` and `video` thresholds) |
| `etc/config.json` | Runtime configuration file |
| `scripts/quiet_boot.sh` | Silent boot configuration |

## If You're Unsure

- Re-read `ARCHITECTURE.md` sections relevant to the task
- Check if the change affects both Phase 1 and Phase 2
- Verify memory constraints for Pi Zero 2 W (512MB)
- Ensure the display backend abstraction isn't leaked
- **Video frame extraction is a backend responsibility.** The frontend must never import ffmpeg/ffprobe or extract frames. Frames are generated by `VideoProcessor` during Phase 2 (OPTIMISE) and referenced via `MediaItem.first_frame_path` / `MediaItem.last_frame_path`.
- Run `cat ARCHITECTURE.md` to re-establish project context
