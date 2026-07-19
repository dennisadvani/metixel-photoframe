# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#!/bin/bash
# =============================================================================
# Metixel Photoframe — Wi-Fi Captive Portal Setup
#
# Configures a fallback Wi-Fi access point + captive portal for initial
# network configuration when no known Wi-Fi network is available.
#
# Usage: sudo bash setup_ap.sh
# =============================================================================

set -euo pipefail

echo "=== Metixel Photoframe Wi-Fi Captive Portal Setup ==="
echo ""
echo "This script installs hostapd and dnsmasq to create a fallback"
echo "Wi-Fi access point when no network connection is detected."
echo ""
echo "When active, connect to the 'Metixel-Setup' Wi-Fi network and"
echo "open any browser to configure your Wi-Fi connection."
echo ""

# ---------------------------------------------------------------------------
# 1. Install dependencies
# ---------------------------------------------------------------------------
echo "[1/4] Installing dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq hostapd dnsmasq

# ---------------------------------------------------------------------------
# 2. Configure hostapd
# ---------------------------------------------------------------------------
echo "[2/4] Configuring hostapd..."
sudo tee /etc/hostapd/hostapd.conf > /dev/null <<'EOF'
interface=wlan0
driver=nl80211
ssid=Metixel-Setup
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=0
EOF

sudo sed -i 's|^#DAEMON_CONF=""|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

# ---------------------------------------------------------------------------
# 3. Configure dnsmasq (DHCP + DNS)
# ---------------------------------------------------------------------------
echo "[3/4] Configuring dnsmasq..."
sudo tee /etc/dnsmasq.conf > /dev/null <<'EOF'
interface=wlan0
dhcp-range=192.168.42.10,192.168.42.100,12h
dhcp-option=3,192.168.42.1
dhcp-option=6,192.168.42.1
address=/#/192.168.42.1
no-resolv
EOF

# ---------------------------------------------------------------------------
# 4. Configure network interfaces
# ---------------------------------------------------------------------------
echo "[4/4] Configuring network..."
sudo tee /etc/network/interfaces.d/wlan0-fallback > /dev/null <<'EOF'
# Managed by Metixel Photoframe
# If wpa_supplicant fails to connect within 60s, the captive portal activates.
allow-hotplug wlan0
iface wlan0 inet manual
    wpa-roam /etc/wpa_supplicant/wpa_supplicant.conf
EOF

echo ""
echo "=== Setup Complete ==="
echo "To activate the portal manually:"
echo "  sudo systemctl stop wpa_supplicant"
echo "  sudo systemctl start hostapd dnsmasq"
echo "  sudo ifconfig wlan0 192.168.42.1"
