# Changelog

All notable changes to Metixel Photoframe will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- OTA (over-the-air) self-update system via git + GitHub Releases API
- Three update channels: stable, beta, and dev
- Web UI: channel selector, available version display, check/install buttons in Advanced → Updates card
- `UpdateManager` backend: periodic update checks, git-based apply, service restart coordination
- `update` section in configuration with channel, auto-check, and interval settings
- `/api/updates/*` REST endpoints for status, check, apply, and channel switching

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
