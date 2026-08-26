<div align="center">

<a href="https://github.com/dennisadvani/metixel-photoframe">
  <img src="docs/images/metixel_logo_red_white_background.png" alt="Metixel Photoframe" width="450">
</a>

*An open-source digital photo frame for the Raspberry Pi.*  
*Where your photographs and videos are front and center in your home — synced with Immich, managed from your browser, and hung beautifully on the wall.*

<p align="center">
  <a href="#features"><img src="https://img.shields.io/badge/Features-334155?style=for-the-badge" alt="Features"></a>
  &nbsp;
  <a href="https://github.com/dennisadvani/metixel-photoframe/releases/latest"><img src="https://img.shields.io/badge/Download-8B1A2B?style=for-the-badge" alt="Download"></a>
  &nbsp;
  <a href="docs/INSTALLATION.md"><img src="https://img.shields.io/badge/Install-334155?style=for-the-badge" alt="Install"></a>
  &nbsp;
  <a href="docs/INSTALLATION.md#set-up-your-frame"><img src="https://img.shields.io/badge/Set_up-334155?style=for-the-badge" alt="Set up"></a>
  &nbsp;
  <a href="docs/FAQ.md"><img src="https://img.shields.io/badge/FAQ-334155?style=for-the-badge" alt="FAQ"></a>
</p>

<a href="https://github.com/dennisadvani/metixel-photoframe/actions/workflows/ci.yml"><img alt="CI status" src="https://github.com/dennisadvani/metixel-photoframe/actions/workflows/ci.yml/badge.svg"></a> <img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue">

<img src="docs/images/metixel_github_10fps.gif" alt="Metixel Photoframe in action" width="100%">

**The picture frame, not the dashboard.**<br>
No clocks, no widgets, no weather overlays. Metixel treats your photographs and videos like art, with nothing to distract from the moment.

**The Favorite-to-Frame workflow.**<br>
Stop letting daily memories get forgotten in the cloud. Connect Metixel to an Immich album, and any photo you drop in from your phone automatically hangs on your wall, or upload from your phone.

**Zero-terminal setup.**<br>
DIY hardware shouldn't require a computer science degree. Once installed, Metixel is managed entirely from a web interface — no SSH, no config files, no command-line rituals.

<img src="docs/images/metixel_web_ui.PNG" alt="Metixel Web Dashboard" width="70%">

</div>

---

## Features

- **Image playback** supporting JPEG, PNG, WebP, BMP, and GIF — with EXIF auto-rotation and smooth hardware-accelerated crossfades. iPhone HEIC/HEIF photos are automatically converted to JPEG on upload
- **Video playback** supporting MP4, MOV, M4V, AVI, MKV, WebM, and MPG/MPEG (H.264/AVC, H.265/HEVC, MPEG-4, VP8/VP9, and more via ffmpeg), with automatic transcoding to Pi-friendly H.264 + AAC and HDR → SDR tone-mapping
- **Web UI** on port 80/http — configure everything from a browser or phone, no text file editing required
- **Web media upload** — upload photos and videos from any phone or browser via the Media Library (opens the phone gallery on iOS/Android; drag & drop on desktop)
- **Smooth crossfade transitions** with hardware OpenGL ES rendering
- **Native Immich v3 sync** — auto-pull albums and assets
- **Runs locally** — no cloud dependency, your photos stay on your network
- **Screen Off Hours** — turn off the screen at night to save power
- **Over the Air Updates** — update via the web UI
- **EXIF Auto Rotation** — automatically rotate photos based on EXIF metadata
- **Video Transcoding** — convert videos to a format that older Pi's can play
- **Home Assistant MQTT Integration** — auto-discover the frame in Home Assistant with control buttons, a screen-power switch, and diagnostic sensors (playback state, current media, CPU, memory, disk, temperature); control it from HA or any MQTT client
- **Keyboard and Remote Support** — assign keys to remote controls via the web UI
- **Network Management** — Wi-Fi scanning + connection, AP fallback with captive portal + PIN gate, auto-deactivation, connectivity check

[Detailed Features](docs/FEATURES.md)

---

## Build Your Own Frame

Any Raspberry Pi makes a beautiful wall frame. My own build paired an old
monitor with a custom frame and matte, the Pi mounted discreetly behind. For
step-by-step inspiration, see these guides on [building a 32-inch 4K frame with a Raspberry Pi 4](https://www.thedigitalpictureframe.com/how-i-built-a-stunning-32-inch-4k-digital-picture-frame-with-a-raspberry-pi-4-featuring-smooth-image-crossfading-transitions/), [creating a DIY frame in under twenty minutes](https://medium.com/women-make/diy-digital-photo-frame-in-less-than-20-minutes-69bc35ed6364), and [making a digital photo frame](https://www.noahrousell.com/blog/making-a-digital-pho/).


---

## Get started

Metixel goes from download to your first slideshow in about ten minutes:

| | Step | What you'll do | Go |
|---|---|---|---|
| 1 | **Download** | Grab the pre-built image for your Pi | [Latest release](https://github.com/dennisadvani/metixel-photoframe/releases/latest) |
| 2 | **Install** | See what hardware you need, then flash the image — or install manually | [Installation guide](docs/INSTALLATION.md) |
| 3 | **Set up your frame** | Connect to Wi-Fi, open the dashboard, add your photos | [Set up your frame](docs/INSTALLATION.md#set-up-your-frame) |
| 4 | **Questions?** | Troubleshooting and quick fixes | [FAQ](docs/FAQ.md) |

---

## Reference & contributing

- **[User Guide](docs/USER_GUIDE.md)** — the reference manual for every feature
- [`FEATURES.md`](docs/FEATURES.md) — the complete feature list
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system design & implementation roadmap
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — contribution guide: dev setup, testing, code style
- [`docs/API.md`](docs/API.md) — REST API and IPC reference
- [`THIRD_PARTY_LICENSES.md`](docs/THIRD_PARTY_LICENSES.md) — third-party dependency licenses


---

## License

[Apache 2.0](LICENSE) © 2024–2026 Metixel Photoframe Contributors

See [`NOTICE`](docs/NOTICE) for attributions and [`THIRD_PARTY_LICENSES.md`](docs/THIRD_PARTY_LICENSES.md) for third-party dependency licenses.
