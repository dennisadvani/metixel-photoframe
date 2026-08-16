# Getting Started with Metixel Photoframe

This guide gets you from unboxing your Raspberry Pi to watching your first
slideshow in **under 10 minutes**.

---

## What you need

| Item | Notes |
|---|---|
| Raspberry Pi 2, 3, 4, or 5 | Pi 5 with 2GB+ RAM is recommended; Pi 4 is supported but untested; Pi 2 requires manual install (no .img) |
| MicroSD card | 8GB minimum, Class 10 or better |
| HDMI display + cable | 1080p recommended |
| 5V power supply | 3A for Pi 5, 2.5A for Pi 2/3/4 |
| Computer with SD card reader | For flashing the image |
| Wi-Fi or Ethernet | For initial setup and the web dashboard |

> **Pi Zero 2 W (512MB)?** Metixel runs on it but is **untested**. Images only —
> no video playback, optimisation, or transcoding. Manual install required
> (no pre-built .img). See [`docs/HARDWARE.md`](HARDWARE.md).

---

## Step 1: Download the pre-built image

Go to the **[latest release](https://github.com/dennisadvani/metixel-photoframe/releases/latest)**
and download `metixel_trixie_vX.X.X.img.zip`.

This is a complete Debian Trixie image with Metixel pre-installed — nothing
else to configure.

---

## Step 2: Flash the SD card

1. Install **[Raspberry Pi Imager](https://www.raspberrypi.com/software/)**
   on your computer.
2. Insert your MicroSD card.
3. Open Pi Imager, click **Choose OS** → **Use custom** → select the
   downloaded `.img.zip` file.
4. Click **Choose Storage** → select your SD card.
5. Click **Write** and wait for the flash + verification to complete.

> **Note:** Pi Imager's advanced options (gear icon) are not available for
> custom images. To configure Wi-Fi before first boot, see the alternative
> methods in the **[User Guide](USER_GUIDE.md)**.

---

## Step 3: Boot the Pi

1. Insert the SD card into your Raspberry Pi.
2. Connect the HDMI cable to your display.
3. Plug in the power supply.

The Pi will boot. You'll see the Metixel logo with a spinning animation,
followed by your first slideshow (sample media is included).

---

## Step 4: Connect to Wi-Fi

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

---

## Step 5: Open the dashboard

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

---

## Step 6: Add your photos

You have several options — pick whichever fits your setup:

| Method | Best for | How |
|---|---|---|
| **Web upload** | Anyone on the network | Dashboard → Media Library → Upload Media (or drag & drop); opens the phone gallery on mobile |
| **Samba share** | Windows/Mac users | Open `\\metixel\metixel-media` (Windows) or `smb://metixel/metixel-media` (Mac), drag files in |
| **Immich** | Existing Immich users | Enter your Immich server URL + API key in the dashboard → Sync tab |
| **Network share** | NAS users | Mount your SMB/NFS share on the Pi, add the mount point as a watch folder |

New files are detected automatically and appear in the slideshow within a
minute or two (larger files take longer due to optimisation).

---

## What's next?

- **[User Guide](USER_GUIDE.md)** — Full setup, every feature, Wi-Fi, Immich & MQTT
- **[FAQ](FAQ.md)** — Common questions and quick fixes
- **[Installation Guide](INSTALLATION.md)** — Manual install for tinkerers (flash Trixie yourself)
- **[Hardware Guide](HARDWARE.md)** — Enclosures, displays, wiring, accessories
