# CLAUDE.md — Metixel Photoframe Project Instructions for AI Assistants

## Project Summary

**Metixel Photoframe** is a custom, open-source Digital Photo Frame operating system and application suite targeting:
- **Phase 1:** Raspberry Pi 2, 3, 4, 5, and Zero 2 W (Mesa/DRM on Trixie). Pi 5 (2GB+) is the recommended platform. Pi 4 is supported but untested. Pi 2 and Pi Zero 2 W are 32‑bit manual‑install only — no pre‑built image available. Pi Zero 2 W (512MB) is untested and image‑only (no video, optimisation, or transcoding).
- **Phase 2:** Non-Pi SBCs like the Radxa Zero 3W (Mesa/DRM/Wayland)

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

2. **Respect the display backend abstraction.** Never import `pi3d` directly in presentation or widget code. Always use `metixel.display.backend.DisplayBackend` — the factory in `metixel.display.__init__` auto-detects the hardware and returns the correct backend. The only file that may import pi3d is `src/metixel/display/dispmanx_backend.py`.

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
   - Pi Zero 2 W (512MB) is untested — not a current deployment target

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

9. **Systemd is the process manager.** Three services: `metixel-backend.service`, `metixel-cage.service` (on Trixie, the frontend runs under cage), and `metixel-cursor-hider.service` (hides the cage cursor via a persistent virtual mouse). The frontend depends on the backend. Do not propose init.d scripts or cron-based startup.

10. **The web UI is served from the backend process** on port 8080. It's a lightweight, **modular vanilla-JS SPA built from native ES6 modules** — no bundler, no build step, no React/Angular, no frameworks. **Never reintroduce a single-file JS monolith** — see Web UI Style Guide → **JavaScript architecture** below.

11. **Test on desktop first.** The `tk_backend.py` (tkinter-based) allows running the entire stack on a development machine without Pi hardware. Always test there before targeting ARM.

12. **Phase 1 vs Phase 2 awareness.** When writing code:
    - Check `metixel.display.__init__.detect_backend()` to know which pipeline is active
    - Phase 1: pi3d runs under cage + XWayland on Trixie (Mesa EGL)
    - Phase 2: uses Mesa EGL, Wayland compositor, or DRM/KMS directly
    - `/opt/vc/` paths only exist on legacy Bullseye; never assume them on Trixie

13. **Respect the Clean Architecture (`src/` layout + dependency inversion).**
    - **`src/` layout:** All application packages live under `src/metixel/`. Never add new top-level packages at the repository root.
    - **Ports & Adapters (dependency inversion):** Core business logic must NEVER import third-party libraries directly (`requests`, `paho-mqtt`, `libcec`, ...). Define a `typing.Protocol` *port* in `src/metixel/shared/ports.py`, a concrete *adapter* in `src/metixel/shared/adapters.py`, and inject the port via constructor with a **real default** (behaviour stays identical when nothing is injected). Bundle injectable dependencies with the `Ports` dataclass (`BackendDaemon(..., ports=Ports(http=...))`).
    - **Composition root:** `src/metixel/__main__.py` is thin — CLI parsing + logging only; it delegates to `build_backend()` (`backend/daemon.py`) and `build_renderer()` (`frontend/renderer.py`). Never put business logic in `__main__.py`.
    - **New external systems:** when adding a new external dependency, add a Protocol + adapter — do not import the library into core. See `ARCHITECTURE.md` §6.7.

14. **Tests mirror the package and use Protocol fakes.** Unit tests live in `testing/unit_tests/backend|frontend|display|shared/`, mirroring `src/metixel/...`. Tests must NOT touch real hardware, the network, or systemd — inject fakes that implement the port Protocols (they are `@runtime_checkable`, so `isinstance(fake, HttpGateway)` works). Hardware-dependent tests use `pytest.importorskip(...)`. Web tests use the shared fixtures in `testing/unit_tests/backend/web/conftest.py` (real `create_app()` + mocked outbound deps).

15. **OTA is a thin bootstrap + atomic Blue/Green hand-off + startup self-heal.** The runtime pip
    deps (`requirements-pip.txt`) and system deps (`requirements-system.txt`) must be applied on
    every upgrade:
    - **Thin bootstrap (`update_manager._build_update_script`):** the generated OTA script only
      stops services, then delegates to `scripts/update.sh <target ref>` under the `live` symlink.
      The Blue/Green workflow (staging → strict install → remove obsolete → config backup →
      symlink swap → health-check → rollback) lives in `scripts/update.sh` *in the repo*, so the
      running (old) code is never relied on to know the current steps — a device upgrading from
      an old release applies the NEW version's install logic. Never bake install steps inline
      into the generated script.
    - **Atomic swap + rollback:** `update.sh` stages the new release into
      `/opt/metixel/releases/<version>`, installs packages **strictly** (any failure, e.g. no
      internet, aborts before the swap and deletes the staging dir), backs up the config, then
      `ln -sfn` flips the `/opt/metixel/live` symlink. It health-checks `/api/health` and rolls
      back (symlink flip + config restore) if the new release doesn't come up.
    - **Package lifecycle:** Metixel-managed packages are tracked in
      `/opt/metixel/data/installed_packages.json`; an update removes obsolete managed packages
      (apt remove / pip uninstall) — never pre-existing ones.
    - **Self-migrating first upgrade:** a device still on the old monolithic layout (no `/data`,
      no `/live`) is bridged by `ota_install.sh`, which detects the flat layout and runs
      `migrate_to_atomic.sh --no-restart --no-backup` BEFORE installing. Migration moves code
      into `releases/<ver>`, creates the `live` symlink, and rewrites systemd units; then
      `ota_install.sh` re-points the working repo at the migrated release (via the
      `MIGRATED_RELEASE_DIR=` line it prints) so `pip install -e` targets the new location.
      `logging.conf` lives at `/opt/metixel/data/etc/logging.conf` — `__main__.py` resolves it
      as `data_dir()/etc/logging.conf` (never `config_path.parent.parent` arithmetic). All
      persistent config (config.json + logging.conf) lives under `/data`.
    - **Versioned device fixups:** `ota_install.sh` runs one-time repair scripts from
      `scripts/fixups/` (listed in `scripts/fixups/manifest.txt`) to fix device-level issues
      that aren't packages or config files (e.g. `gpu_mem` in `/boot/firmware/config.txt`).
      Each runs exactly once per device, tracked in `/opt/metixel/data/installed_fixups.json`,
      and is warn-and-continue (a failure never aborts the update).
    - **Startup self-heal (`backend/dependencies.py`):** on boot, `BackendDaemon.run()` calls
      `ensure_runtime_dependencies()` which detects missing deps via `importlib.metadata` and
      installs them. This makes a **single** OTA resolve missing runtime deps (e.g. `pillow-heif`
      for HEIC) even for devices upgrading from code that predates the hand-off.
    - **Ownership/sudo:** the backend service is hardened (`ProtectHome=yes` → `/home` read-only;
      `ProtectSystem=full` → `/usr` read-only), so it CANNOT install to `~/.local` or the system
      dist-packages, and plain `sudo` inherits the hardened mount namespace. The self-heal must
      run pip as **root** via `sudo -n systemd-run --wait --collect --unit=metixel-deps` into the
      **system** dist-packages — the same location the OTA installs to (never `--user`; never
      split deps across `~/.local` and `/usr`).
    - Runtime deps go in `requirements-pip.txt`, NOT only in pyproject optional extras (those are
      skipped by `pip install -e .` and were the root cause of the HEIC/HEIF OTA bug).
    - Guarded by `testing/unit_tests/backend/test_update_manager.py` and `testing/unit_tests/backend/test_dependencies.py`.
    - **Startup self-heal (`backend/dependencies.py`):** on boot, `BackendDaemon.run()` calls
      `ensure_runtime_dependencies()` which detects missing deps via `importlib.metadata` and
      installs them. This makes a **single** OTA resolve missing runtime deps (e.g. `pillow-heif`
      for HEIC) even for devices upgrading from code that predates the hand-off.
    - **Ownership/sudo:** the backend service is hardened (`ProtectHome=yes` → `/home` read-only;
      `ProtectSystem=full` → `/usr` read-only), so it CANNOT install to `~/.local` or the system
      dist-packages, and plain `sudo` inherits the hardened mount namespace. The self-heal must
      run pip as **root** via `sudo -n systemd-run --wait --collect --unit=metixel-deps` into the
      **system** dist-packages — the same location the OTA installs to (never `--user`; never
      split deps across `~/.local` and `/usr`).
    - Runtime deps go in `requirements-pip.txt`, NOT only in pyproject optional extras (those are
      skipped by `pip install -e .` and were the root cause of the HEIC/HEIF OTA bug).
    - Guarded by `testing/unit_tests/backend/test_update_manager.py` and `testing/unit_tests/backend/test_dependencies.py`.

## Web UI Style Guide

The dashboard (`metixel/backend/web/`) is a vanilla-JS SPA with a burgundy-on-white design system. **Keep styling consistent** — follow these rules for any UI change.

### JavaScript architecture — native ES6 modules (no bundler)

The dashboard JS lives in `src/metixel/backend/web/static/js/` and is organised as **native ES6 modules** — no bundler, no build step, no minification, no framework. **Do not reintroduce a single-file monolith.**

- **`main.js`** is the ONLY entry point (loaded by `index.html` as `<script type="module" src="/static/js/main.js?v=N">`). It imports `core.js` + every page module, binds the nav/burger shell, registers pages, and boots the SPA. No page logic lives here.
- **`core.js`** is the shared-infrastructure module and must stay page-agnostic: the API layer (`apiGet`/`apiPut`/`apiPost` + private connection tracking), the SPA router (`navigateTo`/`registerPage`), `showToast`, `openDrawer`/`closeDrawer`, and DOM/string utils (`escapeHtml`, `sanitizeInt`, `setChecked`, `setValue`, `setStat`, `updatePowerButton`, `timeAgo`).
- **One module per feature area**, each exporting its page loader and keeping its own state module-private: `dashboard-page.js`, `settings-page.js`, `network-page.js`, `sync-page.js`, `media-page.js`, `logs-page.js`, `advanced-page.js`, `updates-page.js`.
- **Router pattern (no circular imports):** page modules call `registerPage("name", loader)`; `navigateTo(page)` dispatches through core's registry. `core.js` must never import page modules, and page modules must only import the cross-module symbols listed below.
  - Allowed cross-module edges (keep the import graph a DAG): `sync-page → media-page` (`loadMedia`), `advanced-page → logs-page` (`refreshLogs`) and `advanced-page → updates-page` (`loadUpdateStatus`, `bindUpdateControls`). Everything else imports `core.js` only.
- **Scoping & state:** use strict `import`/`export`; modules are strict-mode by default (no `"use strict"`, no IIFE wrapper). Keep module-level state private inside its own module — never share mutable state across page modules; only `core.js` holds cross-cutting state (API connection tracking).
- **Line endings:** all web JS files are **CRLF**. When a script regenerates files, normalise `\r\n`→`\n` internally and write `\n`→`\r\n`.
- **ES modules are deferred**, so `document` is fully parsed when module top-level code runs (e.g. `core.js` may cache `#nav-drawer`/`#nav-backdrop` at load).
- **Serving:** Flask serves `/static/` via `send_from_directory` with `Cache-Control: no-cache` and no CSP that would block modules — relative `import "./x.js"` works as-is. A backend restart is only needed when editing `templates/*.html` (Jinja template cache).
- **Keep it modular:** if a page module grows past ~500 lines, split it further (e.g. sub-helpers per concern) rather than consolidating. During refactors, move code verbatim — never change logic, API endpoints, or CSS classes.

### Icons — monochrome only

- Use **Google Material Symbols** (`material-symbols-outlined` font, already loaded in `index.html`) for ALL icons.
- **NEVER use coloured emoji as UI icons** (`✅`, `❌`, `⚠`, `⚠️`, `🔒`, `⏹`, `🖼`, …). Replace them with a Material Symbol or plain text.
- Standard pattern:
  ```html
  <span class="material-symbols-outlined" style="font-size:1em;vertical-align:middle">warning</span>
  ```
- Colour icons with `style="color:var(--text-muted)"` (or another token) — Material Symbols inherit `currentColor`.
- **Captive portal** (`captive.html`) does NOT load the Material Symbols font — use small inline SVG icons with `fill="currentColor"` there (see the lock / check-circle examples).

### Status colours — green is background-only

| State | Colour |
|---|---|
| Success **text** | `var(--text)` (white) — **never** `var(--success)` |
| Error text | `var(--danger)` (red) |
| Cancelled / neutral text | `var(--text-muted)` |
| Warning text | `#f0a030` (amber — no token exists) |
| Green `var(--success)` / `#059669` | backgrounds/accents ONLY: Connected button, toast background, progress-bar fill, connected-row border/tint |

### Use design tokens

Prefer CSS variables over raw hex: `var(--primary)` (brand burgundy `#8B1A2B`), `var(--text)`, `var(--text-secondary)`, `var(--text-muted)`, `var(--danger)`, `var(--success)`, `var(--border)`, `var(--bg)`, `var(--surface)`. The only raw colour allowed in text is the amber `#f0a030` (no token).

### Mobile (max-width 480px) button behaviour

The mobile block forces `button { width: 100% }`. Small inline buttons must keep their natural width — add `btn--sm` / `btn-browse` classes or scope the rule to `.watch-path-row button`. Never let an inline action (browse, fetch, set, add, remove) stretch full-width and crush a form row.

### Cache-busting

When editing `dashboard.css` or any file under `static/js/` (SPA entry point is `main.js`), **bump the `?v=` query** on both the stylesheet `<link>` and the `<script src>` in `index.html` (e.g. `main.js?v=15` → `v=16`). Otherwise browsers serve stale assets.

### Tailwind CSS build (dev machine only)

The dashboard uses **Tailwind CSS v4** for styling, but the Pi **never runs Tailwind**. CSS is built on the dev machine and the compiled output is committed.

- **Source:** `src/metixel/backend/web/static/css/input.css` — the Tailwind entry point. It imports `tailwindcss`, defines the design tokens in `@theme`, and imports the hand-written styles.
- **Hand-written styles:** `src/metixel/backend/web/static/css/custom.css` — the legacy design system, preserved verbatim. It is imported **inside `@layer components`** so Tailwind utilities (in `@layer utilities`, a higher-priority layer) can override it. **Do not move it out of the layer** — the un-layered universal reset `*, ::before, ::after { margin:0; padding:0 }` would otherwise beat every Tailwind utility and they'd all compute to 0.
- **Build:** `npm run build:css` (one-shot) or `npm run watch:css` (watch). Output goes to `dashboard.css` (minified), which is what the Pi serves.
- **Workflow:** edit `input.css` / `custom.css` / `index.html` → run `npm run build:css` → bump `?v=` in `index.html` → sync via the existing `scp` task. **Never build on the Pi.**
- **Config:** `tailwind.config.js` scans `templates/**/*.html` and `static/js/**/*.js` for class names.

### Premium design system (Slideshow Settings pattern)

The dashboard uses a premium, cinematic design language — Apple-like restraint, Sonos/consumer-electronics feel, art-gallery aesthetic. **Avoid** generic admin-dashboard styling, excessive cards, gradients everywhere, neon colours, glassmorphism, huge headings, or pill-shaped everything.

**Card anatomy** (every card follows this):
- Container: `bg-surface border border-border rounded-card p-6 mb-4 shadow transition-colors hover:border-border-light`
- Header: `flex items-center gap-2.5` with a `w-1 h-[1.1em] bg-primary rounded-sm` accent bar (vertically centered against the title via `items-center`, not `items-baseline`) + an uppercase `tracking-[0.14em]` eyebrow label on the right
- Section dividers: `border-t border-border/60` — used only at **semantic boundaries** (header→content, control-group→toggle-group, content→action), never between every row
- Action footer: `mt-5 pt-3 border-t border-border/60 flex justify-end`

**Setting rows** (label left, control right, hint below label):
```html
<div class="setting-row">
  <div class="setting-label">
    <span class="text-[0.85rem] font-medium text-text-secondary">Label</span>
    <span class="text-[0.72rem] text-text-muted leading-snug">Hint</span>
  </div>
  <div class="setting-control">
    <input ... class="input-premium">
  </div>
</div>
```

**Premium controls** (defined in `input.css` under `@layer components`):
- `.input-premium` — near-black fill `rgba(0,0,0,0.35)`, hairline `#2a2f45` border, soft burgundy focus ring
- `.range-premium` — thin 2px track, tactile 14px thumb with burgundy-tinted border
- `.checkbox-premium` — custom 18px square, burgundy fill + **SVG background-image checkmark** (NOT `::after` — pseudo-elements don't render on `<input>` elements)
- `.select-premium` — `appearance:none` with a custom SVG chevron

**Grouping rule:** group settings by **interaction type** (value controls vs on/off toggles) or by semantic concern, and separate groups with a single divider. Don't put a divider between every row.

### General

- No frameworks.
- Settings live in `.card` blocks with an `<h2>` title; fields use `.form-group` + `.form-label`; primary save buttons are `.btn--primary`.
- The sync task mirrors only `src/metixel/` — UI files under `src/metixel/backend/web/` ARE included, but `testing/` are not (copy separately).

## Build & Run Commands

```bash
# Install dependencies (Phase 1 — Trixie)
pip install -r requirements-pip.txt

# Run the backend daemon (development)
python -m metixel --mode backend --config etc/config.json

# Run the frontend renderer (development, uses tk_backend on desktop)
python -m metixel --mode frontend --config etc/config.json

# Run the frontend under cage (Trixie/Pi hardware)
cage -- python3 -m metixel --mode frontend --config etc/config.json

# Run tests
python -m pytest testing/unit_tests/ -v

# Build Phase 1 OS image
sudo bash scripts/build_phase1.sh

# Build Phase 2 OS image
sudo bash scripts/build_phase2.sh

# Lint
ruff check src/metixel/

# Type check
mypy src/metixel/
```

## Key File Locations

| File | Purpose |
|---|---|
| `ARCHITECTURE.md` | **READ THIS FIRST** — complete system design |
| `src/metixel/display/backend.py` | DisplayBackend ABC — the interface everything renders through |
| `src/metixel/display/dispmanx_backend.py` | Phase 1 pi3d implementation |
| `src/metixel/display/wayland_backend.py` | Phase 2 PyOpenGL implementation (future) |
| `src/metixel/display/tk_backend.py` | Desktop dev: tkinter-based software renderer |
| `src/metixel/display/cursor_hider.py` | Hides the cage/Wayland cursor via a persistent virtual absolute mouse (evdev) |
| `src/metixel/display/__init__.py` | Backend auto-detection factory |
| `src/metixel/backend/state.py` | Atomic config read/write + change notification + playlist management |
| `src/metixel/backend/daemon.py` | Main daemon — starts all background threads including OptimisationQueue + startup dependency self-heal |
| `src/metixel/backend/dependencies.py` | Startup dependency self-heal — detects/installs missing `requirements-pip.txt` deps as root via `sudo systemd-run` |
| `src/metixel/backend/update_manager.py` | OTA updates — thin bootstrap script delegating to `scripts/update.sh`, launched via `systemd-run` |
| `scripts/update.sh` | Atomic Blue/Green updater — staging → strict install → remove obsolete → config backup → symlink swap → health-check → rollback |
| `scripts/migrate_to_atomic.sh` | Migration of an old monolithic install to the `/data` + `/releases` + `/live` layout — invoked automatically by `ota_install.sh` on the first Blue/Green upgrade |
| `scripts/ota_install.sh` | OTA/system install steps (system packages + `pip install -e .` + `requirements-pip.txt`, strict by default) — run from the NEW checkout |
| `src/metixel/shared/paths.py` | Central path resolution: `install_root`/`data_dir`/`releases_dir`/`live_dir`/`resolve_install_path` |
| `src/metixel/backend/processing/optimisation_queue.py` | 4-phase pipeline orchestrator: classifies, thresholds, optimises, queues |
| `src/metixel/backend/processing/image.py` | Image resize + thumbnail generation + `needs_optimisation()` threshold check |
| `src/metixel/backend/processing/video.py` | `VideoProcessor` facade — `process()` + `needs_optimisation()`; delegates ffmpeg work to `probe`/`ffmpeg_cmds`/`frames` |
| `src/metixel/backend/processing/probe.py` | ffprobe wrappers, `available_ram_bytes()`, Pi-model detection |
| `src/metixel/backend/processing/ffmpeg_cmds.py` | Pure ffmpeg/ffprobe command builders (`transcode_cmd`, thumbnail/frame/probe cmds, throttle helpers) |
| `src/metixel/backend/processing/frames.py` | Thumbnail + first/last frame extraction and cache cleanup |
| `src/metixel/backend/sync/folder_watcher.py` | Phase 1 WATCH: metadata-only scanning, pushes to OptimisationQueue |
| `src/metixel/backend/sync/immich.py` | Phase 4 SYNC: Immich API client, downloads to `media/sync/immich/` |
| `src/metixel/frontend/presentation/engine.py` | Slideshow logic (platform-agnostic) — does NOT generate thumbnails, extract frames, or run ffmpeg/ffprobe |
| `src/metixel/shared/config.py` | Config schema, validation, defaults (includes `image` and `video` thresholds) |
| `src/metixel/shared/ports.py` | Clean Architecture **ports** — `typing.Protocol` interfaces (HttpGateway, MqttGateway, CecController, IrSocket, DisplayDriver) + `Ports` bundle |
| `src/metixel/shared/adapters.py` | Concrete **adapters** wrapping the real libraries (RequestsHttpGateway, PahoMqttGateway, LibCecAdapter, LircSocketAdapter) |
| `src/metixel/shared/system_stats.py` | `/proc` system stats + GPU log formatting — single home for meminfo/stat/loadavg parsers |
| `src/metixel/shared/platform.py` | Raspberry Pi detection (`is_raspberry_pi`, `detect_pi_model`) + `vcgencmd get_mem` helpers |
| `src/metixel/backend/daemon.py` | Main daemon + `build_backend()` composition-root factory |
| `src/metixel/frontend/renderer.py` | Frontend renderer + `build_renderer()` composition-root factory |
| `src/metixel/__main__.py` | Thin composition root — CLI parsing + logging, delegates to the factories |
| `etc/config.json` | Runtime configuration file |
| `scripts/quiet_boot.sh` | Silent boot configuration |
| `src/metixel/backend/web/static/js/main.js` | Web SPA entry point — wires core + page modules to the router |
| `src/metixel/backend/web/static/js/core.js` | Web SPA shared infra — API layer, router (`navigateTo`/`registerPage`), toast, DOM utils |
| `src/metixel/backend/web/static/js/*-page.js` | Web SPA — one ES module per page (dashboard/settings/network/sync/media/logs/advanced/updates) |

## If You're Unsure

- Re-read `ARCHITECTURE.md` sections relevant to the task
- Check if the change affects both Phase 1 and Phase 2
- Verify memory constraints for Pi Zero 2 W (512MB)
- Ensure the display backend abstraction isn't leaked
- **Video frame extraction is a backend responsibility.** The frontend must never import ffmpeg/ffprobe or extract frames. Frames are generated by `VideoProcessor` during Phase 2 (OPTIMISE) and referenced via `MediaItem.first_frame_path` / `MediaItem.last_frame_path`.- **Never import third-party libraries in core** — add a Protocol port + adapter and inject it (see rule 13)
- Verify your change with the full test suite on the Pi (`python3 -m pytest testing/unit_tests/`) after desktop tests
- Run `cat ARCHITECTURE.md` to re-establish project context
- **Keep the web JS modular** — native ES6 modules, no bundler; see Web UI Style Guide → JavaScript architecture before touching `static/js/`
