# Metixel Photoframe — User Guide

## 1. Welcome to Metixel

Metixel turns your Raspberry Pi into a beautiful digital photo frame. It displays
your photos and videos on a TV or monitor, with smooth crossfade transitions, a
web dashboard for settings, and automatic updates.

**What you'll need:**

- Raspberry Pi 3, 4, or 5 (2 GB+ recommended)
- MicroSD card (8 GB+) with Metixel installed
- HDMI cable and a TV or monitor
- Power supply for your Pi
- A phone or computer to manage the frame over Wi-Fi

---

## 2. First-Time Setup

1. **Connect everything.** Plug the HDMI cable into the Pi and your display.
   Connect the power supply. The Pi will boot automatically — there's no power
   button.

2. **Watch the boot screen.** You'll see the Metixel logo and a spinner while
   the frame processes your media. This can take a few minutes on first boot.

3. **The slideshow begins.** Once ready, the boot screen fades out and your
   photos and videos start playing. If you haven't added any media yet, sample
   images are included.

4. **Find the dashboard.** You'll see a message on screen with the frame's IP
   address (e.g. `http://192.168.1.50`). Open that in a browser on any device
   on the same network. You can also use `http://metixel.local` if your
   network supports mDNS.

---

## 3. Getting Online

### Option A: Ethernet (easiest)

Plug an Ethernet cable into the Pi before powering it on. The frame connects
automatically — no configuration needed.

### Option B: Wi-Fi via Captive Portal

If Ethernet isn't available:

1. Look at the frame's display. After the boot animation, a **4-digit PIN**
   appears on screen.
2. On your phone or laptop, join the Wi-Fi network **Metixel-Setup** (no
   password).
3. Your browser should open a setup page automatically. If not, go to
   `http://192.168.42.1`.
4. Enter the PIN shown on the frame, then select your home Wi-Fi network and
   enter its password.
5. The frame connects to Wi-Fi and shows the IP address on screen.

### Switching Wi-Fi later

Open the dashboard → Settings → Network. You can scan for networks, switch
connections, or forget saved networks from there.

---

## 4. Adding Your Photos & Videos

### Option A: Web Upload (easiest)

Open the dashboard on any device (phone, tablet, or computer) and use the
**Media Library → Upload Media** button (or drag & drop files onto the grid):

- **Phone (iOS/Android):** the upload button opens your photo gallery — pick
  one or many photos/videos and tap done.
- **Computer:** tap Upload Media to pick files, or drag & drop them onto the
  media grid.
- HEIC photos from an iPhone are converted automatically, and the frame
  starts showing new uploads within about 30 seconds.

### Option B: Samba Share

Metixel also creates a shared folder on your network:

- **Windows:** Open File Explorer, type `\\metixel` or `\\<ip-address>` in the
  address bar. Open the `metixel-media` folder. Use `pi` / `raspberry` to log
  in.
- **Mac:** In Finder, press Cmd+K, enter `smb://<ip-address>/metixel-media`,
  and connect as `pi` with password `raspberry`.
- Drag photos and videos into the folder. The frame picks them up within a few
  seconds.

### Option B: USB Drive

Copy your media files onto a USB drive, plug it into the Pi, and use the
dashboard (Media page) to copy them to the frame. **USB auto-import is planned
but not yet available.**

### Option C: Immich Sync

If you use [Immich](https://immich.app) to manage your photo library, Metixel
can automatically download new photos:

1. Open the dashboard → Settings → Immich.
2. Enter your Immich server URL, API key, and an album name.
3. The frame syncs new photos on a schedule you configure.

### Supported File Types

| Type | Formats |
|------|---------|
| Photos | `.jpg`, `.jpeg`, `.png`, `.bmp`, `.gif` |
| Videos | `.mp4`, `.mov`, `.mkv`, `.avi` (transcoded to H.264 for smooth playback) |

---

## 5. The Web Dashboard

Open `http://<ip-address>` in any browser:

| Page | What you can do |
|------|----------------|
| **Media** | Browse your library, delete items, see what's playing |
| **Playback** | Pause/resume, skip to next, see current item |
| **Settings** | Configure everything: timing, video, network, Immich |
| **Advanced** | View logs, check for updates, reboot/shutdown |

**Pausing:** Click the pause button on the Playback page or use the
pause/play icon on the dashboard status card. The frame holds on the current
image until you resume.

**Skipping:** The next button advances to the next item immediately.

---

## 6. Customising Your Frame

All settings are in the dashboard under **Settings**.

### Slideshow Timing

- **Duration per image:** How long each photo stays on screen (default 15s)
- **Transition:** Crossfade speed between images (default 2.5s)
- **Shuffle:** Randomise the order or play sequentially

### Videos

- **Video playback:** Turn on/off entirely
- **Maximum duration:** Limit how long videos play (0 = full length)
- **Transcoding:** Automatically convert videos for smooth playback on your Pi

### Display

- **Fit mode:** `cover` (fill the screen, cropping edges) or `contain` (show
  the whole image with black bars)
- **Sleep schedule:** Set times for the display to turn off (e.g. overnight)
- **Quiet boot:** Hide kernel messages during startup for a cleaner look

---

## 7. Keeping It Updated

Metixel checks for updates automatically and tells you when a new version is
available.

### Update Channels

- **Stable:** Fully tested releases. Best for most users.
- **Beta:** Early access to new features. May have rough edges.

Switch channels in **Advanced → Updates**. Click **Check for Updates** to
see what's available, then **Install Update** to apply it. The frame reboots
automatically after updating.

---

## 8. Troubleshooting

### Frame won't turn on

- Check the power supply — Pi 4/5 need a good USB-C supply (5V 3A).
- Make sure the HDMI cable is connected before powering on (some displays
  need it for the Pi to detect resolution).
- Try a different SD card if the green activity light doesn't flash.

### Can't connect to Wi-Fi

- **"Metixel-Setup" doesn't appear on your phone:** The Pi's Wi-Fi hardware
  may still be initialising. The AP appears within 60–90 seconds of boot. Try
  refreshing your Wi-Fi list.
- **Entered wrong password:** The frame shows a "WiFi Connection Failed"
  message. The captive portal button re-enables — try again.
- **Still can't connect:** Plug in Ethernet, open the dashboard, and
  configure Wi-Fi from Settings → Network.

### Videos don't play

- Check **Settings → Video → Video Playback** is enabled.
- Large or unusual video formats may take time to transcode. Check the
  Background Processing card on the dashboard — if it's still transcoding,
  wait for it to finish.
- Very old Pi models (Pi 2, Pi Zero 2 W) may struggle with high-resolution
  video. Try 1080p or lower.

### Web dashboard not loading

- Make sure you're using the correct IP address (check the frame's display).
- Try `http://metixel.local` instead.
- If the frame shows a PIN and "Metixel-Setup" Wi-Fi, it doesn't have a
  network connection — use the captive portal to connect Wi-Fi first.
- Reboot the Pi and wait a minute for services to start.

### Factory reset

To wipe all settings and start fresh:

```bash
ssh pi@<ip-address>
sudo systemctl stop metixel-backend metixel-cage
sudo rm /opt/metixel/etc/config.json
sudo rm -rf /opt/metixel/cache/*
sudo reboot
```

The frame will create a fresh config and re-process all media on next boot.
