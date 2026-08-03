# Changelog

All notable changes to Metixel Photoframe will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.1-beta.1]

### Added

- `first_frame_path` and `last_frame_path` fields on `MediaItem` — backend-generated
  video frame caches referenced directly by the presentation engine
- `VideoProcessor._extract_video_frames()` — extracts first frame (t=0) and last
  frame (sseof) during Phase 2 OPTIMISE; cached as `.1.frame` / `.2.frame` JPEGs
- **Image optimisation worker subprocess** (`worker.py`) — PIL operations (load,
  transpose, resize, save) run in an isolated process with `cpulimit -l 50` (hard
  50 % CPU cap) and `nice -n 19` (lowest priority); OS reclaims all memory on exit
- **Adaptive CPU throttling** — sleep between image optimisations scales with
  1‑minute load average (`load1 × 0.2`, capped at 1.0 s); folder watcher also
  yields between thumbnail generations when the optimiser is busy
- **Boot screen progress bar** — red fill bar below the spinner showing
  optimisation progress (1/6 → 6/6); uses `draw_image` with 1×1 pixel textures
  to avoid pi3d colour-space issues
- **Per‑phase progress bars** in the Web UI Background Processing card — separate
  persistent bars for scanning, image optimisation, and video transcoding;
  each retains its last position when the active phase switches
- **Frontend→backend slideshow‑started signal** — `POST /api/slideshow-started`
  defers the network monitor's AP‑fallback countdown until the first slideshow
  items are ready, preventing CPU/I/O contention during initial processing
- `/api/config/processing-status` endpoint — serves per‑phase progress data
- `FEATURES.md` — comprehensive feature list linked from README
- Per‑image processing time and per‑batch memory delta logging (DEBUG level)
- **Raspberry Pi 5 (2GB+) official Phase 1 support** — runs the same Trixie
  image via cage + XWayland; now the recommended platform over Pi 3
- **Immich network connectivity gate** — syncer checks `is_connected()` before
  attempting API calls, avoiding wasted time on doomed requests when offline
- **Network Controller** (`network_controller.py`) — single-owner state machine
  for WiFi/AP management; eliminates race conditions between network monitor
  thread and Flask request threads via ``threading.Lock``
- **WiFi AP grace period** — 5‑minute silent retry of saved WiFi before
  activating the captive portal; handles transient router reboots without
  user intervention
- **WiFi AP exhaustion** — AP auto‑stops after 10 minutes if no user connects;
  never reactivates until next reboot, preventing continuous AP cycling on
  permanent WiFi loss
- **WiFi AP crash detection** — controller detects when hostapd dies
  unexpectedly and transitions to exhausted state rather than lying about
  the AP being active
- **WiFi regulatory domain** — configurable country code in setup script and
  Web UI; persists via ``cfg80211.ieee80211_regdom`` module parameter
- **WiFi power management disabled** — setup script writes NetworkManager
  config (``wifi.powersave = 2``), fixing Pi 3 WiFi unreliability
- **Pipeline reset on config change** — changes to video, image, or display
  settings clear all queues and re‑scan; boot layer reactivates with spinner
  during rebuild instead of showing a black screen
- **Boot layer reactivation** — ``reactivate()`` method re‑shows logo + spinner
  on pipeline reset; reset mode skips 3 s minimum display and progress bar
- **Documentation overhaul** — new ``GETTING_STARTED.md``, ``WIFI_SETUP.md``,
  ``INSTALLATION.md``; README restructured with logo, showcase images, and
  two‑path quick‑start section
- **Contributing link** — README Contributing section links to dev setup guide
  in ``INSTALLATION.md#path-c-development-setup-remote-via-samba``
- **Cumulative progress tracking** — image and video optimisation progress bars
  now show total backlog across all batches instead of resetting every 6 items
- **Folder watcher scanning progress** — incremental scans (new files added to
  watch folders) now report progress to the Web UI bar, not just the initial scan

### Changed

- **Image processing moved to subprocess** — `ImageProcessor.process()` spawns
  `worker.py` for cache misses; cache hits remain in‑process (fast path)
- **Throttle formula 4× more aggressive** — `load1 × 0.05` → `load1 × 0.2`
- **Batch sizes reduced** — optimiser flush 12→6, folder watcher enqueue 24→6;
  items reach the slideshow ~4× faster after startup
- **Boot layer** fades based on `slideshow_ready` signal from renderer (not
  playlist.json on disk) and waits for active GPU texture before signalling
  readiness — eliminates fade‑to‑black and jump‑cut regressions
- `_write_progress` uses per‑phase format — scanning, optimising, and transcoding
  each track their own `total`/`processed` independently
- **Web UI** Background Processing card replaced with three persistent progress
  bars using Material Symbols icons
- `is_ready_to_play` now requires `first_frame_path` and `last_frame_path` for
  video items — videos without cached frames are excluded from the slideshow
- Dev setup script: removed `python3-pip` and `python3-pygame` (dead deps)
- **Pi 5 moved from Phase 2 to Phase 1** — `ARCHITECTURE.md`, `CLAUDE.md`,
  `README.md`, and `HARDWARE.md` updated; Pi 5 (2GB+) now the recommended platform
- **Boot progress bar auto‑hides** when slideshow queue reaches 6 items,
  preventing repeated 100 % cycling on cached restarts
- **Release scripts** (`release.ps1`, `release.sh`): merge strategy changed
  from `--ff-only` to `--no-ff` for cleaner release history
- **Web UI** scanning label updated to "Scanning folders and generating thumbnails"
- **WiFi AP state machine** — daemon's ``_network_monitor_loop`` now delegates
  to ``NetworkController`` with phase‑based transitions (``MONITORING``,
  ``GRACE_PERIOD``, ``AP_ACTIVE``, ``AP_EXHAUSTED``)
- **Video transcode CPU limit default** — increased from 50 % to 300 %
  (3 full cores on Pi 5/3)
- **VLC playback messages** — changed from ``INFO`` to ``DEBUG`` log level
- **Immich test connection** — saves server URL and API key on successful test
  so Fetch Albums works immediately without a separate save step
- **Quiet boot hint** — removed "Requires a reboot to take effect" from Web UI

### Fixed

- **AP fallback falsely activated under CPU load** — `is_connected()` now returns
  `True` on exception (nmcli timeout) instead of `False`
- **Worker JSON corrupted by cpulimit** — cpulimit prints `Process NNNN detected`
  to stdout; parser now takes the first JSON line only
- **Duplicate throttle line** — old `×0.05` formula overwrote new `×0.2` formula
- **Cache‑hit `AttributeError`** — removed `_build_item` call, inlined
  `MediaItem` construction with `_get_cached_dimensions()`
- **Boot screen jump cut** — `slideshow_ready` now requires non‑None active GPU
  texture before signalling, preventing fade timer expiry during sync texture load
- **Slideshow‑started sent on empty queue** — notification only fires when items
  are present; hot‑reload path also signals if initial load was empty
- **Progress bar jumping backward** — uses batch size (6) as total instead of
  shrinking `total_remaining`
- **Progress bar disappearing** — retains last‑known percentage when phase
  switches away from `optimising_images`
- **Boot layer duplicate docstring** — removed dead duplicate of
  `_read_progress_pct` that shadowed the real implementation
- **WiFi captive portal not appearing on first boot** — ``start_ap_mode()``
  return value now checked; ``ap_was_active`` only set on success; wlan0
  readiness polled (up to 30 s) before starting AP; hostapd no longer
  auto‑starts at boot
- **AP fallback retry deadlock** — retry guard now checks ``is_ap_mode_active()``
  as source of truth instead of a stale ``ap_was_active`` boolean
- **PIN validation race** — PIN validated against cleared state (passed any
  input); now owned by ``NetworkController`` with ``threading.Lock``; never
  passes when no PIN is active
- **Transparent PNG rendering** — RGBA/PA images composited onto black
  background before RGB conversion; fixes white artifacts in transparent areas
  for both thumbnails and optimised images
- **Display power button state** — Web UI button now reads actual display state
  from daemon via health endpoint instead of using a local toggle guess
- **Media library CPU spike** — removed on‑the‑fly thumbnail generation from
  ``/api/media/list``; relies on FolderWatcher‑generated thumbnails only
- **Log viewer follow mode** — auto‑follow now jumps to last page when new
  entries arrive; scroll target corrected to ``#log-viewer`` container
- **On‑screen message spacing** — Y‑position now accumulated from actual
  heights of preceding messages instead of ``index × current_height``,
  eliminating gaps and overlaps between differently‑sized popups

### Removed

- All ffmpeg/ffprobe calls from `PresentationEngine` (`engine.py`):
  `_extract_frame_array_cpu`, `_get_or_create_video_frame`,
  `_video_frame_cache_path`, `_video_frame_is_cached`
- `contextlib` import from frontend (no longer needed)
- `_count_playlist_items()`, `_PLAYLIST_PATH`, and `json` import from boot layer
- `_resize_to_screen`, `_extract_exif`, `_build_item` from `ImageProcessor`
- `ImageOps`, `UnidentifiedImageError` imports from `image.py`

## [0.2.2-beta.3] — 2026-08-01

### Added

- Version bump script (`scripts/bump_version.py`) with full semver + pre-release support
- Release documentation (`docs/RELEASING.md`) covering versioning, channels, and workflow

### Fixed

- Channel switch hanging on "Checking for updates…" — added polling loop so the
  UI refreshes when the background check completes instead of showing the spinner
  indefinitely

### Changed

- Setup script (`setup_trixie_metixel.sh`): standalone execution support,
  auto-detects git repository state, improved user feedback during setup
- README revised for clarity and setup script details

## [0.2.0-beta.1] — 2026-08-01

### Added

- **OTA self-update system** via git + GitHub Releases API
  - Three update channels: stable, beta, and dev
  - `UpdateManager` backend: periodic checks, git-based apply, service restart coordination
  - Web UI: channel selector, available version display, check/install buttons (Advanced → Updates)
  - `/api/updates/*` REST endpoints for status, check, apply, and channel switching
  - `update` section in configuration with channel, auto-check, and interval settings
- **WiFi Captive Portal** with PIN protection for headless network setup
  - `NetworkManager` handles AP fallback when no WiFi is configured
  - Captive portal web page served on port 8080 during AP mode
- **System Messages overlay** — popup notification system rendered on the display
  - Configurable default duration, max visible count, and persistent messages
  - Message layer pauses during video playback to avoid distraction
- **Boot screen** — custom splash screen with spinner (replaces pygame dependency)
  - `BootLayer` renders on startup until the first media item is ready
- **System status monitoring** in the Web UI dashboard
  - CPU utilisation, memory usage, disk space, and uptime
  - Scheduled sync status and next-sync countdown
- **NTP time synchronisation** with configurable NTP servers
- **Display sleep schedule** — configurable on/off times for screen power saving
- **Reboot and shutdown buttons** in the Web UI (Advanced → System)
- **CPU throttling for image optimisation** — `cpulimit`/`nice` integration prevents
  background processing from starving the render loop
- **Transcode status tags** visible in the media list view
- **New media pipeline v2** — improved queue management, better parallelism,
  and per-item state tracking through the watch→optimise→queue→sync phases
- Updated sample media set with new royalty-free images

### Changed

- **Web UI redesigned** — cleaner layout, dark theme, responsive cards
- **Removed pygame** from the dependency tree entirely (replaced by `BootLayer`)
- **Removed legacy Pi 3 boot messages** for cleaner startup on Trixie
- Slide transition timer now pauses during video playback
- Image optimisation defaults tuned for Pi Zero 2 W memory constraints
- Config schema: added `image`, `video`, `messages`, `network`, `system`, and `update` sections

### Fixed

- Clean install failures — missing cache/log directories now created automatically
- GitHub repository URL references throughout the codebase and NOTICE file
- System Status values reporting incorrect memory/disk figures
- Boot screen timing race with AP fallback on first boot
- Network connectivity check looping when DNS was unreachable
- Video setting changes not propagating to the active playback queue
- Hot-reload playlist corruption after rapid config changes
- Thumbnail generation failing for certain EXIF orientations
- Race conditions in the media pipeline when watcher and optimiser overlapped
- Parallel transcode processes exhausting CPU and memory on Pi Zero 2 W
- Video processing OOM (out of memory) during large-file optimisation
- GPU texture count warnings from unreleased textures on memory-constrained devices
- Lint errors and crash when no display monitor was attached

## [0.1.1] — 2025-07-30

### Initial Release

- Phase 1 hardware support: Raspberry Pi 2, 3, Zero 2 W
- Display backend abstraction (pi3d under cage + XWayland on Trixie)
- 4-phase media pipeline: watch → optimise → queue → sync
- Web dashboard (Flask + vanilla JS SPA) on port 8080
- Immich integration for photo sync
- Local folder watching with configurable paths
- Slideshow engine with crossfade transitions
- Video playback with ffmpeg transcoding
- MQTT client for Home Assistant
- HDMI-CEC input handling
- Systemd service management
- Network manager with AP fallback (captive portal)
- Image optimisation (resize, matte, EXIF rotation)
- Thumbnail generation by backend processors
