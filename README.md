# Metixel Photoframe

<p align="center">
  <em>A custom, open-source digital photo frame OS for Raspberry Pi.<br>Beautiful slideshows. Immich integration. Zero-cloud. Just pictures.</em>
</p>

---

## What is Metixel Photoframe?

Metixel Photoframe turns a Raspberry Pi into a polished, hardware-accelerated digital photo frame. It's a complete operating system overlay — not just an app — designed to run headless on a wall or shelf, pulling photos from local folders, USB drives, network shares, or an [Immich](https://immich.app/) server.

- 🖥️ **Web dashboard** on port 80/http — configure everything from any browser
- 🎞️ **Smooth crossfade transitions** with hardware OpenGL ES rendering
- 📷 **Native Immich sync** — auto-pull albums and assets
- 🎬 **Video playback** via VLC
- 🔒 **Runs locally** — no cloud dependency, your photos stay on your network

---

## Features

| Category | Feature | Status |
|---|---|---|
| **Display** | Hardware-accelerated OpenGL ES 2.0 rendering (pi3d + Mesa) | ✅ |
| | Smooth crossfade, slide, and fade transitions | ✅ |
| | Automatic resolution detection | ✅ |
| **Media** | Local folder watching (auto-detect new files) | ✅ |
| | Immich server sync (albums, favorites, people) | ✅ |
| | Video playback | ✅ |
| **Control** | Web dashboard (vanilla JS SPA) | ✅ |
| **System** | systemd services (auto-start on boot) | ✅ |
| | Atomic config writes (no corruption on power loss) | ✅ |

---

## Hardware Support

| Phase | Models | RAM | SWAP | Capabilities | GPU |
|---|---|---|---|---|---|
| Pi Zero 2 W | 512MB | Required | Images only (no video playback or transcoding) | VideoCore IV | ✅ Active |
| Pi 2, Pi 3 | 1GB | Required | Images + video playback (1080p max; >1080p requires transcoding before being queued) | VideoCore IV | ✅ Active |
| **Pi 5** | 2GB+ | Recommended | Images + video playback + transcoding — **recommended platform** | VideoCore VII | ✅ Active |

> **Notes:**
> - A SWAP file is **required** on all models.
> - Pi 3 hardware video decoder is limited to **1080p** — higher-resolution video must be transcoded down.
> - Video playback or transcoding needs at least **1GB RAM** — the Pi Zero 2 W (512MB) is image-only.
> - **Pi 5 (2GB+)** is the recommended platform — it runs the same Trixie image as Pi 2/3 with no changes.

**Tested on:** Raspberry Pi 3 (1GB) and Raspberry Pi 5 (2GB) running Debian Trixie (13) Lite.

See [`docs/HARDWARE.md`](docs/HARDWARE.md) for detailed setup and accessory recommendations.

---

## Installation (Raspberry Pi)

### Prerequisites

- Raspberry Pi 3+ (Pi 5 with 2GB+ recommended)
- MicroSD card (8GB+, Class 10 recommended)
- Debian **Trixie** (13) Lite flashed to the SD card, use [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
- HDMI display (720p, 1080p or 1200p recommended for RPi 3)
- Network connection (Wi-Fi or Ethernet)

### Setup

```bash
wget https://raw.githubusercontent.com/dennisadvani/metixel-photoframe/main/scripts/setup_trixie_metixel.sh
sudo bash setup_trixie_metixel.sh
```

The setup script can take up to an hour and handles everything:
1. Installs git and clones the repository to `/opt/metixel`
2. Installs system packages (cage, XWayland, Mesa, ffmpeg, VLC, Samba)
3. Sets up Python dependencies
4. Installs systemd services (`metixel-backend` + `metixel-cage`)
5. Configures quiet boot (no kernel messages on screen)
6. Configures Wi-Fi captive portal for initial setup
7. Enables I²C, SPI, and HDMI-CEC
8. Sets GPU memory to 16MB (KMS doesn't need more)
9. Reboots automatically when complete

After reboot, the photo frame starts automatically. Access the dashboard at `http://<pi-ip-address>:8080`.

### Manual Service Control

```bash
sudo systemctl restart metixel-backend   # Restart the web dashboard
sudo systemctl restart metixel-cage      # Restart the display renderer
sudo systemctl stop metixel-backend metixel-cage   # Stop everything
sudo journalctl -u metixel-backend -f    # Follow logs
```

---

## Media Sources

| Source | How it works |
|---|---|
| **Local folder** | Via watch paths in the Web UI — new files auto-imported |
| **Network share** | Mount via SMB/NFS, add to watch paths |
| **Immich** | Configure server URL + API key — albums auto-sync every N seconds |

---

## Documentation

| Document | Content |
|---|---|
| [`FEATURES.md`](FEATURES.md) | Complete feature list |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Full system design & implementation roadmap |
| [`docs/HARDWARE.md`](docs/HARDWARE.md) | Hardware setup, wiring, and accessories |
| [`docs/API.md`](docs/API.md) | REST API and IPC protocol reference |
| [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) | Dependency license details |

---

## Contributing

Contributions are welcome! Before diving in:

1. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) — it's the single source of truth
2. Check the [issues](https://github.com/dennisadvani/metixel-photoframe/issues) for open tasks
3. Run scripts/setup_trixie_dev_env.sh to setup a dev SMB share to mount in VS Code

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
