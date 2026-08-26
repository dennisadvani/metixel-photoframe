# Metixel Photoframe — User Guide

> Screenshots referenced throughout this guide live in `docs/images/`.

## Table of Contents

- [1. Overview](#1-overview)
- [2. Setup / Installation / Wifi](#2-setup--installation)
- [3. Adding Your Own Photos](#3-adding-your-own-photos)
- [4. Using the Web Dashboard](#4-using-the-web-dashboard)
- [5. Immich Sync (detailed)](#5-immich-sync-detailed)
- [6. MQTT / Home Assistant (detailed)](#6-mqtt--home-assistant-detailed)
- [7. Keyboard & Remote Control](#7-keyboard--remote-control)
- [8. OTA Updates](#8-ota-updates)
- [9. Everyday Use & Care](#9-everyday-use--care)
- [10. Troubleshooting](#10-troubleshooting)

---

## 1. Overview

**Metixel Photoframe** turns a Raspberry Pi into a beautiful digital photo
frame. It sits on your wall or shelf and cycles through your own photos and
videos with smooth crossfade transitions. You manage everything from a web
dashboard on your phone or computer — no coding, no command line needed.

Your photos come from two places:

1. **Files you copy in** (upload from the browser, or drop them onto a shared
   network folder), and/or
2. **Photos synced from an Immich server** (see [section 5](#5-immich-sync-detailed)).

The frame is designed to be left on and forgotten about:

- Photos rotate as a **slideshow** — with optional shuffle and smooth
  transitions.
- The screen **turns off overnight** to save power.
- It **survives reboots and network drops** — if Wi-Fi or the internet goes
  down, it keeps playing your local photos.

### What you'll need

| Item | Notes |
|------|-------|
| **Raspberry Pi** | Pi 5 (2 GB+ recommended), Pi 4, or Pi 3. Pi 2 and Pi Zero 2 W work but are for advanced users only. |
| **MicroSD card** | 8 GB or larger. Bigger card = more photos. |
| **HDMI cable + display** | Any TV or monitor with HDMI. |
| **Power supply** | The official supply for your Pi model (Pi 4/5 need a good 5 V/3 A USB-C supply). |
| **Phone or computer** | To manage the frame over your Wi-Fi. |

---

## 2. Setup / Installation

> **New here?** The quick path is **[Installation & Setup](INSTALLATION.md)**.
> This chapter is the detailed reference — most people only need the quick
> path.

### Step 1 — Flash the Metixel image onto an SD card (recommended)

1. Download the latest **Metixel OS image** from the [releases page](https://github.com/dennisadvani/metixel-photoframe/releases).
2. Use a flashing tool such as [Raspberry Pi Imager](https://www.raspberrypi.com/software/) or [balenaEtcher](https://etcher.balena.io/) to write the image to your SD card.
3. Wait for the flash to finish, then remove the card safely.

> **Advanced — manual install:** If you have an unsupported model (Pi 2, Pi Zero 2 W, or a non-Pi board), or you prefer to install Metixel on top of an existing Raspberry Pi OS, see `docs/INSTALLATION.md`. That path involves the command line and is not required for most users.

### Step 2 — Boot the frame

1. Insert the SD card into the Pi.
2. Connect the **HDMI cable** to the Pi and your display *before* powering on (some displays need this to detect the resolution).
3. Connect the **power supply**. The Pi boots automatically — there's no power button.

![Screenshot: frame booting — Metixel logo and spinner](images/boot-screen.png)

4. Watch the boot screen. The first boot can take a few minutes while the frame processes your media. When it's ready, the slideshow begins.

### Step 3 — Connect to Wi-Fi

Metixel needs a network connection so you can reach the web dashboard. Pick the
method that fits your situation — they all end with the frame on your home Wi-Fi.

| # | Method | Best when | Requires |
|---|--------|-----------|----------|
| 1 | **Captive portal** (default) | No Ethernet, first boot | A phone or laptop with Wi-Fi |
| 2 | **Ethernet + dashboard** | You have an Ethernet cable | An Ethernet cable |
| 3 | **Pre-configure before boot** | You want Wi-Fi ready at first boot | SD card reader + text editor |
| 4 | **SSH + `nmcli`** | Headless, you know the IP | SSH access *(advanced)* |
| 5 | **`raspi-config`** | Keyboard + monitor attached | Keyboard + monitor *(advanced)* |

#### Method 1 — Captive portal (easiest, no cables)

This is the zero-config path. If Metixel boots with no Wi-Fi configured and no
Ethernet plugged in, it creates its own hotspot:

1. Look at the frame's display — a **4-digit PIN** appears after the boot animation.
2. On your phone or laptop, join the Wi-Fi network named **`Metixel-Setup`** (no password).
3. Your browser should open the setup page automatically. If not, go to `http://192.168.42.1`.
4. Enter the **PIN** shown on the frame, then pick your home Wi-Fi network and enter its password.
5. The frame connects, shows its **IP address** on screen, and the `Metixel-Setup` hotspot disappears.

![Screenshot: Wi-Fi setup / captive portal page](images/wifi-setup.png)

> **Can't see `Metixel-Setup`?** The hotspot can take up to ~90 seconds to
> appear after boot. If it still doesn't appear, briefly plug in an Ethernet
> cable then disconnect it — this forces a retry.

#### Method 2 — Ethernet, then set up Wi-Fi from the dashboard

1. Plug an **Ethernet cable** into the Pi.
2. Find the Pi's IP — check your router's DHCP client list (look for `metixel`), try `http://metixel.local`, or use `arp -a`.
3. Open the dashboard at `http://<ip>` and go to the **Network** tab.
4. Click **Scan**, select your Wi-Fi network, enter the password, and **Connect**.
5. You can now unplug the Ethernet cable — the frame stays on Wi-Fi.

#### Method 3 — Pre-configure Wi-Fi before booting *(advanced)*

Edit the SD card's boot partition after flashing the image:

<details>
<summary>Click to expand</summary>

1. Flash the image to the SD card, then **do not eject** it yet. The **boot** partition (FAT32) appears on your computer.
2. On the boot partition, create a file named `wpa_supplicant.conf`:
   ```
   country=US
   ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
   update_config=1

   network={
       ssid="YourNetworkName"
       psk="YourPassword"
   }
   ```
   Replace `US` with your [ISO 3166-1 country code](https://en.wikipedia.org/wiki/ISO_3166-1) and fill in your Wi-Fi details.
3. (Optional) Create an empty file named `ssh` (no extension) on the boot partition to enable SSH on first boot.
4. Eject the SD card and boot the Pi — it connects to Wi-Fi automatically.

> This works with Debian Trixie images (which include the Raspberry Pi OS
> first-boot hooks). If it doesn't on your image, use Method 1 or 2 instead.
</details>

#### Method 4 — SSH + `nmcli` *(advanced)*

If you can reach the Pi over SSH:

```bash
# List nearby networks
nmcli device wifi list

# Connect to a WPA2 network
sudo nmcli device wifi connect "MyNetwork" password "mypassword"

# Verify
nmcli -t -f DEVICE,STATE device status | grep wlan0
```

The connection is saved and persists across reboots.

#### Method 5 — `raspi-config` *(advanced)*

If you have a keyboard and monitor attached to the Pi:

1. Plug in a USB keyboard and open a terminal.
2. Run `sudo raspi-config`.
3. Go to **System Options → Wireless LAN**, enter your country, network, and password.
4. Select **Finish** and reboot.

#### Changing or forgetting a Wi-Fi network

Open the dashboard → **Network** page. You can **Scan** for networks, switch
connections, or **Forget** the current one. If the frame has no network at all,
the `Metixel-Setup` hotspot reappears so you can set up a new connection.

> **Wi-Fi country code:** if your region's Wi-Fi channels don't work, set the
> country code under **Settings → Network → Wi-Fi Country Code**.

### Step 4 — Open the web dashboard

The frame shows its IP address on screen, e.g. `http://192.168.1.50`. Open that
in any browser on the same network. You can also try `http://metixel.local` if
your network supports mDNS.

There are **no login credentials** by default — if you're on your home network,
the dashboard just opens. (This keeps setup simple; the dashboard is only
reachable on your local network.)

![Screenshot: the Metixel web dashboard, dashboard page](images/dashboard-home.png)

---

## 3. Adding Your Own Photos

There are three ways to get photos onto the frame. New files are scanned
automatically — you don't need to restart anything. Within about 30 seconds of
adding files, the frame resizes and thumbnails them and they appear in the
slideshow.

### Option A — Upload from the browser (easiest)

1. Open the dashboard → **Media Library**.
2. Click **Upload Media** (or drag & drop files straight onto the grid).

![Screenshot: Media Library page with Upload Media button](images/media-upload.png)

- **Phone (iOS/Android):** the button opens your photo gallery — pick one or many photos/videos and tap **Done**.
- **Computer:** pick files, or drag & drop them onto the media grid.
- iPhone **HEIC** photos are converted automatically.

### Option B — Network shared folder (Samba)

Metixel shares a folder on your network. Copy files into it and the frame picks
them up.

- **Windows:** in File Explorer, type `\\metixel` (or `\\<ip-address>`) in the address bar, open the `metixel-media` folder, and log in with `pi` / `raspberry`.
- **Mac:** in Finder press **Cmd+K**, enter `smb://<ip-address>/metixel-media`, and connect as `pi` with password `raspberry`.

> **Default login:** SSH and the Samba share both use the account `pi` with the
> default password `raspberry`. Change it once your frame is set up, especially
> if it's reachable beyond your home network.

### Option C — Immich sync

If you use [Immich](https://immich.app) to organise your photo library, Metixel
can download photos automatically. See the full [Immich section](#5-immich-sync-detailed).

### Supported file types

| Type | Formats |
|------|---------|
| **Photos** | `.jpg`, `.jpeg`, `.png`, `.bmp`, `.gif` (`.heic` / `.heif` are converted automatically) |
| **Videos** | `.mp4`, `.mov`, `.mkv`, `.avi` (automatically transcoded to H.264 for smooth playback) |

**For best performance:** photos up to around 4K work well. Very large files
(tens of megabytes) take longer to optimise. Videos are best kept to 1080p for
older Pi models.

---

## 4. Using the Web Dashboard

Open `http://<ip-address>` in any browser. The left-hand menu (or the ☰ menu on
a phone) takes you between pages.

| Page | What you can do |
|------|----------------|
| **Dashboard** | See system status, what's playing, playback controls, background processing, and sync status. |
| **Media Library** | Browse, filter, upload, and delete your photos and videos. |
| **Settings** | Configure slideshow, display, video, image, local folders, and timezone. |
| **Image Sync** | Set up and monitor Immich sync. |
| **Network** | Check connectivity and the current IP, change Wi-Fi. |
| **Advanced** | System info, keyboard/remote mapping, OTA updates, display settings, clock, and MQTT. |

### Dashboard page

The dashboard gives you a live overview:

- **System Status** — uptime, disk/cache/media usage, CPU, memory, and temperature.
- **Now Playing** — the current photo or video.
- **Controls** — pause/resume and next/previous.
- **Background Processing** — progress bars for *scanning folders*, *optimising images*, *scanning video*, and *transcoding video*. When a file can't be processed (e.g. a transcode failed), it appears in an **issues** list below the bars with **Retry** and **Delete** actions.
- **Sync Status** — the last Immich sync result.

![Screenshot: dashboard page showing status, controls, and processing bars](images/dashboard-status.png)

> **Tip:** if a video fails to transcode, use the **Retry** button once you've
> fixed the file, or **Delete** to remove it from the library.

### Media Library page

- **Upload Media** — add photos/videos from your device.
- **Filter** — by filename, folder, or type (Images / Videos).
- **Grid** — A badge shows whether a video is
  *Queued*, *Transcoding*, or ready.

![Screenshot: media library grid with filters and badges](images/media-upload.png)

### Settings page

- **Slideshow** — how long each photo stays (default 15 s), transition style
  and speed, and shuffle on/off.
- **Display** — fit mode (`cover` fills the screen and crops, `contain` shows
  the whole image with bars), and the **sleep schedule** for turning the screen
  off overnight.
- **Video** — video playback on/off, maximum duration (0 = full length), and
  transcoding options.
- **Image** — whether to auto-optimise/resize photos, and size limits.
- **Local Folders** — which folders the frame watches.
- **Timezone & Clock** — set your timezone (used by the clock and sleep schedule).

### Network page

Shows connectivity and the current IP. Use it to scan for and switch Wi-Fi
networks, or forget a saved network.

### Advanced page

- **System** — Pi model, OS, kernel, Python, GPU memory, and hostname.
- **Updates** — check for and install updates (see [section 8](#8-ota-updates)).
- **Display Settings** — sleep schedule and clock.
- **Keyboard / Remote Control** — map a remote (see [section 7](#7-keyboard--remote-control)).
- **MQTT / Home Assistant** — connect the frame to your smart-home hub (see [section 6](#6-mqtt--home-assistant-detailed)).
- **Reboot / Shutdown** — restart or power off the frame cleanly.

---

## 5. Immich Sync (detailed)

### What it does

[Immich](https://immich.app) is a self-hosted photo library. Instead of copying
photos onto the frame by hand, Metixel can **download new photos automatically**
from your Immich server — great if you already organise your photos there.

You pick which **albums** to sync. Each album lands in its own folder on the
frame (`media/sync/immich/album_<id>/`) and is picked up by the slideshow
automatically.

### Before you start

You need:

- An Immich server you can reach from your network (or the internet), and
- An **Immich account** on that server (any account with access to the albums
  you want).

### Step 1 — Generate an Immich API key

The API key is how Metixel proves it's allowed to read your photos.

1. Log in to the **Immich web app** in your browser.
2. Click your **profile picture / avatar** (top-right) to open your account menu.
3. Open **Account Settings** → **API Keys**.
4. Click **New API Key**.
5. Give it a name (e.g. `Metixel Photo Frame`).
6. **Select the required permissions (scopes).** Metixel needs these four to
   list and download your albums and their photos — select **all** of them:
   - `asset.read`
   - `asset.download`
   - `album.read`
   - `album.download`
7. Click **Create**.
8. **Copy the key immediately** — it is shown only once, and you can't see it again later.
9. Store it somewhere safe (a password manager is ideal).

![Screenshot: Immich account settings — API Keys, creating a new key](images/immich-api-key.png)

> If you lose the key, delete it in Immich and generate a new one — then update
> the key in Metixel.

### Step 2 — Find your server URL

The server URL is the address you use to open Immich in your browser, including
the scheme and port. Examples:

- `https://immich.example.com` — if you use a domain name
- `http://192.168.1.50:2283` — if it's on your local network (note the port, commonly `2283`)

### Step 3 — Enter the details in Metixel

1. Open the dashboard → **Image Sync** page.
2. Turn on **Enable Immich sync**.
3. Paste the **Server URL**.
4. Paste the **API key**.
5. Click **Test Connection** — it should confirm the connection works.

![Screenshot: Image Sync page with server URL, API key, and Test Connection](images/immich-settings.png)

### Step 4 — Choose albums to sync

1. Click **Fetch Albums** to load your albums from the server.
2. Pick an album from the list and click **Add Album**.
3. Repeat for any other albums. They appear in the **Synced Albums** list.
4. To stop syncing an album, click **Remove** (this also deletes its downloaded
   folder from the frame).

### Step 5 — Set the schedule

- **Sync interval** — how often the frame checks for new photos (default every
  **1 hour**).
- **Strict sync** (optional) — when on, the local folder mirrors the album
  exactly: photos removed from the album on Immich are also removed from the
  frame. When off (default), Metixel only downloads new photos and never
  deletes anything.

### Step 6 — Sync now & monitor

Use **Sync Now** to trigger an immediate download instead of waiting for the
schedule. The **Sync Status** card (dashboard) shows the last result — how many
photos were downloaded, skipped, or failed.

### Troubleshooting

| Problem | What to check |
|---------|---------------|
| **Server unreachable** | Is the URL correct (scheme + host + port)? Can your phone open it from the same network? |
| **Wrong API key** | Regenerate the key in Immich and update it in Metixel. |
| **Nothing syncs** | Is *Enable Immich sync* on? Did you add at least one album? Check the sync interval and use **Sync Now**. |
| **Server was down** | Metixel retries automatically with backoff and keeps the slideshow running from local photos in the meantime. |

---

## 6. MQTT / Home Assistant (detailed)

### What MQTT is and what it does here

MQTT is a lightweight messaging system that smart-home hubs use to talk to
devices. By connecting Metixel to an **MQTT broker**, you can:

- **Control the frame from Home Assistant** (or any MQTT tool): next, previous,
  pause/resume, and screen power on/off.
- **See what it's doing**: current media, playback state, screen power, and
  system health.

Metixel publishes a handful of status topics and subscribes to a few control
topics, all scoped to a per-frame ID so **multiple frames on one broker never
collide**.

### Prerequisites

- An **MQTT broker** — e.g. [Mosquitto](https://mosquitto.org), the Home
  Assistant add-on, or a hosted broker. *(Optional but recommended: Home
  Assistant, so the frame appears automatically.)*

### Step 1 — Create an MQTT username/password for the frame

It's good practice to give the frame its own login rather than using the broker
admin account.

- **Mosquitto (via Home Assistant add-on):** open the add-on **Configuration**
  tab, find the `mosquitto` section (or an *extra users* area), and add a user
  such as `metixel` with a password.
- **Standalone Mosquitto:** create a password file and user, for example:

  ```bash
  # On the broker machine, as root
  touch /etc/mosquitto/passwd
  mosquitto_passwd -c /etc/mosquitto/passwd metixel
  # (it will prompt for a password, then add to mosquitto.conf:)
  #   allow_anonymous false
  #   password_file /etc/mosquitto/passwd
  ```

> **Advanced:** the exact steps depend on your broker. The key outcome is a
> username + password the frame can log in with, and that the broker allows
> that user to publish/subscribe to topics under `metixel/<device_id>/#`.

### Step 2 — Enter the settings in Metixel

Open the dashboard → **Advanced** → **MQTT / Home Assistant**:

| Setting | What to enter |
|---------|---------------|
| **Enable MQTT** | Turn on. |
| **Broker** | The broker's hostname or IP (e.g. `192.168.1.20`, or `localhost` if running on the frame itself). |
| **Port** | Usually `1883` (default). |
| **Username / Password** | The credentials you created in Step 1. Leave blank if your broker allows anonymous access. |
| **Device ID** | Optional. Leave empty to auto-generate a unique per-frame ID (recommended for multiple frames). |
| **Discovery** | *Home Assistant discovery* — leave on to auto-add the frame to HA. |

Click **Save**. The page should show a **Connected** status.

![Screenshot: Advanced page — MQTT settings form](images/mqtt-settings.png)

### Step 3 — Understand the topics

All topics are prefixed with `metixel/<device_id>/`. The frame:

**Publishes (status):**

| Topic | Payload | Meaning |
|-------|---------|---------|
| `…/status` | `online` / `offline` (retained) | Whether the frame is connected. |
| `…/health` | JSON | System health (CPU, memory, disk, temperature…). |
| `…/current_media` | JSON | Current file, title, media type, paused state. |
| `…/state` | `playing` / `paused` / `off` | Playback state. |
| `…/screen` | `ON` / `OFF` | Screen power state. |

**Subscribes (control — send these to control the frame):**

| Topic | Payload | Effect |
|-------|---------|--------|
| `…/cmd` | `next` | Skip to next item. |
| `…/cmd` | `prev` | Go to previous item. |
| `…/cmd` | `pause` | Pause playback. |
| `…/cmd` | `resume` | Resume playback. |
| `…/cmd` | `toggle_pause` | Pause or resume. |
| `…/cmd` | `power_on` | Turn the screen on. |
| `…/cmd` | `power_off` | Turn the screen off. |
| `…/album/set` | an album ID | Switch to that album. |
| `…/screen/set` | `ON` / `OFF` | Turn the screen on/off. |

Where `…` is `metixel/<device_id>` — e.g. `metixel/abcd1234/cmd`.

### Step 4 — Add Metixel to Home Assistant

If **Discovery** is on (default), Home Assistant auto-discovers the frame and
creates these entities:

| Entity | Type | What it does |
|--------|------|--------------|
| Metixel Next / Previous / Pause/Resume | Buttons | Control playback. |
| Metixel Screen Power | Switch | Turn the screen on/off (shows real state). |
| Metixel Playback State | Sensor | Playing / paused / off. |
| Metixel Current Media | Sensor (diagnostic, disabled by default) | The file currently playing. |

Give Home Assistant a few minutes (or restart it) after enabling MQTT on the
frame. If discovery doesn't pick it up, check the broker is reachable and that
the frame shows **Connected**.

![Screenshot: Metixel in Home Assistant MQTT](images/home-assistant-device.png)

> **Manual setup (advanced):** if you prefer not to use discovery, create MQTT
> entities in Home Assistant pointing at the topics in the table above using the
> payloads listed.

### Troubleshooting

| Problem | What to check |
|---------|---------------|
| **Status shows not connected** | Broker address/port correct? Is the broker running? |
| **Authentication error** | Wrong username/password. Re-check Step 1. |
| **Frame connects but HA shows nothing** | Discovery on? Broker reachable from HA? Try restarting HA. |
| **Multiple frames** | Leave **Device ID** empty so each gets a unique ID — otherwise they share topics. |

---

## 7. Keyboard & Remote Control

You can map buttons on a **USB wireless remote** or a **keyboard** to frame
controls (next, previous, pause, power, etc.).

1. Open the dashboard → **Advanced** → **Keyboard / Remote Control**.
2. Click **Learn**.
3. Press the key on your remote/keyboard that you want to map.
4. Choose the action for that key (e.g. Next, Previous, Pause, Power On/Off).
5. Repeat for each button, then **Save**.

![Screenshot: Advanced page — Keyboard / Remote Control mapping table](images/keyboard-map.png)

The frame responds to your remote immediately. (HDMI-CEC support for TV remotes
is available but needs the optional `python3-libcec` package — see
`docs/INSTALLATION.md`.)

---

## 8. OTA Updates

Metixel checks for updates automatically (default every 6 hours) and shows when
a new version is available.

1. Open the dashboard → **Advanced** → **Updates**.
2. Choose your **Update Channel**:
   - **Stable** — fully tested releases. Best for most users.
   - **Beta** — early access to new features. May have rough edges.
3. Click **Check for Updates** to see what's available.
4. Click **Install Update** to apply it. The frame updates and reboots
   automatically; the slideshow resumes once it's back.

![Screenshot: Advanced page — Updates card](images/updates.png)

You can also toggle **automatically check for updates**.

---

## 9. Everyday Use & Care

- **Pause / resume:** use the pause/play button on the dashboard, the on-screen
  remote, or an MQTT command.
- **Skip:** the **next** button advances immediately.
- **Overnight sleep:** the screen dims/turns off during the **sleep schedule**
  you set under **Settings → Display** (default off-hours are 22:00–07:00).
- **Reboot / shutdown:** use **Advanced → Reboot** or **Shutdown** to turn the
  frame off cleanly (important for the SD card's health). Never just pull the
  power.

---

## 10. Troubleshooting

Quick fixes for common issues. See also `docs/FAQ.md` for more.

| Symptom | Likely fix |
|---------|-----------|
| **Frame won't turn on** | Check the power supply (Pi 4/5 need 5 V/3 A). Ensure HDMI is connected before powering on. |
| **Can't find the IP** | The frame shows it on screen. Or try `http://metixel.local`. |
| **Photos not appearing** | Give it ~30 s after adding. Check the Background Processing bars aren't stuck. |
| **Videos don't play** | Check *Video playback* is on (Settings → Video) and it's finished transcoding. |
| **Screen black during the day** | Check the sleep schedule (Settings → Display) isn't covering now. |
| **Dashboard won't load** | Same network as the frame? Correct IP? The frame shows a PIN setup screen if it's offline. |
| **Forget everything (factory reset)** | See `docs/FAQ.md` — an advanced, command-line step. |

---

## Appendix — Where your files live

- **Photos you upload / drop in:** the watched local folders (see **Settings →
  Local Folders**).
- **Immich downloads:** `/opt/metixel/data/media/sync/immich/album_<id>/` on the frame.
- **Optimised/thumbnail cache:** `/opt/metixel/data/cache/` — safe to clear if
  you ever need space (the frame regenerates it).

For advanced setup, hardware notes, and developer docs, see `docs/INSTALLATION.md`
and `docs/WIDGET_DEV.md`.
