# Metixel Photoframe Internal API Documentation
#
# This document describes the internal IPC protocol and REST API for Metixel Photoframe.
# See ARCHITECTURE.md for the full system design.

## REST API (Backend → Web Dashboard)

Base URL: `http://<frame-ip>/api` (port 8080 also works — Flask listens directly)

### Authentication (optional web password)

When a web password is set (`web.password` in config), all `/api/*` routes
except the exempt set require a valid session cookie.  Auth uses a Flask
signed session cookie (`HttpOnly`, `SameSite=Lax`).  There is no TLS on the
LAN by default — the password is the access boundary.

- `POST /api/auth/login` — Authenticate.  Body: `{"password": "..."}`.
  Sets the session cookie on success.  Returns `{"authenticated": true}`.
  Rate-limited (5 attempts, then a 5-minute cooldown).
- `POST /api/auth/logout` — Clear the session cookie.
- `GET /api/auth/me` — Auth status: `{enabled, authenticated,
  session_timeout_minutes, version}`.  Used by the SPA boot gate.
- `POST /api/auth/password` — Set/change/clear the web password (requires an
  authenticated session).  Body: `{"password": "..."}` (empty clears it).
- `POST /api/auth/screen-pin` — Set/change/clear the optional on-screen UI
  PIN (requires an authenticated session).  Body: `{"pin": "...",
  "confirm": "..."}` or `{"clear": true}`.  PINs are 4-6 digits.
- `GET /api/auth/screen-pin/status` — `{enabled, timeout_minutes}`.

**Exempt from auth** (reachable without a session):
- `GET /api/health` — OTA update.sh health-check + monitoring
- `POST /api/auth/login|logout`, `GET /api/auth/me` — the gate itself
- `POST /api/slideshow-started` — frontend renderer loopback signal
- `/api/network/status|scan|validate-pin|connect` — the captive-portal
  Wi-Fi setup calls, exempt **only while the setup hotspot / PIN gate is
  active**.  Every other `/api/network/*` route always requires a session.
- `POST /api/control` — exempt **only for loopback callers** (trusted local
  processes on the Pi); the dashboard reaches it through its normal session.

**Same-origin (CSRF) check:** every state-changing `/api/*` request (`POST`,
`PUT`, `PATCH`, `DELETE`) must carry an `Origin` (or, failing that, `Referer`)
header naming this host, otherwise it is rejected with `403`.  Requests with
neither header (curl, tests, the frontend's loopback signal) are allowed.

### Configuration
- `GET /api/config` — Full configuration
- `GET /api/config/<section>` — Section config
- `PUT /api/config/<section>` — Update section (triggers hot reload)
- `POST /api/config/reload` — Reload from disk
- `GET /api/config/video/profiles` — Transcoding profiles + detected model
- `GET /api/config/path` — Config file path + whether it exists (debugging)
- `POST /api/config/network/apply-wifi-country` — Push a Wi-Fi regulatory
  country code to the radio immediately (`iw reg set`).  Body:
  `{"country": "AU"}`.  The persisted setting is saved separately via
  `PUT /api/config/network`.

### System (power & admin)
- `POST /api/system/restart` — Restart Metixel services
- `POST /api/system/reboot` — Reboot the system
- `POST /api/system/shutdown` — Shut down the system
- `POST /api/system/quiet-boot` — Toggle quiet boot
- `GET /api/system/info` — System/version info (Pi model, GPU memory, DRM driver)
- `GET /api/system/mqtt-status` — MQTT broker connection state
  (`disabled` | `connected` | `auth_error` | `connecting` | `not_responding`), plus
  `broker`/`port` and the rejection `error` (e.g. `Not authorized`) when applicable.
- `POST /api/system/device-password` — Change the synced device password
  (SSH console + Samba share).  Body: `{"new_password": "...",
  "confirm_password": "..."}`.  Runs `sudo -n chpasswd` then
  `sudo -n smbpasswd -a -s pi`.  Requires an authenticated session (no
  "current password" field — sudo is NOPASSWD).  Returns `{"status": "ok"}`
  on success, or `{"status": "partial", "console": "ok", "samba": "failed"}`
  if the console password changed but Samba failed (stores out of sync).

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
  - **Default (no query)** — always `200` while the backend can serve the
    request.  This is the dashboard/monitoring contract; the SPA treats a
    non-2xx as a hard failure, and a deliberately headless device must not
    look permanently broken.
  - **`?require=render`** (aliases: `any`, `all`) — the strict form used by
    the OTA updater's gate.  Returns `503` unless the frontend is
    demonstrably alive, so a release whose frontend crash-loops **fails** the
    gate instead of being declared a success.
  - The response always carries:
    - `liveness.frontend` — `{alive, state, reason, age_seconds, pid, boot_id}`
      where `state` is `alive` | `starting` | `churning` | `stale` | `missing`
      | `unknown`.  `alive` is `null` when tracking is unavailable.
    - `healthy` / `status` — the overall verdict for the *requested* checks.
    - `required` — the checks this request enforced (`[]` by default).
    - `unhealthy_checks` — present only on a `503`.
  - Liveness comes from the frontend's heartbeat
    (`/run/metixel/frontend_heartbeat.json`, rewritten every ~10 s).  Because
    `metixel-cage.service` is `Restart=always`, a **fresh file is not
    enough**: liveness means *the same `pid`/`boot_id` has been beating*, so a
    crash loop is reported as `churning` rather than healthy.
  - `POST /api/slideshow-started` remains a separate, one-shot signal and is
    **not** a liveness source — a device with no media never sends it.
- `GET /api/health/display/info` — Detected display resolution
- `GET /api/health/display/modes` — Display modes the monitor and Pi mutually
  support (populates the resolution dropdown when auto-detect is off)
- `GET /api/health/processing` — Background processing status
- `GET /api/health/processing-status` — Per-phase processing progress
  (`scanning` / `optimising_images` / `inspecting_videos` / `transcoding`),
  plus `issues` (failed/skipped media from the processing journal, with
  `path`, `name`, `state`, `reason`, `updated_at`) and `journal_stats`
  (per-state counts).

### Network
- `GET /api/network/status` — Connection status + `wifi_radio_enabled` /
  `has_saved_wifi` / `ap_mode_active`.
- `GET /api/network/scan` — Visible Wi-Fi networks (cached while the AP is up).
- `POST /api/network/connect` — Connect to a Wi-Fi network.  Body:
  `{"ssid": "...", "password": "..."}` (empty password for open networks).
- `POST /api/network/forget` — Forget a saved network.  Body: `{"ssid": "..."}`.
- `POST /api/network/radio` — Enable or disable the Wi-Fi radio at the OS
  level.  Body: `{"enabled": true|false}`.
  - Toggling **off** returns `409` with `reason: "ap_active"` while the setup
    hotspot is running (turning the radio off would strand a user mid-setup).
    Otherwise the response is flushed before the radio drops, since disabling
    can kill the caller's own connection.
  - Toggling **on** is synchronous; `500` on failure.
  - This is the only supported way to change the radio.  The radio is
    user-owned state: the backend enables it once on a device's first boot
    (latched by `network.wifi_radio_first_run_done`) and never re-asserts it on
    boot or on an OTA.  `scripts/reconcile.sh` intentionally does not manage it.
- `GET /api/network/ap-status` — `{active}`: whether the setup hotspot / PIN
  gate is currently up.
- `POST /api/network/validate-pin` — Validate the AP PIN shown on the frame.
  Body: `{"pin": "1234"}`.  After 3 wrong attempts the PIN locks for 10 minutes.
- `POST /api/network/ap-start` / `POST /api/network/ap-stop` — Manually
  start/stop the access point (debugging only — the NetworkController
  manages the AP lifecycle automatically).

### Updates
- `GET /api/updates/status` — Version, channel, available releases, schedule
  and progress.  Includes **`auto_update_hurdle`**:
  ```json
  {"applies": true, "hardware_ok": false, "model": "pi3",
   "min_safe_version": "2.0.0", "candidate": "2.0.0", "reason": "…"}
  ```
  `applies` is `true` when a pending release is being withheld from the
  **automatic** path because this board is below the 2.0.0 hardware floor
  (a Pi 2/3/Zero 2 W, or an undetectable model).  It is informational only —
  `POST /api/updates/apply` is deliberately **not** gated, so the manual
  Install button keeps working.
- `POST /api/updates/check` — Trigger a check.  `?force=true` bypasses the cache.
- `POST /api/updates/apply` — Install a release (latest on channel, or
  `{"version": "..."}`; `keep_existing` reuses a local copy).  Not affected by
  `auto_update_hurdle`.
- `POST /api/updates/rollback` — Flip the `live` symlink to a locally
  installed release.
- `POST /api/updates/apt-upgrade` — Full `apt update && apt upgrade` + reboot
  (runs detached; returns `{"status": "ok"}` immediately).
- `GET /api/updates/releases` — Cached list of GitHub releases installable
  manually (atomic-era, >= 1.2.3); triggers a background check if not cached.
- `PUT /api/updates/auto-update` — Configure the weekly auto-update schedule.
  Body (any subset): `{"enabled": bool, "day": 0-6, "time": "HH:MM"}`.
- `PUT /api/updates/channel` — Switch the update channel.  Body:
  `{"channel": "stable|beta|dev"}`; triggers a check on the new channel.

### Processing
- `POST /api/processing/retry` — Forget a failed/skipped journal entry so
  the next folder scan re-processes it.  Body: `{"path": "<resolved path>"}`
  (the `issues[].path` from `/api/health/processing-status`).
- `POST /api/processing/delete` — Delete a failed/skipped media file and
  drop its journal entry (and any matching playlist item).  Body:
  `{"path": "<resolved path>"}`.  Only files inside a configured watch
  folder can be deleted; returns `{"status": "ok", "deleted": bool}`.

### Filesystem
- `GET /api/browse?path=...` — Browse folders for path selection.  Returns
  `{current_path, parent_path, entries, base_path, can_create}` where
  `entries` are subdirectory objects `{name, path}` and `can_create` is true
  only inside the media tree.
- `POST /api/browse/create` — Create a new folder inside the currently
  browsed directory.  Body: `{"path": "<parent dir>", "name": "<folder name>"}`.
  Names must be plain (no path separators, `..`, or hidden-dot prefix).
  Restricted to the media tree (`<data dir>/media`).

### Media
- `GET /api/media/list` — List media items
- `POST /api/media/upload` — Upload media files (see below)
- `GET /api/media/thumbnail/<filename>` — Serve a cached thumbnail or video
  frame image (downscaled to 320 px max)
- `POST /api/media/cache/clear` — Delete all processed caches (images,
  thumbnails, videos), clear the playlist and reset the frontend queue.
  Returns `{status, deleted_files, freed_bytes}`.

There is no generic media delete route; failed/skipped files can be removed
via `POST /api/processing/delete` (see Processing).

#### Uploading media (`POST /api/media/upload`)

Accepts `multipart/form-data` with one or more files under the **`files`**
field name.  Files are streamed to the configured upload destination —
`system.upload_dir` (relative paths resolve under the persistent data dir),
falling back to `media/my_media/` (an enabled watch path) when unset — so
the folder watcher picks them up and they flow into the slideshow.

Behaviour:
- **Extension whitelist** — only image/video formats are accepted
  (`.jpg`, `.jpeg`, `.png`, `.bmp`, `.gif`, `.webp`, `.mp4`, `.mov`, `.avi`,
  `.mkv`, `.webm`, `.m4v`, `.mpg`, `.mpeg`).  Anything else is rejected.
- **HEIC/HEIF** — iPhone photos are converted to JPEG (quality 90, EXIF
  orientation preserved) on arrival, because the media pipeline only handles
  the classic formats.
- **Auto-rename** — on a filename collision the file is saved as
  `name-1.ext`, `name-2.ext`, … (never overwrites).
- **Free-space guard** — an upload is refused if it would leave less than 5%
  of the filesystem free.
- **Filename sanitisation** — path components and unsafe characters are
  stripped.

Response: `{saved: [{name, saved_as, size}], errors: [{name, error}],
saved_count, error_count}` with HTTP 201 when anything was saved, else 400.

### Logs
- `GET /api/logs/recent` — Recent log entries
- `POST /api/logs/level` — Change the file-handler log level at runtime
  (persisted to config).  Body: `{"level": "DEBUG|INFO|WARNING|ERROR|NONE"}`.

### DDC/CI monitor control
- `GET /api/ddc/status` — DDC enablement, availability and detected monitors
- `GET /api/ddc/capabilities` — User-facing VCP features for the
  configured/selected display (`?display=N`)
- `GET /api/ddc/vcp/<code>` — Read a single VCP feature
- `PUT /api/ddc/vcp/<code>` — Write a single VCP feature.  Body: `{"value": N}`
- `POST /api/ddc/refresh` — Invalidate caches and re-probe the monitor
- `POST /api/ddc/reset` — Restore the monitor to factory defaults (VCP 0x04)

### Immich
- `GET /api/immich/albums` — List albums from the configured server
  (`{id, name, assetCount}`)
- `POST /api/immich/albums/add` — Add an album to the sync group.  Body:
  `{"id": "...", "name": "..."}`
- `POST /api/immich/albums/remove` — Remove an album from the sync group and
  delete its local folder
- `POST /api/immich/sync` — Trigger a manual sync cycle (runs in the
  background; poll `GET /api/immich/status`)
- `GET /api/immich/status` — Most recent sync result plus live progress
  (`null` if a sync has never run)
- `POST /api/immich/cancel` — Cancel the running sync (finishes the current
  download, then aborts)
- `POST /api/immich/test-connection` — Probe a server URL + API key.  Body:
  `{"server_url": "...", "api_key": "..."}`.  Always returns HTTP `200`
  once the body is valid — the outcome is in the body: `{"ok": true, ...}`
  or `{"ok": false, "status": <upstream HTTP code | "connection_error" |
  "timeout" | "error">, "error": "..."}`.

### Messages
- `GET /api/messages/persistent` — Current list of persistent on-screen messages
- `POST /api/messages/dismiss` — Dismiss one (`{"id": "..."}`) or all
  (`{"all": true}`) persistent messages; removed from config and cleared
  from the screen via IPC

## IPC Protocol (Backend → Frontend)

Transport: Unix Domain Socket (`/run/metixel/control.sock`)
Format: JSON (one message per datagram)

### Commands
```json
{"cmd": "next"}
{"cmd": "prev"}
{"cmd": "pause"}
{"cmd": "resume"}
{"cmd": "screen_off"}
{"cmd": "screen_on"}
{"cmd": "switch_album", "args": {"album_id": "abc123"}}
```

## MQTT Topics (Home Assistant)

The MQTT client publishes state under `<prefix>` (default `metixel/<device_id>`,
scoped by the frame's unique id) and, when `mqtt.discovery_enabled` is true
(default), exposes a **Home Assistant MQTT Discovery** device (`Metixel Photo
Frame`) with buttons, a screen-power switch, and sensors.

**Multiple frames:** every frame's topics and HA device identity are scoped
by `mqtt.device_id` — device identifiers, entity `unique_id`s, and discovery
object IDs are all `metixel_<device_id>_…`, and the raw topics are
`metixel/<device_id>/…`. Leave `device_id` empty (default) to auto-derive it
from the hardware (Pi serial → MAC → machine-id → hostname), which is unique
per physical board — so multiple frames on one broker are fully isolated with
**no configuration required**.

Publish (`<prefix>` = `metixel/<device_id>`):
- `<prefix>/status` — "online" / "offline" (retained; used as HA availability)
- `<prefix>/health` — JSON health metrics
- `<prefix>/current_media` — JSON with `title`, `media_type`, `paused`, `state`
- `<prefix>/state` — "playing" / "paused" / "off"
- `<prefix>/screen` — "ON" / "OFF" (screen power)

Subscribe:
- `<prefix>/cmd` — Control commands: `next`, `prev`, `pause`, `resume`,
  `toggle_pause`, `power_on`, `power_off`
- `<prefix>/album/set` — Switch album (album id payload)
- `<prefix>/screen/set` — "ON" / "OFF" to toggle screen power

### Home Assistant MQTT Discovery

Discovery configs publish to `homeassistant/<component>/metixel_<entity>/config`
(retained) on connect and re-publish every 30 minutes. Entities:

| Component | Entity | Purpose |
|---|---|---|
| `button` | next / prev / pause_toggle | Publish the matching command to `<prefix>/cmd` |
| `switch` | screen_power | ON/OFF screen power (state on `<prefix>/screen`) |
| `sensor` | current_media | Current media title (diagnostic; disabled by default) |
| `sensor` | playback_state | playing/paused/off (diagnostic) |
| `sensor` | uptime | Human-readable uptime, e.g. `2d 3h 45m` (diagnostic) |
| `sensor` | cpu_temperature | CPU temperature (°C) (diagnostic) |
| `sensor` | cpu_usage | CPU utilisation (%) (diagnostic) |
| `sensor` | memory_used | Used memory (%) (diagnostic) |
| `sensor` | swap_used | Used swap (%) (diagnostic) |
| `sensor` | disk_used | Root filesystem used (%) (diagnostic) |

All sensors are registered as HA **diagnostic** entities. `current_media`
(the raw file name) is additionally `enabled_by_default: false` — enable it
in HA if you want to see what is currently playing.
Availability for every entity is `<prefix>/status` (`online`/`offline`).
