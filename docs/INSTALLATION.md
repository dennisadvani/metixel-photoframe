# Installation & Setup

Everything from a new Raspberry Pi to your first slideshow — first install
Metixel, then [set up your frame](#set-up-your-frame).

## Contents

- [What you need (hardware)](#what-you-need-hardware)
- [Quick Start (Pi 3+)](#quick-start-pi-3)
- [Path A: Pre-built image (recommended)](#path-a-pre-built-image-recommended)
- [Path B: Manual install (flash Trixie yourself)](#path-b-manual-install-flash-trixie-yourself)
- [Set up your frame](#set-up-your-frame)
  - [Connect to Wi-Fi](#connect-to-wi-fi)
  - [Open the dashboard](#open-the-dashboard)
  - [Add your photos](#add-your-photos)
- [What's next?](#whats-next)

---

Two paths to a running Metixel frame. Both work on Raspberry Pi 3, 4, and 5;
you must take the manual path for Raspberry Pi 2.

> Pi 4 is supported but **untested**. Pi 2 is **manual install only** (no pre-built .img — 32-bit). Pi Zero 2 W is **untested** and manual install only.

| | Path A: Pre-built image | Path B: Manual install |
|---|---|---|
| **Time** | ~10 minutes | ~1 hour |
| **Skill level** | Beginner | Comfortable with terminal |
| **What you get** | Complete OS with Metixel pre-installed | Stock Trixie + Metixel installed via script |
| **Best for** | Most users, quick setup | Tinkerers, Pi 3 with limited SD cards, inspecting the install |

Both produce an identical result — a Pi that boots directly into the Metixel
slideshow with the web dashboard accessible on port 80.

---

## What you need (hardware)

Metixel runs on a Raspberry Pi with 1GB+ of RAM. A Pi 3 (1GB) is recommended
for 1080p playback; a Pi 5 (2GB+) for 4K. A Pi 2 will work, but transcoding is
slow.

| Model | GPU | Max Playback | Video Transcoding | Tested | OS | .img Available |
|---|---|---|---|---|---|---|
| Pi 5 | VideoCore VII | 4K | Yes | Yes | Trixie 13 Lite (64-bit) | Yes |
| Pi 4 | VideoCore VI | 4K (untested) | Yes | No | Trixie 13 Lite (64-bit) | Yes |
| Pi 3 B/B+ | VideoCore IV | 1080p | Yes | Yes | Trixie 13 Lite (64-bit) | Yes |
| Pi 2 B | VideoCore IV | 1080p | Yes | Yes | Trixie 13 Lite (32-bit) | No — Manual Install |
| Pi Zero 2 W | VideoCore IV | No (RAM Limit) | No (RAM Limit) | No | Trixie 13 Lite (32-bit) | No — Manual Install |

### RAM requirements

| RAM | Photo Playback | Video Playback | Image Optimisation | Video Transcoding |
|---|---|---|---|---|
| 512MB | Yes | No | No | No |
| 1GB | Yes | Yes | Yes | Yes — up to 1080p H.264 |
| 2GB | Yes | Yes | Yes | Yes — up to 4K H.265 (Ultrafast) |
| 4GB+ | Yes | Yes | Yes | Yes — 4K H.265 at Higher Qualities |

> **Notes:**
> - A SWAP file is **required** on all models.
> - Pi 3 and Pi 2 hardware video decoder is limited to **1080p** — higher-resolution video must be transcoded down.
> - Exhausting memory and swap will cause the Raspberry Pi to lock up and eventually reboot.
> - **Phase 2 (planned):** Radxa Zero 3W (Rockchip RK3566, Mali G52).

### Required accessories

- MicroSD card (8GB minimum, Class 10 recommended)
- 5V power supply (3A for Pi 5, 2.5A for Pi 2/3/4, 1.2A for Zero 2 W)
- HDMI cable + display (1080p recommended)
- Optional: IR receiver (TSOP38238 + GPIO), HDMI-CEC capable TV

**Tested on:** Raspberry Pi 2 (1GB), Pi 3 (1GB), and Pi 5 (2GB) running Debian
Trixie (13) Lite.

---

## Quick Start (Pi 3+)

```
1. Download the pre-built image from GitHub Releases
2. Flash it with Raspberry Pi Imager
3. Boot your Pi → done.
```

**[Download latest release](https://github.com/dennisadvani/metixel-photoframe/releases/latest)**

Then continue with **[Set up your frame](#set-up-your-frame)** below.

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
> Ethernet — see the **[User Guide](USER_GUIDE.md)**.

Alternatively, use `dd` or any other flashing tool:

```bash
unzip metixel_trixie_vX.X.X.img.zip
sudo dd if=metixel_trixie_vX.X.X.img of=/dev/sdX bs=4M status=progress
```

### 3. Boot

Insert the SD card, connect HDMI and power. The Pi boots directly into
Metixel.

### 4. Connect

Continue with **[Set up your frame](#set-up-your-frame)** below.

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

The script asks two questions upfront before installing anything:

- **Release channel** — `stable` (pins to the latest tagged release) or
  `beta` (tracks the dev branch with the newest features)
- **WiFi country code** — sets the regulatory domain (e.g. `AU`, `US`,
  `GB`) so the radio uses the correct channels for your region

After answering, press Enter to begin. The script runs for 30–60 minutes:

| Step | What it does |
|---|---|
| 1 | Installs system packages (cage, XWayland, Mesa, ffmpeg, VLC, Samba, hostapd, dnsmasq) |
| 2 | Configures iptables redirect (port 80 → 8080) for the web dashboard |
| 3 | Installs Python packages (pi3d, Flask, Pillow, etc.) |
| 4 | Creates directory structure under `/opt/metixel` |
| 5 | Installs and enables systemd services (`metixel-backend`, `metixel-cage`) |
| 6 | Enables Wi-Fi and applies the chosen country code (rfkill unblock, iw reg set) |
| 7 | Configures the captive portal (hostapd + dnsmasq) |
| 8 | Sets up the Samba share for easy media upload |
| 9 | Configures quiet boot (no kernel messages on screen) |

### 4. Reboot

The script reboots the Pi automatically. After reboot, Metixel starts and
you'll see the boot animation on screen.

### 5. Connect

Continue with **[Set up your frame](#set-up-your-frame)** below.

---

## Set up your frame

The Pi has booted into Metixel — time to connect it to your Wi-Fi and put
some photos on it.

### Connect to Wi-Fi

Metixel will show a PIN on screen and create a Wi-Fi hotspot called
**"Metixel-Setup"**. Connect to it from your phone or laptop:

1. Join the `Metixel-Setup` Wi-Fi network (no password needed).
2. Open a browser and go to **http://192.168.42.1**.
3. Enter the 4-digit PIN shown on the frame's display.
4. Select your home Wi-Fi network and enter the password.
5. The frame connects and the hotspot disappears.

> **Using Ethernet instead?** Just plug in a cable. Open
> `http://metixel.local` or find the Pi's IP in your router's DHCP list and
> go to `http://<ip>`. Configure Wi-Fi from the **Network** tab in the
> dashboard.

For alternative Wi-Fi setup methods (SSH, raspi-config, etc.), see the
**[User Guide](USER_GUIDE.md)**.

### Open the dashboard

From any device on the same network, open a browser and go to:

```
http://metixel.local
```

Or use the IP address shown on the frame's display — e.g. `http://192.168.1.50`.

The dashboard lets you:
- Add media folders to watch
- Configure Immich sync
- Change slideshow settings (duration, transitions, fit mode)
- Set a display sleep schedule
- Check for OTA updates

### Add your photos

You have several options — pick whichever fits your setup:

| Method | Best for | How |
|---|---|---|
| **Web upload** | Anyone on the network | Dashboard → Media Library → Upload Media (or drag & drop); opens the phone gallery on mobile |
| **Samba share** | Windows/Mac users | Open `\\metixel\metixel-media` (Windows) or `smb://metixel/metixel-media` (Mac), drag files in |
| **Immich** | Existing Immich users | Enter your Immich server URL + API key in the dashboard → Sync tab |

New files are detected automatically and appear in the slideshow within a
minute or two (larger files take longer due to optimisation).

---

## What's next?

- **[User Guide](USER_GUIDE.md)** — the reference manual for every feature
- **[FAQ](FAQ.md)** — common questions and quick fixes
