<p align="center">
  <a href="https://github.com/dennisadvani/metixel-photoframe">
    <img src="docs/images/metixel_logo_red_white.png" alt="Metixel Photoframe" width="450">
  </a>
</p>

<p align="center">
  <em>A custom, open-source digital photo frame OS for Raspberry Pi.<br>Beautiful slideshows. Immich integration. Web UI. Zero-cloud. Just pictures and videos.</em>
</p>

<!-- <p align="center">
  <img src="docs/images/digital_frame_wall.PNG" alt="Metixel digital photo frame on a wall" width="70%">
</p> -->

<p align="center">
  <img src="docs\images\metixel_youtube_400px_5fps.gif" alt="Metixel Photoframe in action"  width="70%">
</p>

<p align="center">
  <img src="docs/images/metixel_web_ui.PNG" alt="Metixel Web Dashboard" width="70%">
</p>

---

## What is Metixel Photoframe?

Metixel Photoframe turns a Raspberry Pi into a polished, hardware-accelerated digital photo frame. It's a complete operating system overlay — not just an app — designed to run headless on a wall or shelf, pulling photos from local folders, USB drives, network shares, or an [Immich](https://immich.app/) server.

- **Web UI** on port 80/http — configure everything from a browser or phone, no text file editing required
- **Smooth crossfade transitions** with hardware OpenGL ES rendering
- **Native Immich v3 sync** — auto-pull albums and assets
- **Video playback** via VLC
- **Runs locally** — no cloud dependency, your photos stay on your network
- **Screen Off Hours** — turn off the screen at night to save power
- **Over the Air Updates** — update via the web UI
- **EXIF Auto Rotation** — automatically rotate photos based on EXIF metadata
- **Video Transcoding** — convert videos to a format that a Pi 2 can play
- **Keyboard and Remote Support** — assign keys to controls via the web UI
- **Network Management** — Wi-Fi scanning + connection, AP fallback with captive portal + PIN gate, auto-deactivation, connectivity check

---

## Building Your Own Digital Photoframe

I recommend repurposing a TV or old monitor to build your digital picture frame. In my case, I used an old monitor, had a professional framer create a frame and matte and mounted the monitor behind along with the Raspberry Pi. Instructions for building your own digital picture frame are available [here](https://www.thedigitalpictureframe.com/how-i-built-a-stunning-32-inch-4k-digital-picture-frame-with-a-raspberry-pi-4-featuring-smooth-image-crossfading-transitions/), [here](https://medium.com/women-make/diy-digital-photo-frame-in-less-than-20-minutes-69bc35ed6364), and [here](https://www.noahrousell.com/blog/making-a-digital-pho/).


---

## Hardware Support

### Supported Models

| Model | GPU | Max Playback | Transcoding | Status | OS | IMG Available |
|---|---|---|---|---|---|---|
| Pi 5 | VideoCore VII | 4K | Yes — HW H.265 | Yes | Trixie 13 Lite (64-bit) | Yes |
| Pi 4 | VideoCore VI | 4K (untested) | Yes | Untested | Trixie 13 Lite (64-bit) | Yes |
| Pi 3 B/B+ | VideoCore IV | 1080p | Yes  | Yes | Trixie 13 Lite (64-bit) | Yes |
| Pi 2 B | VideoCore IV | 1080p | Yes  | Yes | Trixie 13 Lite (32-bit) | No — Manual Install |
| Pi Zero 2 W | VideoCore IV | No (RAM Limit) | No (RAM Limit) | Untested | Trixie 13 Lite (32-bit) | No — Manual Install |

### RAM Requirements

| RAM | Photo Playback | Video Playback | Image Optimisation | Video Transcoding |
|---|---|---|---|---|
| 512MB | Yes | No | No | No |
| 1GB | Yes | Yes | Yes | Yes — up to 1080p H.264 |
| 2GB | Yes | Yes | Yes | Yes — up to 4k H.265 (Ultrafast) |
| 4GB+ | Yes | Yes | Yes | Yes — 4k H.265 at Higher Qualities |

> **Notes:**
> - A SWAP file is **required** on all models.
> - Pi 3 and Pi 2 hardware video decoder is limited to **1080p** — higher-resolution video must be transcoded down.
> - Exhausting memory and swap will cause the Raspberry Pi to lock up and eventually reboot.

**Tested on:** Raspberry Pi 2 (1GB), Pi 3 (1GB), and Pi 5 (2GB) running Debian Trixie (13) Lite.

See [`docs/HARDWARE.md`](docs/HARDWARE.md) for detailed setup and accessory recommendations.

---

## Quick Start

```
1. Download the pre-built image from GitHub Releases
2. Flash it with Raspberry Pi Imager
3. Boot your Pi → done.
```

**[Download latest release](https://github.com/dennisadvani/metixel-photoframe/releases/latest)**

Full step-by-step guide: **[Getting Started](docs/GETTING_STARTED.md)**

---

## Installation

Two ways to install — both work on Raspberry Pi 3, 4, and 5.

| | Pre-built image | Manual install |
|---|---|---|
| **Time** | ~10 minutes | ~1 hour |
| **Method** | Download .img.zip → flash with Pi Imager → boot | Flash Trixie Lite → run setup script → reboot |
| **Guide** | [Getting Started](docs/GETTING_STARTED.md) | [Installation Guide](docs/INSTALLATION.md) |

The manual setup script prompts for your **release channel** (stable/beta)
and **WiFi country code** before installing — no config file editing needed.

After installation, connect to Wi-Fi using one of several methods — see
**[WiFi Setup Guide →](docs/WIFI_SETUP.md)**

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
| **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** | Full user guide — setup, adding media, customising, troubleshooting |
| **[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)** | Quick-start guide — 10 minutes to first slideshow |
| **[docs/INSTALLATION.md](docs/INSTALLATION.md)** | Both install methods in detail (image + manual) |
| **[docs/WIFI_SETUP.md](docs/WIFI_SETUP.md)** | All Wi-Fi connection methods (captive portal, Ethernet, SSH, etc.) |
| **[docs/HARDWARE.md](docs/HARDWARE.md)** | Hardware setup, wiring, and accessories |
| [`FEATURES.md`](FEATURES.md) | Complete feature list |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Full system design & implementation roadmap |
| [`docs/API.md`](docs/API.md) | REST API and IPC protocol reference |
| [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) | Dependency license details |

---

## Contributing

Contributions are welcome! Before diving in:

1. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) — it's the single source of truth
2. Check the [issues](https://github.com/dennisadvani/metixel-photoframe/issues) for open tasks
3. Set up a dev environment — see **[Development Setup →](docs/INSTALLATION.md#path-c-development-setup-remote-via-samba)**


---

## License

[Apache 2.0](LICENSE) © 2024–2026 Metixel Photoframe Contributors

See [`NOTICE`](NOTICE) for attributions and [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) for third-party dependency licenses.
