# Metixel Photoframe Hardware Setup Guide

## Supported Hardware

### Phase 1 (Current)

| Model | RAM | GPU | Image | Notes |
|---|---|---|---|---|
| Raspberry Pi 5 | 2GB / 4GB / 8GB | VideoCore VII | 64-bit `.img` | **Recommended (2GB+)** |
| Raspberry Pi 4 Model B | 1–8GB | VideoCore VI | 64-bit `.img` | Untested |
| Raspberry Pi 3 Model B/B+ | 1GB | VideoCore IV | 64-bit `.img` | 1080p max playback |
| Raspberry Pi 2 Model B | 1GB | VideoCore IV | 32-bit manual install | 1080p max playback |
| Raspberry Pi Zero 2 W | 512MB | VideoCore IV | 32-bit manual install | Untested — images only, no video, optimisation, or transcoding |

### Phase 2 (Planned)

| Model | RAM | GPU | Notes |
|---|---|---|---|
| Radxa Zero 3W | 1–4GB | Mali G52 | Rockchip RK3566 |

## RAM Requirements

| RAM | Video Playback | Optimisation | Transcoding | SWAP |
|---|---|---|---|---|
| 512MB | No | No | No | Required |
| 1GB+ | Yes — up to 1080p | Yes | Yes | Required |

## Required Accessories
- MicroSD card (8GB minimum, Class 10 recommended)
- 5V power supply (3A for Pi 5, 2.5A for Pi 2/3/4, 1.2A for Zero 2 W)
- HDMI cable + display (1080p recommended)
- Optional: IR receiver (TSOP38238 + GPIO), HDMI-CEC capable TV

## SD Card Setup
1. Download the pre-built `.img` from [GitHub Releases](https://github.com/dennisadvani/metixel-photoframe/releases/latest) or flash Debian Trixie Lite manually
2. Flash with Raspberry Pi Imager or `dd`
3. For manual install: run `sudo bash /opt/metixel/scripts/setup_trixie_metixel.sh`
4. Reboot — Metixel auto-starts via systemd
5. Configure via web dashboard at `http://<ip>`
