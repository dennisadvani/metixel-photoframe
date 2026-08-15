# Installation Guide

Two paths to a running Metixel frame. Both work on Raspberry Pi 2, 3, 4, and 5.

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

Follow the **[Getting Started guide](GETTING_STARTED.md)** from Step 4.

---

## Path C: Development setup (VS Code sync to Pi)

Metixel development requires a Pi — the display backend (pi3d + Mesa)
doesn't run on a desktop. The workflow is to edit code on your workstation
and sync it to the Pi over SSH using the bundled VS Code tasks
(`.vscode/tasks.json` + `.vscode/sync-to-pi.ps1`).

### 1. Set up a Pi

First, install Metixel on a Pi using **Path A** or **Path B** above.

### 2. One-time SSH setup on your workstation

The sync workflow uses passwordless SSH from your VS Code PC to the Pi.
Do this once per PC:

1. **Generate an SSH key pair** (skip if you already have one):

   ```powershell
   ssh-keygen -t ed25519
   ```

2. **Install your public key on the Pi:**

   ```powershell
   type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh pi@<pi-ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
   ```

   Replace `<pi-ip>` with your Pi's IP address. Enter the Pi's password
   when prompted (this is the only time it's needed).

3. **Add the Pi to your known hosts** and verify the connection:

   ```powershell
   ssh pi@<pi-ip> "echo ok"
   ```

   Answer `yes` when asked to trust the host key. The sync script also
   passes `StrictHostKeyChecking=accept-new`, so first-run connections
   work even without this step — but verifying once here confirms your
   key is installed correctly.

4. **Verify passwordless sudo** works on the Pi:

   ```powershell
   ssh pi@<pi-ip> "sudo -n true && echo sudo-ok"
   ```

   The sync script uses `sudo rsync` to install files into the
   root-owned tree under `/opt/metixel`. The default Raspberry Pi OS
   `pi` user has passwordless sudo, so this normally just works. If it
   prints nothing, enable it with `sudo visudo` and add:
   `pi ALL=(ALL) NOPASSWD: ALL`.

### 3. Point the tasks at your Pi

Edit `.vscode/tasks.json` and update the IP addresses in the `piHost`
input (search for `"id": "piHost"`) to match your Pi(s). The tasks
prompt you to pick a target Pi every time they run.

### 4. Sync and run

Use the VS Code task runner (**Terminal → Run Task…** or
`Ctrl+Shift+P` → "Tasks: Run Task"):

| Task | What it does |
|---|---|
| **[Pi] Sync Code (scp)** | Mirrors `src/metixel/` to `/opt/metixel/src/metixel/` on the Pi (robocopy → scp to staging → sudo rsync; excludes caches) |
| **[Pi] Sync + Restart All (scp)** | Syncs, then restarts both systemd services |
| **[Pi] Sync + Restart Backend (scp)** | Syncs, then restarts only the backend |
| **[Pi] Run Tests** | Runs `pytest` on the Pi |
| **[Pi] Lint** / **[Pi] Type Check** | Run quality checks on the Pi |
| **[Pi] Follow Logs** | Tails both services' journal |
| **[Pi] Restart All / Backend / Frontend** | Quick service restarts without syncing |

> **Note:** the sync task only mirrors `src/metixel/`. Test files under
> `tests/` are not synced — copy them manually when you change tests:
>
> ```powershell
> scp -r tests/ pi@<pi-ip>:/opt/metixel/tests/
> ```

The sync script (`.vscode/sync-to-pi.ps1`) mirrors the package to a temp
folder with `robocopy` (excluding `__pycache__` and caches), then copies
it to the Pi with `scp`, matching the `src/` layout used by the systemd
units (`PYTHONPATH=/opt/metixel/src`).

---

## After installation

- **[User Guide](USER_GUIDE.md)** — Full setup, Wi-Fi, Immich & MQTT
- **[Configuration Guide](CONFIGURATION.md)** — Every setting explained
- **[Dashboard Walkthrough](DASHBOARD.md)** — Tour of the web UI
- **[Adding Media](MEDIA_SOURCES.md)** — Local files, network shares, Immich
