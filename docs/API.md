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

The MQTT client publishes state under `<prefix>` (default `metixel`) and, when
`mqtt.discovery_enabled` is true (default), exposes a **Home Assistant MQTT
Discovery** device (`Metixel Photo Frame`) with buttons, a screen-power switch,
and sensors.

**Multiple frames:** each frame's HA device identity is scoped by
`mqtt.device_id` (default: hostname) — device identifiers, entity `unique_id`s,
and discovery object IDs are all `metixel_<device_id>_…`. Give each frame a
different `device_id` (and a different `topic_prefix` when they share a broker)
so HA treats them as separate devices with no entity collisions.

Publish:
- `metixel/status` — "online" / "offline" (retained; used as HA availability)
- `metixel/health` — JSON health metrics
- `metixel/current_media` — JSON with `title`, `media_type`, `paused`, `state`
- `metixel/state` — "playing" / "paused" / "off"
- `metixel/screen` — "ON" / "OFF" (screen power)

Subscribe:
- `metixel/cmd` — Control commands: `next`, `prev`, `pause`, `resume`,
  `toggle_pause`, `power_on`, `power_off`
- `metixel/album/set` — Switch album (album id payload)
- `metixel/screen/set` — "ON" / "OFF" to toggle screen power

### Home Assistant MQTT Discovery

Discovery configs publish to `homeassistant/<component>/metixel_<entity>/config`
(retained) on connect and re-publish every 30 minutes. Entities:

| Component | Entity | Purpose |
|---|---|---|
| `button` | next / prev / pause_toggle | Publish the matching command to `metixel/cmd` |
| `switch` | screen_power | ON/OFF screen power (state on `metixel/screen`) |
| `sensor` | current_media | Current media title (diagnostic; disabled by default) |
| `sensor` | playback_state | playing/paused/off (diagnostic) |
| `sensor` | uptime | Human-readable uptime, e.g. `2d 3h 45m` (diagnostic) |
| `sensor` | cpu_temperature | CPU temperature (°C) (diagnostic) |
| `sensor` | memory_used | Used memory (%) (diagnostic) |
| `sensor` | swap_used | Used swap (%) (diagnostic) |
| `sensor` | disk_used | Root filesystem used (%) (diagnostic) |

All sensors are registered as HA **diagnostic** entities. `current_media`
(the raw file name) is additionally `enabled_by_default: false` — enable it
in HA if you want to see what is currently playing.
Availability for every entity is `metixel/status` (`online`/`offline`).
