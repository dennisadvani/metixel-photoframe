# CLAUDE.md — Metixel Photoframe Project Instructions for AI Assistants

## Project Summary

**Metixel Photoframe** is a custom, open-source Digital Photo Frame operating system and application suite targeting:
- **Phase 1:** Raspberry Pi 2, 3, and Zero 2 W (512MB–1GB RAM, Mesa/DRM on Trixie)
- **Phase 2:** Raspberry Pi 4, 5, and non-Pi SBCs like the Radxa Zero 3W (Mesa/DRM/Wayland)

The application is written in **Python 3** with a display backend abstraction that isolates the rendering layer (pi3d on Phase 1, PyOpenGL on Phase 2) from the presentation logic. The `Pi3dBackend` works on any Pi with pi3d installed — it runs under cage (minimal Wayland compositor) + XWayland on Trixie/Bookworm.

## Core Rules (ALWAYS follow these)

1. **Consult the architecture first.** Before proposing ANY code change, read `ARCHITECTURE.md` in the repository root. If you've lost context, run:
   ```bash
   cat ARCHITECTURE.md
   ```
   This file contains the complete system design, component relationships, and implementation roadmap.

2. **Respect the display backend abstraction.** Never import `pi3d` directly in presentation or widget code. Always use `metixel.display.backend.DisplayBackend` — the factory in `metixel.display.__init__` auto-detects the hardware and returns the correct backend. The only file that may import pi3d is `metixel/display/dispmanx_backend.py`.

3. **Phase 1 hardware varies in capability.** The Pi Zero 2 W has 512MB RAM; Pi 2/3 have 1GB. Always:
   - Use `Texture(free_after_load=True)` to release CPU memory after GPU upload
   - Prefer `GL_RGB565` (2 bytes/pixel) over `GL_RGBA` (4 bytes/pixel) for GPU textures
   - Limit GPU-resident textures to 3 maximum at any time (current, next, blend)
   - Process media one file at a time in a subprocess that exits to release memory
   - Call `gc.collect()` after large image processing
   - Cap the render loop at 30 FPS (not 60)
   - The Pi Zero 2 W (512MB) is the tightest constraint — test there for memory regressions

4. **Display protection is mandatory.** All screens can burn in. Implement:
   - Configurable pixel shifting (±2px cyclic offset every N minutes)
   - Screen dimming during "sleep" hours
   - A screensaver fallback if no media is queued
   - Use `vcgencmd display_power 0` for deep sleep (legacy) or DRM DPMS via sysfs on KMS

5. **Graceful degradation is required.** If:
   - The GPU memory is too low (<64MB) → log warning, fallback to static images only
   - Hardware video decode unavailable → skip video files, log warning
   - Immich server unreachable → continue slideshow with cached media, retry with exponential backoff
   - Network down → continue with local media only
   - Never crash. Never show a traceback on screen. Log errors and continue.

6. **Configuration is atomic.** Never write `config.json` directly. Always write to a temp file and use `os.replace()` to atomically swap. The frontend watches for `inotify IN_MODIFY` events.

7. **Systemd is the process manager.** Two services: `metixel-backend.service` and `metixel-cage.service` (on Trixie, the frontend runs under cage). The frontend depends on the backend. Do not propose init.d scripts or cron-based startup.

8. **The web UI is served from the backend process** on port 8080. It's a lightweight SPA (vanilla JS). No React/Angular — keep the bundle under 200KB.

9. **Test on desktop first.** The `dev_backend.py` (pygame-based) allows running the entire stack on a development machine without Pi hardware. Always test there before targeting ARM.

10. **Phase 1 vs Phase 2 awareness.** When writing code:
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
| `metixel/backend/state.py` | Atomic config read/write + change notification |
| `metixel/frontend/presentation/engine.py` | Slideshow logic (platform-agnostic) |
| `metixel/shared/config.py` | Config schema, validation, defaults |
| `etc/config.json` | Runtime configuration file |
| `scripts/quiet_boot.sh` | Silent boot configuration |

## If You're Unsure

- Re-read `ARCHITECTURE.md` sections relevant to the task
- Check if the change affects both Phase 1 and Phase 2
- Verify memory constraints for Pi Zero 2 W (512MB)
- Ensure the display backend abstraction isn't leaked
- Run `cat ARCHITECTURE.md` to re-establish project context
