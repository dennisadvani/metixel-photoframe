# Metixel Testing Strategy

This document describes the layered testing strategy for Metixel and the
planned/implemented functional tests.  The goal is to catch breakage at the
**cheapest layer** possible so a broken app or experience never ships.

## Testing pyramid

```
        ┌──────────────┐
        │  Release gate │  ← OTA health-check + rollback (built-in)
        ├──────────────┤
        │  E2E (Playwright) │  ← web dashboard against live frame
        ├──────────────┤
        │  Functional (on-Pi) │  ← sudo, WiFi, AP, media, config, ...
        ├──────────────┤
        │  Unit tests   │  ← fast, CI, every PR
        └──────────────┘
```

| Layer | Where | Runs | Catches |
|---|---|---|---|
| **Unit tests** | `testing/unit_tests/` | CI, every PR | Logic regressions |
| **Functional (on-Pi)** | `testing/functional/` | On a real Pi, before release | Hardware/network/integration issues |
| **E2E (Playwright)** | `testing/web-tests/` | Workstation → live frame | Web UI regressions |
| **OTA health-check + rollback** | `scripts/update.sh` | On every upgrade | Release doesn't boot → auto-rollback |

## Functional test suites (`testing/functional/`)

These run ON a Raspberry Pi as the `pi` user against the **running** stack
(where applicable).  They are gated behind the `functional` pytest marker and
excluded from CI.  Run them with:

```bash
scripts/run_functional_tests.sh <pi-host> [<pi-user>] [--wifi-only]
```

### Implemented

| File | Verifies |
|---|---|
| `test_smoke.py` | Running backend/frontend stack: services active (no crash-loop), `/api/health` serves real data, frontend rendering |
| `test_sudo.py` | Passwordless sudo + privileged commands + hostapd/dnsmasq units present |
| `test_wifi.py` | WiFi scan/connect/forget + network state-machine transitions that drive the welcome/no-network/connected messages (ordered sequence) |
| `test_ap.py` | AP start broadcasts (hostapd active, wlan0 master, `192.168.42.1`), stop cleans up |
| `test_media.py` | Core experience: a media file dropped into a watch folder is scanned, processed, and appears in the playlist; the slideshow advances on a `next` command |
| `test_config.py` | Config save → persists to disk atomically → survives a backend restart (settings stick) |
| `test_captive_portal.py` | Captive-portal PIN validation: 4-digit requirement, wrong-PIN rejection, lockout after 3 attempts (skips when no AP/PIN active) |
| `test_immich.py` | Immich sync: server connection + API key auth, the two configured test albums exist, and a sync cycle downloads them locally (skips without `.env` credentials) |

### Planned (high value)

| Test | Layer | Priority | Status |
|---|---|---|---|
| Scheduled Immich sync | on-Pi | Medium | Planned |
| Scheduled display off | on-Pi | Medium | Planned |
| OTA upgrade + rollback | on-Pi | Medium | Planned |
| NTP time | on-Pi | Medium | Planned |
| MQTT connect/status/commands | on-Pi | Medium | Planned |
| Media library populating images/videos | on-Pi | Medium | Planned |
| Image/video optimisation + thumbnails | on-Pi | Medium | Planned |
| Graceful degradation (GPU mem, Immich down) | on-Pi | Low | Planned |

### Planned (Playwright E2E — deferred until frontend rework)

The frontend is being reworked, so Playwright tests are **held off** to avoid
rework.  These will be added after the frontend changes land:

| Test | Layer | Priority |
|---|---|---|
| Browse folder button + error handling | E2E | High |
| Dismiss welcome message in UI | E2E | High |
| Controls — pause, next, previous | E2E | High |
| Media library filtering | E2E | High |
| Media library populating images/videos | E2E | Medium |
| Immich UI flows (test connection, fetch albums, sync) | E2E | Medium |

## Release workflow

```
1. Make a change
2. Run unit tests + ruff + mypy (CI, fast)     ← catches logic bugs
3. Sync to Pi + run smoke test (~0.1s)         ← catches "did it boot?"
4. Run functional tests (~3 min)               ← catches hardware/network issues
5. Run Playwright E2E (after frontend rework)  ← catches web UI issues
6. Release (OTA health-check + rollback)       ← safety net
```