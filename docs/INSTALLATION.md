# Installation Guide

Two paths to a running Metixel frame. Both work on Raspberry Pi 3, 4, and 5.

| | Path A: Pre-built image | Path B: Manual install |
|---|---|---|
| **Time** | ~10 minutes | ~1 hour |
| **Skill level** | Beginner | Comfortable with terminal |
| **What you get** | Complete OS with Metixel pre-installed | Stock Trixie + Metixel installed via script |
| **Best for** | Most users, quick setup | Tinkerers, Pi 3 with limited SD cards, inspecting the install |

Both produce an identical result — a Pi that boots directly into the Metixel
slideshow with the web dashboard accessible on port 80.

---

## Path A: Pre-built image (recommended)

### 1. Download

Go to the **[latest release](https://github.com/dennisadvani/metixel-photoframe/releases/latest)**
and download `metixel_trixie_vX.X.X.img.zip` (~1.4 GB).

### 2. Flash

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/):

1. **Choose OS** → **Use custom** → select the `.img.zip` file.
2. **Choose Storage** → select your SD card.
3. Click **Write**.

> **Note:** Pi Imager's advanced options (gear icon) are not available for
> custom images. Configure Wi-Fi after boot using the captive portal or
> Ethernet — see **[WiFi Setup](WIFI_SETUP.md)**.

Alternatively, use `dd` or any other flashing tool:

```bash
unzip metixel_trixie_vX.X.X.img.zip
sudo dd if=metixel_trixie_vX.X.X.img of=/dev/sdX bs=4M status=progress
```

### 3. Boot

Insert the SD card, connect HDMI and power. The Pi boots directly into
Metixel.

### 4. Connect

Follow the **[Getting Started guide](GETTING_STARTED.md)** from Step 4.

---

## Path B: Manual install (flash Trixie yourself)

Use this if you want to start from a clean Debian Trixie install — for
example, on a Pi 3 where you want a smaller initial SD card footprint, or
if you want to inspect or modify the install process.

### 1. Flash Debian Trixie Lite

1. Download **Debian Trixie (13) Lite** for Raspberry Pi from the
   [Raspberry Pi website](https://www.raspberrypi.com/software/operating-systems/).
2. Flash it with Raspberry Pi Imager (select "Raspberry Pi OS Lite" and
   choose the Trixie version) or use `dd`.
3. (Optional) In Pi Imager's ⚙ settings, pre-configure Wi-Fi and enable SSH
   so you can access the Pi after boot without a keyboard.

### 2. Boot the Pi

Insert the SD card, connect HDMI, Ethernet (recommended for the install),
and power. Log in as `pi` (or via SSH).

### 3. Run the setup script

```bash
wget https://raw.githubusercontent.com/dennisadvani/metixel-photoframe/main/scripts/setup_trixie_metixel.sh
sudo bash setup_trixie_metixel.sh
```

The script runs for 30–60 minutes and handles everything:

| Step | What it does |
|---|---|
| 1 | Installs system packages (cage, XWayland, Mesa, ffmpeg, VLC, Samba, hostapd, dnsmasq) |
| 2 | Configures iptables redirect (port 80 → 8080) for the web dashboard |
| 3 | Installs Python packages (pi3d, Flask, Pillow, etc.) |
| 4 | Creates directory structure under `/opt/metixel` |
| 5 | Installs and enables systemd services (`metixel-backend`, `metixel-cage`) |
| 6 | Enables Wi-Fi (rfkill unblock, nmcli radio on) |
| 7 | Configures the captive portal (hostapd + dnsmasq) |
| 8 | Sets up the Samba share for easy media upload |
| 9 | Configures quiet boot (no kernel messages on screen) |

### 4. Reboot

The script reboots the Pi automatically. After reboot, Metixel starts and
you'll see the boot animation on screen.

### 5. Connect

Follow the **[Getting Started guide](GETTING_STARTED.md)** from Step 4.

---

## Path C: Development setup (remote via Samba)

Metixel development requires a Pi — the display backend (pi3d + Mesa)
doesn't run on a desktop. The workflow is to edit code on your workstation
and run it on the Pi over a Samba mount.

### 1. Set up a Pi

First, install Metixel on a Pi using **Path A** or **Path B** above.

### 2. Run the dev environment script on the Pi

SSH into the Pi and run:

```bash
sudo bash /opt/metixel/scripts/setup_trixie_dev_env.sh
```

This adds a second Samba share (`metixel`) that exposes the full project
tree at `/opt/metixel` — including all source code, not just the media
folder.

### 3. Mount the project from your workstation

| OS | How |
|---|---|
| **Windows** | Open File Explorer → `\\metixel\metixel` → map as a network drive |
| **macOS** | Finder → Go → Connect to Server → `smb://metixel/metixel` |
| **Linux** | `sudo mount -t cifs //metixel/metixel /mnt/metixel -o username=pi` |

### 4. Open in VS Code

Open the mounted folder in VS Code. You're now editing files directly on
the Pi. Use the VS Code terminal (SSH) to run commands on the Pi:

```bash
# Run tests
cd /opt/metixel && python -m pytest tests/ -v

# Lint
cd /opt/metixel && ruff check metixel/

# Type check
cd /opt/metixel && mypy metixel/

# Restart services to test changes
sudo systemctl restart metixel-backend metixel-cage

# Follow logs
sudo journalctl -u metixel-backend -u metixel-cage -f
```

The Pi's IP is `192.168.222.211` by default when using the dev environment
script (Ethernet static IP).

---

## After installation

- **[WiFi Setup](WIFI_SETUP.md)** — All the ways to connect your Pi to Wi-Fi
- **[Configuration Guide](CONFIGURATION.md)** — Every setting explained
- **[Dashboard Walkthrough](DASHBOARD.md)** — Tour of the web UI
- **[Adding Media](MEDIA_SOURCES.md)** — Local files, network shares, Immich
