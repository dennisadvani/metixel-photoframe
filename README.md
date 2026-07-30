# Metixel Photoframe

<p align="center">
  <em>A custom, open-source digital photo frame OS for Raspberry Pi.<br>Beautiful slideshows. Immich integration. Zero-cloud. Just pictures.</em>
</p>

---

## What is Metixel Photoframe?

Metixel Photoframe turns a Raspberry Pi into a polished, hardware-accelerated digital photo frame. It's a complete operating system overlay — not just an app — designed to run headless on a wall or shelf, pulling photos from local folders, USB drives, network shares, or an [Immich](https://immich.app/) server.

- 🖥️ **Web dashboard** on port 8080 — configure everything from any browser
- 🎞️ **Smooth crossfade transitions** with hardware OpenGL ES rendering
- 📷 **Native Immich sync** — auto-pull albums and assets
- 🎬 **Video playback** via VLC
- 🏠 **Home Assistant** — MQTT control, presence-based on/off, status reporting
- 🔒 **Runs locally** — no cloud dependency, your photos stay on your network

---

## Features

| Category | Feature | Status |
|---|---|---|
| **Display** | Hardware-accelerated OpenGL ES 2.0 rendering (pi3d + Mesa) | ✅ Phase 1 |
| | Smooth crossfade, slide, and fade transitions | ✅ |
| | Automatic resolution detection | ✅ |
| **Media** | Local folder watching (auto-detect new files) | ✅ |
| | Immich server sync (albums, favorites, people) | ✅ |
| | Video playback | ✅ |
| **Control** | Web dashboard (vanilla JS SPA) | ✅ |
| | MQTT (Home Assistant auto-discovery) | ✅ |
| **System** | systemd services (auto-start on boot) | ✅ |
| | Atomic config writes (no corruption on power loss) | ✅ |

---

## Hardware Support

| Phase | Models | RAM | SWAP | Capabilities | GPU | Status |
|---|---|---|---|---|---|---|
| **Phase 1** | Pi Zero 2 W | 512MB | Required | Images only (no video playback or transcoding) | VideoCore IV | ✅ Active |
| **Phase 1** | Pi 2, Pi 3 | 1GB | Required | Images + video playback (1080p max; >1080p requires transcoding before being queued) | VideoCore IV | ✅ Active |

> **Notes:**
> - A SWAP file is **required** on all models.
> - Pi 3 hardware video decoder is limited to **1080p** — higher-resolution video must be transcoded down.
> - Video playback or transcoding needs at least **1GB RAM** — the Pi Zero 2 W (512MB) is image-only.

**Tested on:** Raspberry Pi 3 (1GB RAM) running Debian Trixie (13) Lite.

See [`docs/HARDWARE.md`](docs/HARDWARE.md) for detailed setup and accessory recommendations.

---

## Installation (Raspberry Pi)

### Prerequisites

- Raspberry Pi 3+
- MicroSD card (8GB+, Class 10 recommended)
- Debian **Trixie** (13) Lite flashed to the SD card
- HDMI display (720p, 1080p or 1200p recommended)
- Network connection (Wi-Fi or Ethernet)

### Setup

```bash
sudo apt install git
sudo mkdir /opt/metixel
sudo chown pi /opt/metixel
git clone https://github.com/dennisadvani/metixel-photoframe.git /opt/metixel
sudo bash /opt/metixel/scripts/setup_trixie_metixel.sh
sudo reboot
```

The setup script handles everything:
1. Installs system packages (cage, XWayland, Mesa, ffmpeg, VLC, Samba)
2. Sets up Python dependencies
3. Installs systemd services (`metixel-backend` + `metixel-cage`)
4. Configures quiet boot (no kernel messages on screen)
5. Enables I²C, SPI, and HDMI-CEC
6. Sets GPU memory to 16MB (KMS doesn't need more)

After reboot, the photo frame starts automatically. Access the dashboard at `http://<pi-ip-address>:8080`.

### Manual Service Control

```bash
sudo systemctl restart metixel-backend   # Restart the web dashboard
sudo systemctl restart metixel-cage      # Restart the display renderer
sudo systemctl stop metixel-backend metixel-cage   # Stop everything
sudo journalctl -u metixel-backend -f    # Follow logs
```

---

## Configuration

All settings live in `etc/config.json`. Changes are picked up live — no restart needed.

```jsonc
{
  "display": {
    "width": 0,              // 0 = auto-detect
    "height": 0,
    "fullscreen": true,
    "fps_limit": 30
  },
  "slideshow": {
    "image_duration_seconds": 30,
    "video_playback_enabled": true,
    "transition_duration_ms": 1500,
    "transition_style": "crossfade",
    "fit_mode": "cover",
    "shuffle": true
  },
  "sync": {
    "immich": {
      "enabled": false,
      "server_url": "https://immich.example.com",
      "api_key": ""
    },
    "local": {
      "enabled": true,
      "watch_paths": ["media/"]
    }
  },
  "mqtt": {
    "enabled": false,
    "broker": "localhost",
    "port": 1883,
    "topic_prefix": "metixel"
  }
}
```

💡 **Tip:** Use the web dashboard at `http://<ip>:8080` for a guided configuration UI — no manual JSON editing required.

See [`etc/config.example.json`](etc/config.example.json) for all options.

---

## Architecture

Metixel Photoframe is a Python 3 monorepo split into a **backend** (web server, sync engines, MQTT) and **frontend** (display renderer, presentation engine, widgets). The two processes communicate over a Unix domain socket.

```
┌──────────────┐     Unix socket     ┌──────────────────┐
│   BACKEND    │◄──────────────────►│    FRONTEND       │
│              │                    │                    │
│  Flask web   │                    │  Display backend   │
│  Immich sync │                    │  (pi3d / PyOpenGL) │
│  MQTT client │                    │  Presentation eng  │
│  CEC/IR      │                    │  Widget layer      │
│  Processing  │                    │  Transitions       │
└──────┬───────┘                    └───────────────────┘
       │
       ▼
   Port 8080
   Web Dashboard
```

The display layer has a hardware abstraction — no widget or presentation code imports pi3d directly. This means:

- **Phase 1** (today): pi3d + Mesa EGL under cage/XWayland on Pi 2/3/Zero 2 W
- **Phase 2** (future): PyOpenGL + direct Wayland/DRM on Pi 4/5
- **Desktop**: pygame software renderer for development

Read [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full technical specification.

---

## Media Sources

| Source | How it works |
|---|---|
| **Local folder** | Point `sync.local.watch_paths` at directories — new files auto-imported |
| **USB drive** | Auto-mounted and imported on insert |
| **Network share** | Mount via SMB/NFS, add to watch paths |
| **Immich** | Configure server URL + API key — albums auto-sync every N seconds |

---

## Documentation

| Document | Content |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Full system design & implementation roadmap |
| [`docs/HARDWARE.md`](docs/HARDWARE.md) | Hardware setup, wiring, and accessories |
| [`docs/API.md`](docs/API.md) | REST API and IPC protocol reference |
| [`docs/WIDGET_DEV.md`](docs/WIDGET_DEV.md) | How to build custom widget overlays |
| [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) | Dependency license details |

---

## Contributing

Contributions are welcome! Before diving in:

1. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) — it's the single source of truth
2. Check the [issues](https://github.com/dennisadvani/metixel-photoframe/issues) for open tasks
3. Test on desktop first with `python -m metixel --mode frontend` (pygame dev backend)
4. The Pi Zero 2 W (512MB) is the tightest constraint — test memory usage there

```bash
# Run tests
python -m pytest tests/ -v

# Lint
ruff check metixel/

# Type check
mypy metixel/
```

---

## License

[Apache 2.0](LICENSE) © 2024–2026 Metixel Photoframe Contributors

See [`NOTICE`](NOTICE) for attributions and [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) for third-party dependency licenses.
