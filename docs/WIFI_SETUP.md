# WiFi Setup Guide

Metixel needs a network connection to reach the web dashboard. There are
several ways to get it online — pick the one that fits your situation.

---

## Method comparison

| # | Method | When to use | Requires |
|---|---|---|---|
| 1 | **Captive Portal AP** | No Ethernet, first boot | Phone or laptop with Wi-Fi |
| 2 | **Ethernet + Dashboard** | Ethernet cable handy | Ethernet cable |
| 3 | **Pre-configure before booting** | Want Wi-Fi ready at first boot | SD card reader, text editor |
| 4 | **SSH + nmcli** | Headless, know the IP | SSH access |
| 5 | **raspi-config** | Keyboard + monitor attached | Keyboard, monitor |

All methods produce the same result — a Pi connected to your home Wi-Fi.

> **Note:** The setup script asks for your WiFi country code during
> installation. If you need to change it later, use the Web UI
> (Settings → Network → WiFi Country Code) or run `sudo iw reg set XX`.

---

## Method 1: Captive Portal AP (default)

This is the **zero-config path**. If Metixel boots and finds no Wi-Fi
configured and no Ethernet plugged in, it automatically creates a hotspot.

1. **Look at the frame's display.** After the boot animation, a message
   appears with a 4-digit PIN.
2. On your phone or laptop, **join the Wi-Fi network `Metixel-Setup`**
   (no password required).
3. Open a browser and go to **http://192.168.42.1**.
4. Enter the **4-digit PIN** from the frame's display.
5. Select your home Wi-Fi network from the list and enter the password.
6. The frame connects, and the `Metixel-Setup` hotspot disappears.

> **Can't see `Metixel-Setup`?** The AP can take up to ~90 seconds to appear
> after boot (the frame waits for the slideshow to start first). If it still
> doesn't appear, try plugging in Ethernet briefly then disconnecting — this
> forces a retry. See [Troubleshooting](TROUBLESHOOTING.md) for more.

---

## Method 2: Ethernet + Dashboard

If you have an Ethernet cable, this is the fastest path.

1. **Plug an Ethernet cable** into the Pi.
2. Find the Pi's IP address:
   - Check your router's DHCP client list (look for `metixel`)
   - Try **http://metixel.local** (works on most networks)
   - Use `arp -a` in a terminal and look for a Raspberry Pi MAC address
3. Open **http://<ip>** in a browser.
4. Go to the **Network** tab.
5. Click **Scan** to see available Wi-Fi networks.
6. Select your network, enter the password, and click **Connect**.
7. **Unplug the Ethernet cable.** The Pi stays connected via Wi-Fi.

---

## Method 3: Pre-configure before booting

If you want Wi-Fi configured before the Pi boots, you can edit the boot
partition after flashing the image.

1. Flash the `.img` file to your SD card (Pi Imager or `dd`).
2. **Do not eject** the SD card yet. Mount the **boot** partition (it's
   FAT32 and shows up on Windows/Mac/Linux automatically).
3. On the boot partition, create a file called `wpa_supplicant.conf`:
   ```
   country=AU
   ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
   update_config=1

   network={
       ssid="YourNetworkName"
       psk="YourPassword"
   }
   ```
   Replace `AU` with your [ISO 3166-1 country code](https://en.wikipedia.org/wiki/ISO_3166-1)
   and fill in your Wi-Fi details.
4. (Optional) Create an empty file called `ssh` (no extension) on the boot
   partition to enable SSH on first boot.
5. Eject the SD card and boot the Pi.

When the Pi boots, Raspberry Pi OS copies `wpa_supplicant.conf` to the system
partition and connects to your Wi-Fi automatically. No captive portal needed.

> This method works with Debian Trixie images (which include Raspberry Pi OS
> first-boot hooks). If it doesn't work on your image, use Method 1 or 2
> instead.

---

## Method 4: SSH + nmcli

Use this if you can reach the Pi via SSH (Ethernet, or SSH was enabled in
Pi Imager).

```bash
# Scan for available networks
nmcli device wifi list

# Connect to a WPA2 network
sudo nmcli device wifi connect "MyNetwork" password "mypassword"

# Verify connection
nmcli -t -f DEVICE,STATE device status | grep wlan0
```

The connection is saved and persists across reboots.

---

## Method 5: raspi-config

Use this if you have a keyboard and monitor connected to the Pi.

1. Plug in a USB keyboard.
2. At the terminal, run:
   ```bash
   sudo raspi-config
   ```
3. Navigate to **System Options** → **Wireless LAN**.
4. Enter your country, SSID, and password.
5. Select **Finish** and reboot.

---

## After connecting

Once the Pi is online, the PIN message on the display is replaced with a
confirmation showing the Pi's IP address.

Open **http://metixel.local** (or the IP shown) in any browser on the
same network to access the dashboard.

---

## Changing or forgetting a Wi-Fi network

1. Open the dashboard at `http://<ip>`.
2. Go to the **Network** tab.
3. Click **Forget** next to the current network.
4. The Pi disconnects. If no other network is available, the captive portal
   AP appears so you can configure a new one.
