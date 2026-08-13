# Metixel Photoframe Internal API Documentation
#
# This document describes the internal IPC protocol and REST API for Metixel Photoframe.
# See ARCHITECTURE.md for the full system design.

## REST API (Backend → Web Dashboard)

Base URL: `http://<frame-ip>/api` (port 8080 also works — Flask listens directly)

### Configuration
- `GET /api/config` — Full configuration
- `GET /api/config/<section>` — Section config
- `PUT /api/config/<section>` — Update section (triggers hot reload)
- `POST /api/config/reload` — Reload from disk
- `GET /api/config/video/profiles` — Transcoding profiles + detected model

### System (power & admin)
- `POST /api/system/restart` — Restart Metixel services
- `POST /api/system/reboot` — Reboot the system
- `POST /api/system/shutdown` — Shut down the system
- `POST /api/system/quiet-boot` — Toggle quiet boot
- `GET /api/system/info` — System/version info (Pi model, GPU memory, DRM driver)

### Time
- `GET /api/time` — Current server time
- `GET /api/time/timezones` — Timezone list
- `POST /api/time/timezone` — Set system timezone
- `POST /api/time/ntp` — Configure NTP via systemd-timesyncd

### Input
- `GET /api/input/keyboard/map` — Keyboard key map
- `POST /api/input/keyboard/learn` — Keyboard learn mode

### Control
- `POST /api/control` — Realtime frontend control (IPC: next/prev/pause/resume/screen_off/…)

### Health & Diagnostics
- `GET /api/health` — System health
- `GET /api/health/display/info` — Detected display resolution
- `GET /api/health/processing` — Background processing status
- `GET /api/health/processing-status` — Per-phase processing progress

### Filesystem
- `GET /api/browse?path=...` — Browse folders for path selection

### Media
- `GET /api/media/list` — List media items
- `POST /api/media/upload` — Upload file
- `DELETE /api/media/<id>` — Delete item

### Logs
- `GET /api/logs/recent` — Recent log entries

### Widgets
- `GET /api/widgets` — Widget configs
- `PUT /api/widgets/<type>` — Update widget config

## IPC Protocol (Backend → Frontend)

Transport: Unix Domain Socket (`/run/metixel/control.sock`)
Format: JSON (one message per datagram)

### Commands
```json
{"cmd": "next"}
{"cmd": "prev"}
{"cmd": "pause"}
{"cmd": "resume"}
{"cmd": "power_off"}
{"cmd": "power_on"}
{"cmd": "switch_album", "args": {"album_id": "abc123"}}
```

## MQTT Topics (Home Assistant)

Publish:
- `metixel/status` — "online" / "offline"
- `metixel/health` — JSON health metrics
- `metixel/current_media` — Current item info

Subscribe:
- `metixel/cmd` — Control commands
- `metixel/album/set` — Switch album
