# Metixel Photoframe Internal API Documentation
#
# This document describes the internal IPC protocol and REST API for Metixel Photoframe.
# See ARCHITECTURE.md for the full system design.

## REST API (Backend → Web Dashboard)

Base URL: `http://<frame-ip>:8080/api`

### Configuration
- `GET /api/config` — Full configuration
- `GET /api/config/<section>` — Section config
- `PUT /api/config/<section>` — Update section (triggers hot reload)
- `POST /api/config/reload` — Reload from disk
- `GET /api/config/health` — System health

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
