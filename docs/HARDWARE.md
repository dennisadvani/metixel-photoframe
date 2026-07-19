# Metixel Photoframe Hardware Setup Guide

## Supported Hardware

### Phase 1 (Current)
| Model | RAM | GPU | Notes |
|---|---|---|---|
| Raspberry Pi Zero 2 W | 512MB | VideoCore IV | Minimum recommended spec |
| Raspberry Pi 2 Model B | 1GB | VideoCore IV | |
| Raspberry Pi 3 Model B/B+ | 1GB | VideoCore IV | Recommended for best experience |

### Phase 2 (Planned)
| Model | RAM | GPU | Notes |
|---|---|---|---|
| Raspberry Pi 4 Model B | 1-8GB | VideoCore VI | |
| Raspberry Pi 5 | 4-8GB | VideoCore VII | |
| Radxa Zero 3W | 1-4GB | Mali G52 | Rockchip RK3566 |

## Required Accessories
- MicroSD card (8GB minimum, Class 10 recommended)
- 5V power supply (2.5A for Pi 3, 1.2A for Zero 2 W)
- HDMI cable + display (1080p recommended)
- Optional: IR receiver (TSOP38238 + GPIO), HDMI-CEC capable TV

## SD Card Setup
1. Download Debian Trixie Lite for Raspberry Pi
2. Flash with Raspberry Pi Imager or dd
3. Run `sudo bash /opt/metixel/scripts/setup_trixie.sh`
4. Reboot — Metixel auto-starts via systemd
5. Boot and configure via web dashboard at `http://<ip>:8080`
