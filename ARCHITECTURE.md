# Metixel Photoframe — Architecture & Implementation Plan

---

## Table of Contents

1. [Tech Stack Recommendation](#1-tech-stack-recommendation)
2. [System Architecture Diagram](#2-system-architecture-diagram)
3. [File & Directory Structure](#3-file--directory-structure)
4. [State Management & Hot Reload Strategy](#4-state-management--hot-reload-strategy)
5. [Phase-by-Phase Implementation Roadmap](#5-phase-by-phase-implementation-roadmap)
6. [Detailed Subsystem Design](#6-detailed-subsystem-design)

---

## 1. Tech Stack Recommendation

### 1.1 The Graphics Pipeline

All Pi models use the same `Pi3dBackend`. The underlying driver is determined by the OS:

| Concern | Trixie + KMS (Pi 2/3/Zero 2 W) | Pi 4/5 + KMS |
|---|---|---|
| **GPU** | VideoCore IV | VideoCore VI / VII |
| **Kernel Driver** | Mainline Mesa + vc4 KMS/DRM | Mainline Mesa + v3d/vc4 KMS/DRM |
| **Display API** | KMS/DRM via Wayland compositor (cage) | KMS/DRM via Wayland compositor (cage) |
| **X11 Surface** | XWayland shim — pi3d gets an X11 window | XWayland shim — pi3d gets an X11 window |
| **RAM Overhead** | ~120MB (app + cage + XWayland) | ~120MB (app + cage + XWayland) |
| **OpenGL** | GLES 2.0 via Mesa | GLES 2.0/3.0 via Mesa |

**Critical constraint**: Raspberry Pi OS **Bullseye** (Debian 11) was the last release supporting ARMv6 (Pi 1, original Pi Zero). Metixel now targets **Trixie** (Debian 13) as the baseline — Pi Zero 2 W and Pi 2+ are the minimum.

### 1.2 Application-Layer Abstraction

The application does NOT import pi3d directly. It imports from a display backend interface:

```
src/metixel/
├── display/
│   ├── __init__.py          # Factory: auto-detect hardware, return correct backend
│   ├── backend.py           # Abstract base class: DisplayBackend
│   ├── hardware.py          # GpuInfo / WlrOutput / DisplayPower adapters (extracted from dispmanx_backend)
│   ├── dispmanx_backend.py  # Phase 1: wraps pi3d, uses Mesa EGL via cage/XWayland
│   ├── wayland_backend.py   # Phase 2: PyOpenGL + EGL on Wayland/DRM (future)
│   └── tk_backend.py        # Desktop dev: tkinter-based software renderer
```

### 1.3 Per-Model OS & Graphics Strategy

| Model | CPU | RAM | Image | Graphics Path | Display Surface |
|---|---|---|---|---|---|
| **Pi 5** | ARMv8 | 2–8GB | 64-bit `.img` | Mesa → KMS/DRM → cage/XWayland → pi3d | Wayland + XWayland shim |
| **Pi 4** | ARMv8 | 1–8GB | 64-bit `.img` | Mesa → KMS/DRM → cage/XWayland → pi3d | Wayland + XWayland shim |
| **Pi 3** | ARMv8 | 1GB | 64-bit `.img` | Mesa → KMS/DRM → cage/XWayland → pi3d | Wayland + XWayland shim |
| **Pi 2** | ARMv7 | 1GB | 32-bit manual install | Mesa → KMS/DRM → cage/XWayland → pi3d | Wayland + XWayland shim |
| **Pi Zero 2 W** | ARMv8 | 512MB | 32-bit manual install | Mesa → KMS/DRM → cage/XWayland → pi3d | Wayland + XWayland shim |
| **Radxa Zero 3W** | ARMv8 | 1–4GB | Debian/Ubuntu | Mesa → KMS/DRM → cage/XWayland → pi3d or PyOpenGL | Wayland + XWayland shim |

**Key insight:** On Trixie, pi3d needs an X11 surface — but you don't need the full `xorg` server. Just `xwayland` (~10MB) plus a minimal Wayland compositor like `cage`. The compositor owns the display via DRM/KMS, XWayland provides the X11 compatibility layer pi3d needs, and pi3d renders via Mesa EGL.

**Launch commands:**

```bash
# All Trixie-based platforms (Pi Zero 2 W / Pi 2 / Pi 3 / Pi 4 / Pi 5):
cage -- python3 -m metixel --mode frontend --config etc/config.json
# cage starts a Wayland session + XWayland — pi3d gets its X11 surface
```

The application code and `Pi3dBackend` are identical across all platforms. Only the launch wrapper changes.
| **GPU Memory** | `dtoverlay=vc4-kms-v3d` + `gpu_mem=16` | `dtoverlay=vc4-kms-v3d` + `gpu_mem=16` |
| **Kernel** | 6.6 LTS (Trixie default) | 6.6 LTS (Trixie default) |
| **Init System** | systemd (stripped) | systemd or Busybox init (Buildroot) |

### 1.4 Core Dependencies (Python 3.9+)

| Package | Purpose | Phase 1 | Phase 2 |
|---|---|---|---|
| `pi3d` | OpenGL ES rendering (via Mesa EGL on Trixie) | Yes | No |
| `Pillow` | Image loading, EXIF parsing, resizing | Yes | Yes |
| `numpy` | Array operations for image processing | Yes | Yes |
| `Flask` | Lightweight HTTP server for web dashboard | Yes | Yes |
| `watchdog` | File system monitoring for media folders | Yes | Yes |
| `paho-mqtt` | MQTT client for Home Assistant | Yes | Yes |
| `requests` | HTTP client for Immich API sync | Yes | Yes |
| `python-cec` | HDMI-CEC control | Yes | Yes |
| `lirc` | IR remote control | Yes | Yes |

---

## 2. System Architecture Diagram

```mermaid
graph TB
    subgraph "External Systems"
        IMMICH[Immich Server<br/>REST API]
        HA[Home Assistant<br/>MQTT Broker]
    end

    subgraph "Metixel Photoframe — Core System"
        subgraph "Backend Daemon (Python)"
            SYNC[Sync Engine<br/>Immich + Folder Watcher]
            OPTQ[Optimisation Queue<br/>Image resize + Video transcode]
            STATE[State Manager<br/>JSON Config + inotify]
            HTTP[Web Server<br/>Flask on :8080]
            MQTT[MQTT Client<br/>paho-mqtt]
            CEC[CEC Handler<br/>python-cec]
            IR[IR Handler<br/>lirc]
        end

        subgraph "Frontend Renderer (Python, separate process)"
            ENGINE[Presentation Engine<br/>Slideshow + Transitions]
            WIDGETS[Widget Layer<br/>Clock/Weather/Calendar]
            DISPLAY[Display Backend<br/>Abstract Interface]
            DBMX[DispmanxBackend<br/>pi3d — Phase 1]
            WL[WaylandBackend<br/>PyOpenGL — Phase 2]
        end

        subgraph "Web Dashboard"
            WEBUI[Responsive SPA<br/>Vanilla JS]
        end

        subgraph "Shared State"
            CONFIG[config.json<br/>in /opt/metixel/etc/]
            CACHE[Media Cache<br/>/opt/metixel/cache/]
            SOCKET[Unix Domain Socket<br/>/run/metixel/control.sock]
        end
    end

    subgraph "OS Layer"
        SYSTEMD[systemd Services<br/>metixel-backend.service<br/>metixel-cage.service]
        NETWORK[wpa_supplicant<br/>Wi-Fi Portal fallback]
        OTA[OTA Updater<br/>git-based (GitHub Releases)]
    end

    IMMICH -->|HTTPS| SYNC
    HA <-->|MQTT| MQTT
    SYNC -->|Metadata stubs| OPTQ
    OPTQ -->|Optimised items| CACHE
    OPTQ -->|Ready-to-play| STATE
    STATE <--> CONFIG
    HTTP --> WEBUI
    STATE -->|inotify event| ENGINE
    STATE <-->|Control Socket| ENGINE
    ENGINE --> DISPLAY
    DISPLAY --> DBMX
    DISPLAY --> WL
    ENGINE --> WIDGETS
    CEC -->|HDMI Events| STATE
    IR -->|IR Events| STATE
    MQTT -->|Commands| STATE
    SYSTEMD -->|Manages| HTTP
    SYSTEMD -->|Manages| ENGINE
```

### 2.1 Process Architecture

The system runs as **two systemd services**:

1. **`metixel-backend.service`** — The long-running backend daemon:
   - Sync engine (Immich polling, folder watching)
   - Media processor (background thread pool)
   - Web server (Flask on `0.0.0.0:8080`)
   - MQTT client
   - CEC/IR input handlers
   - Writes `config.json` on settings change

2. **`metixel-frontend.service`** — The display renderer:
   - Starts AFTER `metixel-backend.service`
   - Opens the display backend
   - Reads `config.json` at startup, watches for `inotify IN_MODIFY` events
   - Runs the main render loop
   - Connects to `/run/metixel/control.sock` for immediate commands

**Why two processes?** The frontend renderer must own the GPU context (EGL context is bound to one process). The backend handles I/O-heavy operations that could cause frame drops if run in the same thread.

---

## 3. File & Directory Structure

```
metixel-photoframe/                           # Repository root
├── src/                                # Source layout (packages installed from here)
│   └── metixel/                        # Python package
│       ├── __init__.py
│       ├── __main__.py                 # Entry point
│       │
│       ├── backend/                    # Backend daemon
│       │   ├── __init__.py
│       │   ├── daemon.py               # Main daemon orchestrator
│       │   ├── sync/
│       │   │   ├── __init__.py
│       │   │   ├── immich.py           # Immich API client
│       │   │   ├── folder_watcher.py   # inotify-based folder sync
│       │   │   └── scheduler.py        # Cron-like sync scheduling
│       │   ├── processing/
│       │   │   ├── __init__.py
│       │   │   ├── image.py            # EXIF parse, resize, downsample, rotate
│       │   │   ├── video.py            # Facade: process(), needs_optimisation(), delegates to helpers
│       │   │   ├── probe.py            # ffprobe wrappers, RAM/Pi-model detection
│       │   │   ├── ffmpeg_cmds.py      # Pure ffmpeg/ffprobe command builders
│       │   │   ├── frames.py           # Thumbnail + first/last frame extraction, cache cleanup
│       │   │   ├── thumbnail.py        # Image thumbnail generation
│       │   │   ├── worker.py           # Subprocess worker for heavy PIL work
│       │   │   ├── utils.py            # nice_cmd + shared helpers
│       │   │   ├── optimisation_queue.py  # 4-phase pipeline orchestrator
│       │   │   └── matte.py            # Virtual matte board generation
│       │   ├── web/
│       │   │   ├── __init__.py
│       │   │   ├── server.py           # Flask application
│       │   │   ├── helpers.py          # Shared error/validation/daemon-access helpers
│       │   │   ├── routes/
│       │   │   │   ├── __init__.py
│       │   │   │   ├── config.py       # Config CRUD (core sections)
│       │   │   │   ├── system.py       # Restart/reboot/shutdown, quiet boot, system info
│       │   │   │   ├── time.py         # Server time, timezone, NTP
│       │   │   │   ├── input.py        # Keyboard mapping + learn
│       │   │   │   ├── control.py      # Realtime frontend control (IPC)
│       │   │   │   ├── health.py       # Status/diagnostics endpoints
│       │   │   │   ├── browse.py       # Filesystem folder browser
│       │   │   │   ├── media.py        # Upload, list, delete media
│       │   │   │   ├── logs.py         # System log viewer
│       │   │   │   ├── immich.py       # Immich sync status/config
│       │   │   │   ├── messages.py     # On-screen message endpoints
│       │   │   │   ├── network.py      # Wi-Fi/network configuration
│       │   │   │   └── updates.py      # OTA update endpoints│       │   │   ├── media_service.py    # Flask-free media filesystem logic (extracted from media.py)│       │   │   ├── static/
│       │   │   │   ├── css/
│       │   │   │   │   └── dashboard.css
│       │   │   │   └── js/
│       │   │   │       ├── main.js            # SPA entry (ES module orchestration)
│       │   │   │       ├── core.js            # shared infra (router, api, utils)
│       │   │   │       ├── dashboard-page.js
│       │   │   │       ├── settings-page.js
│       │   │   │       ├── network-page.js
│       │   │   │       ├── sync-page.js
│       │   │   │       ├── media-page.js
│       │   │   │       ├── logs-page.js
│       │   │   │       ├── advanced-page.js
│       │   │   │       └── updates-page.js
│       │   │   └── templates/
│       │   │       └── index.html
│       │   ├── input_handlers/
│       │   │   ├── __init__.py
│       │   │   ├── cec.py              # HDMI-CEC via libcec
│       │   │   └── ir.py               # LIRC-based IR remote
│       │   ├── mqtt_client.py          # Home Assistant MQTT integration
│       │   ├── system_metrics.py       # SystemMetrics — CPU/mem/swap/disk sizing service
│       │   └── state.py                # StateManager
│       │
│       ├── frontend/                   # Display renderer
│       │   ├── __init__.py
│       │   ├── renderer.py             # Main render loop, frame timing
│       │   ├── presentation/
│       │   │   ├── __init__.py
│       │   │   ├── engine.py           # Facade: PresentationEngine (mixins)
│       │   │   ├── base.py             # BaseEngineState — shared engine state
│       │   │   ├── queue.py            # Playlist controller mixin
│       │   │   ├── scheduler.py        # Slideshow scheduler mixin
│       │   │   ├── rendering.py        # Frame rendering + crossfade mixin
│       │   │   ├── preload.py          # Texture preload mixin
│       │   │   ├── video_state.py      # Video state machine mixin
│       │   │   ├── transitions.py      # Fade, slide, zoom, Ken Burns math
│       │   │   ├── layout.py           # Fit-to-screen, virtual matte, smart crop
│       │   │   ├── video_player.py     # Facade: VlcVideoPlayer re-export
│       │   │   └── vlc_player.py       # VLC subprocess video playback
│       │   ├── widgets/
│       │   │   ├── __init__.py
│       │   │   ├── base.py             # Widget ABC
│       │   │   ├── clock.py            # Digital & analog clock widget
│       │   │   ├── weather.py          # Weather forecast widget
│       │   │   └── calendar.py         # Calendar events widget
│       │
│       ├── display/                    # Display backend abstraction
│       │   ├── __init__.py             # detect_backend() → returns correct Backend
│       │   ├── backend.py              # DisplayBackend ABC
│       │   ├── hardware.py             # GpuInfo / WlrOutput / DisplayPower adapters
│       │   ├── dispmanx_backend.py     # Phase 1: pi3d wrapper
│       │   ├── wayland_backend.py      # Phase 2: PyOpenGL on DRM/Wayland (future)
│       │   └── tk_backend.py           # Desktop dev: tkinter-based
│       │
│       └── shared/                     # Shared types and utilities
│           ├── __init__.py
│           ├── config.py               # Config schema, validation, defaults
│           ├── models.py               # Data classes: MediaItem, Album, Widget, etc.
│           ├── ipc.py                  # Unix socket protocol (JSON messages)
│           ├── log_buffer.py           # In-memory log ring buffer
│           ├── ports.py                # Clean Architecture Protocol ports
│           ├── adapters.py             # Concrete adapters (requests, paho, libcec…)
│           ├── system_stats.py         # /proc system stats + GPU log formatting
│           ├── platform.py             # Pi detection + vcgencmd helpers
│           ├── io.py                   # Atomic write + safe JSON read helpers
│           ├── paths.py                # Install-root / run-dir path resolution
│           ├── subprocess.py           # run_cmd / run_sudo / schedule_sudo
│           ├── media.py                # Media extension sets + content hash + fingerprint
│           └── retry.py                # Retry with exponential backoff
│
├── etc/                               # Configuration files
│   ├── config.json                    # Main runtime configuration
│   └── logging.conf                   # Python logging configuration
│
├── scripts/                           # Build & deployment scripts
│   ├── build_phase1.sh               # Build Trixie Lite image for Pi 2/3/Zero 2 W
│   ├── quiet_boot.sh                 # Splash screen + silent boot config
│   ├── setup_ap.sh                   # Wi-Fi captive portal setup
│   ├── setup_trixie.sh              # Install deps for Trixie Lite (cage + pi3d)
│   └── run_trixie.sh                # Launch via cage (Wayland + XWayland) on Trixie
│
├── systemd/                           # systemd unit files
│   ├── metixel-backend.service       # Backend daemon (all platforms)
│   ├── metixel-cage.service          # Frontend under cage (Trixie/KMS)
│   └── metixel-frontend.service      # Frontend direct (legacy Bullseye)
│
├── tests/                             # Automated tests (mirrors src/metixel domains)
│   ├── backend/
│   ├── frontend/
│   ├── display/
│   └── shared/
│
├── docs/                              # Documentation
│   ├── CHANGELOG.md                   # Release notes
│   ├── FEATURES.md                    # Feature list
│   ├── INSTALLATION.md                # Install + set up your frame
│   ├── USER_GUIDE.md                  # Reference manual
│   ├── FAQ.md
│   ├── THIRD_PARTY_LICENSES.md        # Dependency licenses
│   ├── NOTICE                         # Attributions
│   └── WIDGET_DEV.md
│
├── ARCHITECTURE.md                    # This file
├── CLAUDE.md                          # AI assistant instructions
├── README.md                          # Project overview
├── LICENSE                            # AGPL v3
├── requirements-pip.txt               # Python deps
├── requirements-apt-system.txt        # System packages for apt insssssssstall
└── pyproject.toml                     # Modern Python project metadata
```

---

## 4. State Management & Hot Reload Strategy

### 4.1 Configuration Flow

```
Web UI (browser)
    │  PUT /api/config
    ▼
Backend Daemon (Flask)
    │  1. Validate against config schema
    │  2. Write atomically to config.json (write temp → os.replace)
    │  3. Touch /run/metixel/config.updated flag file
    ▼
config.json on disk  ──inotify IN_MODIFY──▶  Frontend Renderer
                                              │
                                              │  Reload config struct
                                              │  Apply changes without restart
                                              ▼
                                         Render loop continues
```

### 4.2 Real-Time Control

For commands needing immediate action:

```
IR Remote / CEC / MQTT / Web UI
    │
    ▼
Backend Daemon receives command
    │
    ▼
Unix Domain Socket (/run/metixel/control.sock)
    │  JSON message: {"cmd": "next", "album_id": "abc123"}
    ▼
Frontend Renderer (non-blocking socket check in render loop)
    │  Process command immediately
    ▼
Next frame reflects change
```

### 4.3 Frame Integrity During Config Changes

- Config read into **immutable snapshot** at start of each slideshow cycle
- Inotify events are **coalesced** — only last change applied per cycle
- GPU texture cache uses **LRU eviction** — never during active crossfade
- Render loop at fixed tick rate (30 FPS), yielding CPU to backend process

---

## 5. Phase-by-Phase Implementation Roadmap

### Phase 0: Project Scaffolding & Development Environment (CURRENT)

**Duration:** 1–2 weeks  
**Goal:** Bootable dev environment, CI pipeline, placeholder files.

1. [x] Initialize monorepo with directory structure
2. [x] Create `pyproject.toml` with dependencies
3. Create `tk_backend.py` (tkinter-based) for local testing without Pi hardware
4. Write the `DisplayBackend` ABC with all methods documented
5. Set up GitHub Actions for linting (ruff), type checking (mypy), and unit tests

### Phase 1: Core on Raspberry Pi 2/3/4/5 (Trixie)

**Duration:** Complete (v1.0.1-beta.1)  
**Goal:** Working digital photo frame on Pi 2 (1GB), Pi 3 (1GB), and Pi 5 (2GB+). Pi 4 supported but untested. Pi Zero 2 W (512MB) is untested — images only, no video or optimisation.

#### Step 1.1: OS Image & Quiet Boot
- Trixie Lite base image (64-bit for Pi 3/4/5; 32-bit for Pi 2/Zero 2 W)
- Pre-built `.img` available for 64-bit models; manual install for 32-bit
- `config.txt`: `dtoverlay=vc4-kms-v3d`, `gpu_mem=16`, `disable_splash=1`
- `cmdline.txt`: `console=tty3 quiet loglevel=3 logo.nologo`
- Plymouth splash screen with metixel logo (optional)
- Debug mode: GPIO pin 17 HIGH or `/boot/debug` file → verbose boot

#### Step 1.2: Display Backend (pi3d via cage/XWayland)
- `dispmanx_backend.py` wrapping pi3d — pi3d auto-detects Mesa EGL on Trixie
- Hardware introspection (GPU memory, wlr-randr output, display power)
  lives in `display/hardware.py` (`GpuInfo` / `WlrOutput` / `DisplayPower`)
- Memory: `Texture(free_after_load=True)`, `GL_RGB565` format, max 3 GPU textures
- Verify 30 FPS on Pi 3 and Pi 5
- Launch via `cage --` for Wayland + XWayland surface

#### Step 1.3: Presentation Engine
- Slideshow queue with configurable display duration
- Transition effects using pi3d blend shaders
- Ken Burns effect via texture coordinate animation
- Virtual matte board and fit-to-screen layout
- Video playback via ffmpeg → numpy → `Texture.update_ndarray()`

#### Step 1.4: Backend Daemon
- StateManager with atomic JSON writes
- System metrics (CPU/memory/swap/disk/cache sizing) live in
  `backend/system_metrics.py` (`SystemMetrics`), injected into StateManager
- Immich API client (auth, albums, assets, download)
- Folder watcher (inotify + polling fallback)
- MediaProcessor (resize, EXIF, transcode, cache)

#### Step 1.5: Web Dashboard
- Flask backend with responsive SPA
- Pages: Dashboard, Settings, Media, Widgets, Logs

#### Step 1.6: Input & Control
- CEC handler via python-cec
- IR handler via LIRC
- MQTT client for Home Assistant
- Wi-Fi captive portal fallback

#### Step 1.7: Power Management
- Screen off timer: `vcgencmd display_power 0`
- Configurable on/off schedule

### Phase 2: Non-Pi SBCs (Radxa Zero 3W, etc.)

**Duration:** Future
**Goal:** Adapt Phase 1 codebase to Wayland/KMS on non-Pi hardware.

#### Step 2.1: Wayland Display Backend
- Implement `wayland_backend.py` using PyOpenGL + EGL/GBM/DRM
- Reuse all presentation engine and widget code unchanged

#### Step 2.2: Buildroot Image
- Buildroot external tree for metixel-photoframe
- Python 3, Mesa, Wayland (cage), ffmpeg, Pillow, numpy
- Target sub-300MB rootfs, <5 second boot

#### Step 2.3: Balena Integration
- Containerized metixel with fleet management

#### Step 2.4: OTA Updates (Non-Balena)
- Git-based in-place self-update via `UpdateManager` + GitHub Releases — already
  live in Phase 1, no bootloader A/B scheme required
- Optional future: signed A/B partition updates for non-git prebuilt images

### Phase 3: Polish & Ecosystem

**Duration:** Ongoing
- Calendar widget (CalDAV)
- Weather widget (OpenWeatherMap)
- Multi-language localization
- Community widget marketplace

---

## 6. Detailed Subsystem Design

### 6.1 Media Processing Pipeline (4-Phase)

```
┌─────────────────────────────────────────────────────────────┐
│ Phase 1: WATCH (FolderWatcher)                              │
│                                                             │
│  Scan enabled watch paths → gather minimal metadata only:   │
│  • File type (image/video by extension)                     │
│  • Pixel dimensions (PIL header for images, ffprobe for     │
│    videos)                                                  │
│  • Video codec name (for threshold gating)                  │
│                                                             │
│  Does NOT resize, transcode, or add to playlist.            │
│  Pushes metadata stubs to the OptimisationQueue.            │
└──────────────────────┬──────────────────────────────────────┘
                       │ list of MediaItem stubs
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ Phase 2: OPTIMISE (OptimisationQueue — background thread)   │
│                                                             │
│  ALL videos go through VideoProcessor.process() — frame     │
│  extraction (first + last frame) is always required for     │
│  slideshow preload/swap.  The transcode step is skipped     │
│  for H.264 videos within resolution limits.                 │
│                                                             │
│  Classifies each item against thresholds:                   │
│  ┌─────────────────────┐  ┌─────────────────────┐           │
│  │ Image threshold     │  │ Video threshold     │           │
│  │ Default: display    │  │ Default: display    │           │
│  │ resolution.         │  │ resolution + must   │           │
│  │ Skip if ≤ threshold │  │ be H.264 codec.     │           │
│  │ (UI overridable).   │  │ Skip transcode if   │           │
│  │                     │  │ both met (frames    │           │
│  │                     │  │ still extracted).   │           │
│  └────────┬────────────┘  └────────┬────────────┘           │
│           │                        │                        │
│  Priority: Images first → Videos second.                    │
│  Current job finishes before switching.                     │
│  Cleanup partial transcodes on startup.                     │
│  Frame extraction is idempotent (skips if cached).          │
└──────────────────────┬──────────────────────────────────────┘
                       │ Optimised MediaItems
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ Phase 3: QUEUE (StateManager playlist → PresentationEngine) │
│                                                             │
│  Slideshow playlist contains ONLY ready-to-play items:      │
│  • Images ≤ threshold (no opt needed)                       │
│  • Optimised images (post-resize)                           │
│  • Videos with cached first/last frames                     │
│    (VideoProcessor extracts .1.frame / .2.frame)            │
│  • Transcode-skipped videos (H.264 + within limits)         │
│  • Optimised videos (post-transcode)                        │
│                                                             │
│  Playlist persisted to /run/metixel/playlist.json.          │
│  Frontend reads via polling + inotify.                      │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ Phase 4: SYNC (Immich Syncer → feeds into Phase 1)         │
│                                                             │
│  Downloads to media/sync/immich/ (default).                 │
│  This directory is a watch path — picked up by Phase 1     │
│  on the next poll cycle.                                    │
│  Schedule-based or manually triggered.                      │
└─────────────────────────────────────────────────────────────┘
```

**Image optimisation** is handled by ``ImageProcessor``: EXIF orientation is
applied, oversized images are resized to the display threshold, and a thumbnail
is written to ``cache/thumbnails/``.  Heavy PIL work runs in a subprocess worker
(``processing/worker.py``) so RAM is reclaimed on exit and a crash never takes
down the backend daemon.  Videos land in ``cache/videos/`` with their first/last
frames (``.1.frame`` / ``.2.frame``) alongside.

### Processing Journal (Phase 1/2 state)

`processing/journal.py` is a **single-writer, persisted** per-file state store
at ``<cache_dir>/processing_state.json``.  It records each file's lifecycle and
outcome — ``pending`` / ``processing`` / ``ready`` / ``failed`` / ``skipped`` —
alongside its ``(mtime_ns, size)`` fingerprint and a ``failure_reason``.

* **Owner model** — the ``FolderWatcher`` owns appear/disappear/modify (it
  marks ``pending`` and removes deleted files); the ``OptimisationQueue`` owns
  the processing outcome (``processing`` → ``ready`` / ``failed``).  Every
  mutation goes through the journal's single lock + debounced atomic write
  (temp file + ``os.replace``), so the concurrent threads can never interleave
  writes.
* **Never-picked-up-twice** — the watcher skips any file whose fingerprint is
  unchanged and whose state is terminal (``ready`` / ``failed`` / ``skipped``).
  Ready files are re-gathered once on startup so the tmpfs playlist is rebuilt
  from cache; failed/skipped files are **not** re-attempted while unchanged.
* **Failed videos never play** — ``TranscodeStatus.FAILED`` is excluded from
  ``MediaItem.is_ready_to_play`` and the queue refuses to add failed videos to
  the playlist (no native-resolution fallback).  A file change or the
  dashboard's Retry action (``POST /api/processing/retry``) re-attempts them.
* **Two-phase video pipeline** — videos are processed in two passes.  Phase A
  (*scanning*) runs ``VideoProcessor.scan()`` (probe + thumbnail + first/last
  frames) for **every** queued video, streaming non-transcode videos into the
  playlist immediately and recording scan errors in the journal.  Once all
  scanning is done, Phase B (*transcoding*) runs ``VideoProcessor.transcode()``
  on only the videos the full-profile check flagged, then adds them to the
  playlist.  The scan result (probe info + frame/thumbnail paths) is carried
  into the transcode so the encode never re-probes.
* **Progress phases** — the dashboard renders four bars: ``scanning``,
  ``optimising_images``, ``inspecting_videos`` (probe + thumbnail + frame
  extraction for all videos) and ``transcoding`` (actual ffmpeg encode of the
  flagged subset, which starts only after all scanning completes).

### Image Render Path (Frontend)

Images reach the screen through a double-buffered texture-slot swap, so the
next slide is already on the GPU when the crossfade begins:

1. **Preload** — ``TexturePreloader`` (background thread) decodes the next
   image from disk into a numpy array.
2. **Upload** — on the render thread, ``load_texture()`` copies the array into
   the inactive GPU slot, then ``tex.load_opengl()`` forces the GL upload and
   the backend ``flush_gpu()`` (``glFinish``) ensures the DMA transfer completes
   before ``free_after_load`` releases the CPU buffer.
3. **Crossfade** — at the slide boundary, ``FrameRenderer`` blends the two
   slots in a single GPU pass (crossfade shader).
4. **Swap** — the slots swap; the old active slot becomes the preload target
   for the next image.

At most three textures are GPU-resident at any time (current, next, blend).

### Video Playback Architecture (Frontend)

Video playback in the presentation engine follows a **guaranteed-no-black-screen** order.
The last frame is fully loaded and verified on the GPU **before** VLC is launched.  VLC
renders on top via its own X11 window, so the GPU upload is invisible.

```
┌─ ① First frame already in active texture slot (normal preload → advance flow)

│ ② LOAD last frame from disk → force GL upload → glFinish → verify GL texture
│    If ANY step fails → skip video, advance.  No VLC launched = no black screen.
│
│ ③ Draw first frame to BOTH pi3d buffers (overwrites GL state from step ②)
│
│ ④ Launch VLC subprocess with RC TCP interface (--extraintf rc --rc-host)
│    VLC creates its own X11 window on top of the pi3d display.
│
│ ⑤ WAITING state: poll VLC's RC socket (is_playing) until VLC confirms
│    playback has started.  No timers — real signal from VLC.
│
│ ⑥ PLAYING state: swap timer = now + (duration × 0.50)
│
│ ⑦ SWAP at 50 %: move preloaded last-frame texture into active slot under
│    VLC.  No I/O, no GL work — just a pointer swap.  VLC covers everything.
│
│ ⑧ VLC exits → last frame revealed → crossfade to next slide
└───────────────────────────────────────────────────────────────

Key invariants
    • pi3d Texture objects do NOT create OpenGL textures eagerly.
      ``load_texture(path)`` only loads the JPEG into a numpy array
      (``disk_loaded=True, opengl_loaded=False``).  You MUST call
      ``tex.load_opengl()`` to force the GPU upload, followed by
      a backend ``flush_gpu()`` (``glFinish`` via ctypes) to ensure
      the DMA transfer completes before pi3d's ``free_after_load``
      releases the CPU buffer.  Without the flush, VideoCore IV DMA
      can read freed memory → black texture.

    • Never pass ``free_after_load=False`` to pi3d — it blocks eager
      GL texture creation entirely (``_tex`` stays ``c_ulong(0)``).

    • The VLC RC interface uses TCP (``--rc-host localhost:<port>``),
      NOT Unix sockets.  VLC 3.x LUA CLI ignores ``--rc-unix``.

    • ``_load_texture_for_slot`` loads the new texture BEFORE unloading
      the old one.  If the load fails, the slot keeps its previous
      texture rather than going black.

GPU memory on Pi 2/3
    Pi 2/3 use a STATIC GPU memory partition (``gpu_mem`` in config.txt).
    Set ``gpu_mem=128`` for all Pi models — Pi 2/3 need the static
    partition for the framebuffer (~8 MB at 1080p) plus pi3d textures
    (~4 MB each RGB565).  Pi 4/5 use CMA dynamic allocation and ignore
    ``gpu_mem`` entirely, so a single value keeps the base image portable.
```

**RAM budget for Phase 1 (512MB Pi Zero 2 W — untested):**
- Linux + systemd: ~80MB
- Backend daemon (Python): ~60MB
- Frontend renderer (Python + pi3d): ~80MB
- GPU memory (gpu_mem=128): 128MB (separate pool)
- Media processing (peak, one file): ~100MB
- **Total**: ~320MB of 512MB — tight but workable
- **Mitigation**: Process one file at a time; subprocess for heavy work; `gc.collect()` after large images

### 6.2 Immich API Sync Protocol

```
1. POST /api/auth/login → { "apiKey": "..." }
2. GET /api/albums → [ { "id", "albumName", "assetCount", "updatedAt" } ]
3. For each album:
   GET /api/albums/{id} → { "assets": [ { "id", "originalPath", ... } ] }
4. For each NEW or UPDATED asset:
   GET /api/assets/{id}/original → raw file download
5. MediaProcessor.ingest()
```

- Poll interval: configurable (default 5 min Immich, 30 sec local)
- Sync state persisted in cache/sync_state.json

### 6.3 Virtual Matte Board Algorithm

```python
def create_matte(image_w, image_h, screen_w, screen_h, matte_color):
    image_ratio = image_w / image_h
    screen_ratio = screen_w / screen_h
    if abs(image_ratio - screen_ratio) < 0.02:
        return (fit="fill", matte=None)
    if image_ratio > screen_ratio:
        # Image wider → letterbox top/bottom
        display_h = screen_w / image_ratio
        matte_top = (screen_h - display_h) / 2
        return (fit="contain", matte_rects=[...], color=matte_color)
    else:
        # Image taller → pillarbox left/right
        display_w = screen_h * image_ratio
        matte_left = (screen_w - display_w) / 2
        return (fit="contain", matte_rects=[...], color=matte_color)
```

### 6.4 Widget System Interface

```python
class Widget(ABC):
    position: tuple[int, int]
    size: tuple[int, int]
    refresh_interval: int       # seconds
    z_index: int                # draw order (0=behind, 10=on top)

    def update(self, config: dict, shared_state: dict) -> None: ...
    def draw(self, backend: DisplayBackend) -> None: ...
    def needs_refresh(self) -> bool: ...
```

Widgets render on a transparent overlay layer via a second orthographic camera at higher z_index. Photos render behind widgets without being affected.

### 6.5 Quiet Boot Configuration

**`/boot/config.txt`:**
```
disable_splash=1
avoid_warnings=2
gpu_mem=128
```

**`/boot/cmdline.txt`:**
```
console=tty3 quiet loglevel=3 logo.nologo plymouth.enable=0 vt.global_cursor_default=0 fsck.mode=auto
```

**systemd drop-ins:**
```ini
# /etc/systemd/system/getty@tty1.service.d/noclear.conf
[Service]
TTYVTDisallocate=no
```

Debug mode: `/boot/debug` file OR GPIO 17 pulled HIGH → verbose boot.

### 6.6 OTA Update Mechanism

Updates are applied **in-place** by `UpdateManager` (`backend/update_manager.py`)
via a **git-based self-update** — there is no A/B root partition scheme, no
signed tarball download, no `/boot/autoboot.txt`, and no bootloader `tryboot`
flag. The live git checkout at `/opt/metixel/` is the update target.

#### Discovery

`UpdateManager` polls the GitHub API (default every 6 hours; results cached for
5 minutes) and categorises what is available into three channels:

| Channel | GitHub source | Target ref |
|---|---|---|
| **stable** | Latest non-prerelease release tag | `refs/tags/vX.Y.Z` |
| **beta** | Latest pre-release tag | `refs/tags/vX.Y.Z-beta.N` |
| **dev** | HEAD of `origin/dev` | commit SHA |

The repository is read from the `updates.github_repo` config field; the channel
from `updates.channel`. Auto-checking is governed by `updates.auto_check` and
`updates.check_interval_hours`.

#### Apply flow

The apply step cannot run inside the backend process — stopping the service
kills the process. `UpdateManager` therefore writes a **thin bootstrap** shell
script to `/opt/metixel/cache/metixel-update.sh` and launches it with
`systemd-run` as a **transient unit** (`metixel-update`, `--collect`), so it
runs in its own cgroup and survives the backend being stopped:

1. Stop `metixel-cage.service` (frontend renderer), then `metixel-backend.service`
2. `git config --system --add safe.directory /opt/metixel`
3. `git fetch --tags --force origin`
4. `git reset --hard <target ref>` (stable/beta tag or dev HEAD)
5. **Hand off** to the install steps in the freshly checked-out repo:
   `bash "$REPO/scripts/ota_install.sh" "$REPO"` — which installs missing
   system packages from `requirements-system.txt` (idempotent), then
   `pip install --break-system-packages -e "$REPO"`, then installs/updates the
   runtime pip dependencies from `requirements-pip.txt`.
6. A `trap` on EXIT guarantees `systemctl restart metixel-backend metixel-cage`
   runs whether the update succeeded or failed — the frame is never left black
7. The script deletes itself; the transient unit is collected on exit

The bootstrap is deliberately **thin**: it only stops services, checks out the
target, and delegates to `scripts/ota_install.sh`. Because that script lives
*in the repo*, the running code (which generates the bootstrap at click-time)
is never relied on to know the current install steps — so a device upgrading
from an older release applies the **new** version's install logic, including
newly-required system and pip dependencies.

#### Startup dependency self-heal (single-step OTA dep resolution)

An OTA that comes from *old* running code cannot install deps its bootstrap
didn't know about. To guarantee a device on an older release resolves newly
required runtime deps (e.g. `pillow-heif` for HEIC/HEIF media) in that **same
single upgrade**, the backend runs a dependency self-heal on startup
(`backend/dependencies.py`, wired into `BackendDaemon.run()`):

1. Read `requirements-pip.txt` and, using `importlib.metadata`, detect any
   missing runtime dependency (fast no-op on normal boots).
2. If any are missing, install them as **root** via
   `sudo -n systemd-run --wait --collect --unit=metixel-deps python3 -m pip
   install --break-system-packages -r requirements-pip.txt`.

The install is run through `systemd-run` (a fresh, non-hardened transient
unit) because the backend service is hardened (`ProtectHome=yes` →
`/home` read-only; `ProtectSystem=full` → `/usr` read-only), so it can neither
write to `~/.local` nor to the system dist-packages — and plain `sudo` would
inherit the hardened mount namespace. Running as root into the **system**
dist-packages also keeps deps in the same location the OTA installs to,
avoiding a split-brain of deps spread across `~/.local` and `/usr`.
Failures are logged and never raised; the backend always continues to start.

---

### 6.7 Dependency Inversion (Ports & Adapters)

The backend depends on external systems (Raspberry Pi hardware and third-party
APIs) through **ports** — `typing.Protocol` interfaces defined in
`src/metixel/shared/ports.py`. Concrete **adapters** in
`src/metixel/shared/adapters.py` wrap the real libraries; they are injected at
the composition root (`BackendDaemon(..., ports=Ports(...))`).

```
┌────────────────────────────────────────────────────────────────┐
│ Core business logic (backend/daemon.py, sync, input_handlers) │
│   depends only on Protocols in shared/ports.py                │
└──────────────────────────────┬─────────────────────────────────┘
                               │ dependency inversion
┌──────────────────────────────▼─────────────────────────────────┐
│ Adapters (shared/adapters.py)  — real implementations         │
│   RequestsHttpGateway · PahoMqttGateway · LibCecAdapter        │
│   LircSocketAdapter · (DisplayBackend already abstracted)     │
└────────────────────────────────────────────────────────────────┘
```

| Port (Protocol) | Real adapter | External dependency |
|---|---|---|
| `HttpGateway` | `RequestsHttpGateway` | Immich API, GitHub API (OTA) |
| `MqttGateway` | `PahoMqttGateway` | Home Assistant broker |
| `CecController` | `LibCecAdapter` | HDMI-CEC (TV remote) |
| `IrSocket` | `LircSocketAdapter` | LIRC IR remote |
| `DisplayDriver` | display factory (`detect_backend`) | pi3d / PyOpenGL / tkinter |

Every service constructor accepts its port with a **real default**, so existing
behaviour is unchanged when no ports are injected; tests inject lightweight
fakes. The display backend was already abstracted via the `DisplayBackend` ABC
— `DisplayDriver` documents that contract as a port.

The **composition root** is `src/metixel/__main__.py`: it parses CLI args,
configures logging, then delegates to `build_backend()` / `build_renderer()`.
Those factories (in `backend/daemon.py` and `frontend/renderer.py`) resolve the
default adapters and return the runnable object — keeping `__main__.py` free of
business logic and giving tests a single seam to inject fakes
(e.g. `build_backend(config, ports=Ports(http=fake))`).

**Rules of thumb (for AI-assisted development):**
- Core business logic must NEVER import third-party libraries directly — add a
  `typing.Protocol` port in `src/metixel/shared/ports.py` and a concrete adapter
  in `src/metixel/shared/adapters.py`, then inject it via constructor with a
  **real default** (behaviour unchanged when nothing is injected).
- New external dependencies (APIs, hardware, services) get a Protocol + adapter —
  never a direct import in core.
- `src/metixel/__main__.py` stays thin: CLI parsing + logging only, delegating to
  the `build_*` factories.
- Unit tests mirror the package (`tests/backend|frontend|display|shared/`) and use
  fakes implementing the ports (the Protocols are `@runtime_checkable`, so
  `isinstance(fake, HttpGateway)` works). Hardware-dependent tests use
  `pytest.importorskip`. Web tests use the shared fixtures in
  `tests/backend/web/conftest.py` (real `create_app()` + mocked outbound deps).

---

## Appendix A: Key Technical Constraints

| Constraint | Pi Zero 2 W — 512MB (untested) | Mitigation |
|---|---|---|
| RAM | 512MB (shared via split) | `gpu_mem=128`; one file at a time; subprocess for heavy work |
| CPU | Quad-core ARM Cortex-A53 @ 1GHz | HW video decode; image-only (no video on 512MB) |
| GPU | VideoCore IV @ 400MHz | Max 3 GPU textures; GL_RGB565; 30 FPS cap |
| Storage | SD card (8–32GB) | Aggressive caching; LRU eviction; warn if <500MB |
| Network | 2.4GHz Wi-Fi 4 | Background downloads; process locally at low priority |

## Appendix B: Comparison with Existing Projects

| Feature | Metixel Photoframe | pi3d PictureFrame | dakboard | MagicMirror² |
|---|---|---|---|---|
| No X11/Wayland (Phase 1) | Yes | Yes | No | No |
| Immich integration | Yes — native | No | No | No (3rd party) |
| Home Assistant MQTT | Yes — native | No | No | Yes |
| Pi Zero 2 W support | Untested | Yes | No | No |
| Video playback | Yes — HW accel | No | No | Yes |
| Hot reload config | Yes | No | No | No |
| OLED protection | Yes | No | No | No |
| OTA updates | Yes | No | No | No |
