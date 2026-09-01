# Metixel Photoframe — Frequently Asked Questions

## Contents

- [Photos & videos](#photos--videos)
  - [My photos aren't showing up — what do I check?](#my-photos-arent-showing-up--what-do-i-check)
  - [Why is there a delay before a new photo appears?](#why-is-there-a-delay-before-a-new-photo-appears)
  - [My videos are blurry / won't play](#my-videos-are-blurry--wont-play)
  - [Can I use Google Photos / iCloud / OneDrive?](#can-i-use-google-photos--icloud--onedrive)
  - [How many photos/videos can it hold?](#how-many-photosvideos-can-it-hold)
  - [It's a Raspberry Pi — what if the SD card is full?](#its-a-raspberry-pi--what-if-the-sd-card-is-full)
- [Display & power](#display--power)
  - [The screen is black / dimmed during the day](#the-screen-is-black--dimmed-during-the-day)
  - [The screen is staying on all night](#the-screen-is-staying-on-all-night)
  - [How do I reboot or shut down cleanly?](#how-do-i-reboot-or-shut-down-cleanly)
- [Network & access](#network--access)
  - [The frame isn't connecting to my Wi-Fi / I changed my Wi-Fi and it can't join](#the-frame-isnt-connecting-to-my-wi-fi--i-changed-my-wi-fi-and-it-cant-join)
  - [How do I find the frame's IP address again?](#how-do-i-find-the-frames-ip-address-again)
  - [The web dashboard isn't loading](#the-web-dashboard-isnt-loading)
  - [I forgot the dashboard password](#i-forgot-the-dashboard-password)
- [Immich sync](#immich-sync)
  - [Immich sync isn't pulling my photos](#immich-sync-isnt-pulling-my-photos)
  - [I lost my Immich API key](#i-lost-my-immich-api-key)
  - [What permissions does the Immich API key need?](#what-permissions-does-the-immich-api-key-need)
  - [How often does Immich sync?](#how-often-does-immich-sync)
- [MQTT & Home Assistant](#mqtt--home-assistant)
  - [MQTT isn't talking to Home Assistant](#mqtt-isnt-talking-to-home-assistant)
  - [I have more than one frame — will they clash on MQTT?](#i-have-more-than-one-frame--will-they-clash-on-mqtt)
  - [Which topics does Metixel use?](#which-topics-does-metixel-use)
- [General](#general)
  - [What are the default SSH / Samba logins?](#what-are-the-default-ssh--samba-logins)
  - [How do I update the software?](#how-do-i-update-the-software)
  - [Is my data private?](#is-my-data-private)
  - [How do I factory-reset / start fresh?](#how-do-i-factory-reset--start-fresh)
  - [What Pi models are supported?](#what-pi-models-are-supported)
  - [Where can I get more help?](#where-can-i-get-more-help)

---

## Photos & videos

### My photos aren't showing up — what do I check?

The most common cause is simply that the frame hasn't finished processing them
yet — new files are scanned every ~30 seconds, then optimised and thumbnailed in
the background. Wait about 30 seconds to a minute. If they still don't appear,
open the **Dashboard** and look at the **Background Processing** bars: a file
that failed will appear in the issues list below them (use **Retry** or
**Delete**). Also confirm the folder the files are in is actually enabled under
**Settings → Local Folders**.

### Why is there a delay before a new photo appears?

When you add a photo, the frame doesn't play the original file straight away. It
first scans it, then optimises it (resizes if needed) and builds a thumbnail in
the background. Large files and videos take longer. You'll see this progress on
the Dashboard's **Background Processing** card.

### My videos are blurry / won't play

Check that **Video playback** is enabled under **Settings → Video**. Large or
unusual video formats are transcoded to H.264 in the background for smooth
playback — if the Dashboard still shows it *Transcoding*, wait for it to finish.
On older Pi models (Pi 2, Pi Zero 2 W) high-resolution video may struggle; try
1080p or lower. If a video fails to transcode, the Dashboard lists it under
**issues** with a **Retry** button.

### Can I use Google Photos / iCloud / OneDrive?

Not directly. Metixel supports two paths for getting photos onto the frame:
(1) copy the files in — upload from the **Media Library** page, or drop them
onto the shared network folder; or (2) sync automatically from an **Immich**
server (see the user guide, section 5). If your photos live in Google
Photos/iCloud/OneDrive, export or download them and use one of those two paths.

### How many photos/videos can it hold?

That depends on your SD card size and your media. A typical photo is a few
megabytes, so an 8 GB card holds thousands of photos. Videos are much larger —
a 1 GB card won't hold many long videos. If the card fills up, see "What if the
SD card is full?" below.

### It's a Raspberry Pi — what if the SD card is full?

Free up space by deleting media from the **Media Library** page (which also
cleans up its cached copies), or clear the image cache from **Advanced →
System**. For a thorough clean, see the factory-reset question below.

---

## Display & power

### The screen is black / dimmed during the day

Metixel turns the screen off overnight to save power. Check
your **sleep schedule** under **Settings → Display** — if the current time falls
inside the scheduled "off" window, that's expected. Either adjust the schedule
or turn off the schedule entirely. If the screen is on but shows nothing, check
the HDMI connection and that the frame finished booting.

### The screen is staying on all night

Your sleep schedule may be disabled or set to cover the wrong hours. Open
**Settings → Display** and confirm *schedule enabled* is on and the on/off times
match when you want it awake/asleep (default is 07:00–22:00).

### How do I reboot or shut down cleanly?

Use **Advanced → Reboot** or **Advanced → Shutdown** in the dashboard. This
turns the frame off properly, which keeps the SD card healthy. Avoid pulling the
power.

---

## Network & access

### The frame isn't connecting to my Wi-Fi / I changed my Wi-Fi and it can't join

If the frame shows the **Metixel-Setup** network and a PIN, it has no Wi-Fi
connection — use the setup portal again to connect it (see the user guide,
section 2). If it can't join after you changed your router password, easiest is
to plug in Ethernet, open the dashboard, and update the Wi-Fi from **Network →
Scan**. If the **Metixel-Setup** network doesn't appear on your phone, the Pi's
Wi-Fi may still be initialising — it appears within 60–90 seconds of boot.

### How do I find the frame's IP address again?

The frame shows its IP address on the display. You can also try
`http://metixel.local` in a browser (works if your network supports mDNS). On
many routers you can look at the "connected devices" list and find the Metixel
Pi.

### The web dashboard isn't loading

Make sure your phone/computer is on the **same network** as the frame and you're
using the correct IP (check the frame's display). Try `http://metixel.local`.
If the frame is showing the PIN setup screen, it's offline — connect it to Wi-Fi
first. Rebooting the frame and waiting a minute for services to start can also
help.

### I forgot the dashboard password

There isn't one. The dashboard has **no login credentials** by default and is
only reachable on your local network. If a future update adds authentication,
you'll be prompted to set a password during setup.

---

## Immich sync

### Immich sync isn't pulling my photos

Check, in order: **Image Sync** has *Enable Immich sync* switched on; you've
added at least one **album**; the **server URL** is correct (scheme + host +
port, e.g. `http://192.168.1.50:2283`); the **API key** is valid. Use **Test
Connection** and **Sync Now** to verify. If the server was briefly unreachable,
Metixel retries automatically with backoff — the slideshow keeps playing local
photos meanwhile.

### I lost my Immich API key

API keys are shown only once when created. In Immich, open **Account Settings →
API Keys**, delete the old key, create a new one, and paste it into the frame's
**Image Sync** page.

### What permissions does the Immich API key need?

When you create the API key in Immich, give it at least these four scopes so
Metixel can list and download your albums and photos: `asset.read`,
`asset.download`, `album.read`, and `album.download`.

### How often does Immich sync?

By default every **1 hour**. You can change the interval on the **Image Sync**
page, or use **Sync Now** at any time for an immediate check.

---

## MQTT & Home Assistant

### MQTT isn't talking to Home Assistant

Check the frame's **Advanced → MQTT** page shows **Connected** (if it shows an
authentication error, re-check the username/password). Confirm the broker is
running and reachable from both the frame and Home Assistant. If discovery is on
but HA shows nothing, try restarting Home Assistant and wait a few minutes.

### I have more than one frame — will they clash on MQTT?

No. Leave the **Device ID** empty and Metixel generates a unique per-frame ID,
so each frame uses its own topic namespace (`metixel/<id>/…`) and appears as a
separate device in Home Assistant.

### Which topics does Metixel use?

All topics are under `metixel/<device_id>/`. The frame publishes `status`,
`health`, `current_media`, `state`, and `screen`, and subscribes to `cmd`,
`album/set`, and `screen/set`. Full details and payloads are in the user guide,
section 6.

---

## General

### What are the default SSH / Samba logins?

Both SSH and the Samba (network share) use the same default account:

| Access | Username | Password |
|--------|----------|----------|
| SSH | `pi` | `raspberry` |
| Samba (`metixel-media`) | `pi` | `raspberry` |

> **Security tip:** change these from the defaults once your frame is set up,
> especially if it can be reached beyond your home network.

### What is the "device password"?

SSH and the Samba share share a single **device password**. Changing it from
**Settings → Security → Device Password** updates **both** the console password
(`chpasswd`) and the Samba password (`smbpasswd`) together, so the two stores
never drift apart. It is a **separate** credential from the web dashboard
password and the screen PIN.

If you forget it, recover via the physical console (`sudo passwd pi`) or SSH.

### Can I password-protect the web dashboard?

Yes. Set a **Web Dashboard Password** from **Settings → Security**. Once set,
the dashboard and its API require a login (a session cookie, valid for the
configured idle timeout — default 30 minutes, or "forever"). The login screen
appears automatically on the next visit.

If you forget the web password, clear it by editing `web.password` in
`config.json` and restarting the backend, or run
`python -m metixel --clear-web-password --config <path>`.

> **Note:** the web dashboard password is separate from the device password
> (SSH + Samba) and the screen PIN. There is no TLS on the LAN by default —
> the password is the access boundary.

### How do I update the software?

Open **Advanced → Updates**, click **Check for Updates**, then **Install
Update**. The frame updates and reboots automatically. Choose **Stable** for
well-tested releases, or **Beta** for early access.

### Is my data private?

Yes — Metixel is **local-first**. Your photos live on the frame's SD card and
your own network. Nothing is uploaded to a Metixel cloud (there isn't one). The
only optional outbound connections are the ones you configure: Immich (to fetch
your photos) and GitHub (to check for software updates). MQTT traffic goes only
to your own broker.

### How do I factory-reset / start fresh?

> **Advanced** — this uses the command line.

```bash
# From a computer on the same network, replace <ip-address>:
ssh pi@<ip-address>
sudo systemctl stop metixel-backend metixel-cage
sudo rm /opt/metixel/data/config.json
sudo rm -rf /opt/metixel/data/cache/*
sudo reboot
```

The frame creates a fresh config and re-processes all media on next boot. To
also remove your photos, delete the media folders too.

### What Pi models are supported?

Pi 5 (recommended), Pi 4, and Pi 3 are fully supported. Pi 2 and Pi Zero 2 W
work but are for advanced users (32-bit manual install, with limits on video
performance). See `docs/INSTALLATION.md` for details.

### Where can I get more help?

The main **user guide** is `docs/USER_GUIDE.md`. Installation and hardware
details are in `docs/INSTALLATION.md`, and the project's `ARCHITECTURE.md`.
