# Changelog

All notable changes to Metixel Photoframe will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `first_frame_path` and `last_frame_path` fields on `MediaItem` — backend-generated
  video frame caches referenced directly by the presentation engine
- `VideoProcessor._extract_video_frames()` — extracts first frame (t=0) and last
  frame (sseof) during Phase 2 OPTIMISE; cached as `.1.frame` / `.2.frame` JPEGs
- **Time card** in the Web UI Advanced page — separate NTP/timezone settings
  (extracted from the System card)

### Changed

- **Frame extraction moved from frontend to backend** — `PresentationEngine` no
  longer runs ffmpeg/ffprobe; all video frame extraction is now a Phase 2
  (OPTIMISE) responsibility performed by `VideoProcessor`
- **All videos route through the optimisation queue** — the folder watcher no
  longer bypasses the queue for PLAY_ORIGINAL videos; `VideoProcessor.process()`
  skips transcode for H.264 within resolution limits but always extracts frames
- **Last frame swap at 20% of video playtime** (was ~1 s before end) — eliminates
  the race condition where VLC exits before/during synchronous ffmpeg extraction
- **Web UI**: System card moved to top of Advanced page; NTP/time settings
  extracted into their own Time card with independent save button
- `is_ready_to_play` now requires `first_frame_path` and `last_frame_path` for
  video items — videos without cached frames are excluded from the slideshow

### Fixed

- **Black screen after cache clear** — frontend playlist hot-reload no longer
  removes items when the backend playlist is smaller than the frontend queue
  (backend is still building its initial playlist)
- **Video playback with missing frames** — videos no longer appear in the
  playlist before first/last frame extraction completes
- **Missing `first_frame_path` / `last_frame_path` in playlist hot-reload**
  deserialization path

### Removed

- All ffmpeg/ffprobe calls from `PresentationEngine` (`engine.py`):
  `_extract_frame_array_cpu`, `_get_or_create_video_frame`,
  `_video_frame_cache_path`, `_video_frame_is_cached`
- `contextlib` import from frontend (no longer needed)

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
