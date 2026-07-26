# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Network Manager — Wi-Fi scanning, connection, and AP fallback.

Uses ``nmcli`` (NetworkManager CLI) for all Wi-Fi operations.  NetworkManager
is available on Trixie/Bookworm and handles wpa_supplicant safely behind the
scenes — no raw config-file editing needed.

AP (access point) mode is controlled via systemd units for hostapd and
dnsmasq, which are configured by ``scripts/setup_ap.sh``.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Systemd units for AP mode (created by setup_ap.sh)
# ---------------------------------------------------------------------------

HOSTAPD_UNIT = "hostapd.service"
DNSMASQ_UNIT = "dnsmasq.service"

# How long to wait (seconds) for a connection attempt to succeed/fail
CONNECT_TIMEOUT = 30

# Well-known connectivity check URLs
DEFAULT_CONNECTIVITY_URL = "http://connectivity-check.ubuntu.com"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_wifi_radio_enabled() -> bool:
    """Check whether the Wi-Fi radio is enabled at the OS level.

    Returns False if Wi-Fi has been disabled via rfkill, raspi-config,
    or ``nmcli radio wifi off``.  The wlan0 interface may still exist
    but will show as "unavailable" in device status.
    """
    try:
        result = subprocess.run(
            ["nmcli", "-t", "radio", "wifi"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "enabled"
    except Exception:
        logger.debug("Wi-Fi radio check failed", exc_info=True)
        # If we can't determine, assume enabled (don't block the user)
        return True


def has_saved_wifi_networks() -> bool:
    """Check whether any Wi-Fi networks are saved/configured for auto-connect.

    Returns True if NetworkManager has at least one saved Wi-Fi connection
    (regardless of whether it's currently in range).
    """
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "wifi":
                return True
        return False
    except Exception:
        logger.debug("Saved Wi-Fi check failed", exc_info=True)
        return False


def is_connected() -> bool:
    """Quick check: do we have an IP address on wlan0?

    Returns True if wlan0 has a non-link-local IPv4 address, meaning
    we're connected to a Wi-Fi network (or Ethernet, if that's wired).
    Also returns True if any interface other than lo has a routable IP.
    """
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,STATE", "device", "status"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 2:
                dev, state = parts[0], parts[1]
                if dev != "lo" and state == "connected":
                    return True
        return False
    except Exception:
        logger.debug("is_connected() check failed", exc_info=True)
        return False


def scan_networks() -> list[dict[str, Any]]:
    """Scan for visible Wi-Fi networks.

    Returns a list of dicts with keys:
        ssid, signal (0-100), security (e.g. "WPA2"), freq (MHz)
    """
    try:
        # Trigger a fresh scan first
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan"],
            capture_output=True, timeout=10,
        )
        time.sleep(1.0)  # Wait for scan results

        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list"],
            capture_output=True, text=True, timeout=10,
        )
        networks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split(":")
            if len(parts) < 2:
                continue
            ssid = parts[0].strip()
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            try:
                signal = int(parts[1]) if len(parts) > 1 else 0
            except (ValueError, IndexError):
                signal = 0
            security = parts[2].strip() if len(parts) > 2 else ""
            try:
                freq = int(parts[3]) if len(parts) > 3 else 0
            except (ValueError, IndexError):
                freq = 0
            networks.append({
                "ssid": ssid,
                "signal": signal,
                "security": security,
                "freq": freq,
            })
        # Sort by signal strength (strongest first)
        networks.sort(key=lambda n: n["signal"], reverse=True)
        logger.debug("Wi-Fi scan found %d network(s)", len(networks))
        return networks
    except Exception:
        logger.warning("Wi-Fi scan failed", exc_info=True)
        return []


def connect_to_network(ssid: str, password: str) -> tuple[bool, str]:
    """Connect to a Wi-Fi network.

    Args:
        ssid: The network SSID.
        password: The WPA2 passphrase (empty for open networks).

    Returns:
        (success, message) tuple.
    """
    if not ssid:
        return False, "SSID is required"

    try:
        cmd = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        cmd += ["timeout", str(CONNECT_TIMEOUT)]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CONNECT_TIMEOUT + 10)
        if result.returncode == 0:
            logger.info("Connected to Wi-Fi network: %s", ssid)
            return True, f"Connected to {ssid}"
        else:
            err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            # nmcli often puts useful messages in stdout even on failure
            logger.warning("Failed to connect to %s: %s", ssid, err)
            return False, _friendly_error(err)
    except subprocess.TimeoutExpired:
        logger.warning("Connection attempt to %s timed out", ssid)
        return False, "Connection timed out — please check the password and try again"
    except Exception:
        logger.exception("Connection attempt to %s failed", ssid)
        return False, "Connection failed — please try again"


def forget_network(ssid: str) -> bool:
    """Remove a saved Wi-Fi network from NetworkManager."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,UUID", "connection", "show"],
            capture_output=True, text=True, timeout=5,
        )
        uuid = None
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[0] == ssid:
                uuid = parts[1]
                break
        if uuid:
            subprocess.run(
                ["nmcli", "connection", "delete", uuid],
                capture_output=True, timeout=10,
            )
            logger.info("Forgot Wi-Fi network: %s", ssid)
            return True
        logger.debug("No saved connection found for SSID: %s", ssid)
        return False
    except Exception:
        logger.exception("Failed to forget network %s", ssid)
        return False


def get_connection_status() -> dict[str, Any]:
    """Get current network connection status.

    Checks all active interfaces (Wi-Fi and Ethernet) and returns details
    for the primary connected interface.  Wi-Fi is preferred for reporting
    when both are connected.

    Returns a dict with keys:
        connected (bool), interface_type ("wifi" | "ethernet" | ""),
        interface (str, e.g. "wlan0"), ssid (str, Wi-Fi only),
        signal (int 0-100, Wi-Fi only), ip (str)
    """
    status: dict[str, Any] = {
        "connected": False,
        "interface_type": "",
        "interface": "",
        "ssid": "",
        "ip": "",
        "signal": 0,
        "security": "",
        "wifi_radio_enabled": is_wifi_radio_enabled(),
        "has_saved_wifi": has_saved_wifi_networks(),
    }

    try:
        # ── Discover which interfaces are connected ──────────────────
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"],
            capture_output=True, text=True, timeout=5,
        )
        connected_ifaces: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 3:
                dev, dev_type, state = parts[0], parts[1], parts[2]
                if dev != "lo" and state == "connected":
                    connected_ifaces.append({
                        "device": dev,
                        "type": dev_type,
                    })

        if not connected_ifaces:
            return status

        # Prefer Wi-Fi, fall back to Ethernet
        wifi = next((i for i in connected_ifaces if i["type"] == "wifi"), None)
        eth = next((i for i in connected_ifaces if i["type"] == "ethernet"), None)
        primary = wifi or eth
        if primary is None:
            return status  # Shouldn't happen, but be safe

        status["connected"] = True
        status["interface"] = primary["device"]

        if primary["type"] == "wifi":
            status["interface_type"] = "wifi"
            _fill_wifi_details(status, primary["device"])
        else:
            status["interface_type"] = "ethernet"
            _fill_ethernet_details(status, primary["device"])
    except Exception:
        logger.debug("Connection status check failed", exc_info=True)

    return status


def _fill_wifi_details(status: dict[str, Any], device: str) -> None:
    """Populate Wi-Fi-specific fields (SSID, signal, security) into status."""
    try:
        conn_result = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid,signal,security", "device", "wifi", "list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in conn_result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == "yes":
                status["ssid"] = parts[1].strip()
                try:
                    status["signal"] = int(parts[2])
                except (ValueError, IndexError):
                    pass
                if len(parts) >= 4:
                    status["security"] = parts[3].strip()
                break
    except Exception:
        logger.debug("Wi-Fi detail fetch failed", exc_info=True)

    _fill_ip_address(status, device)


def _fill_ethernet_details(status: dict[str, Any], device: str) -> None:
    """Populate Ethernet-specific fields into status."""
    try:
        # Get the connection name for the Ethernet interface
        conn_result = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", device],
            capture_output=True, text=True, timeout=5,
        )
        for line in conn_result.stdout.strip().splitlines():
            if line.startswith("GENERAL.CONNECTION:"):
                name = line.split(":", 1)[-1].strip()
                if name:
                    status["ssid"] = name  # Reuse ssid field for connection name
                break
    except Exception:
        logger.debug("Ethernet detail fetch failed", exc_info=True)

    _fill_ip_address(status, device)


def _fill_ip_address(status: dict[str, Any], device: str) -> None:
    """Populate the IP address for a given device into status."""
    try:
        ip_result = subprocess.run(
            ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", device],
            capture_output=True, text=True, timeout=5,
        )
        for line in ip_result.stdout.strip().splitlines():
            if line.startswith("IP4.ADDRESS["):
                val = line.split(":", 1)[-1].split("/")[0].strip()
                if val:
                    status["ip"] = val
                    break
    except Exception:
        logger.debug("IP address fetch failed for %s", device, exc_info=True)


def start_ap_mode() -> bool:
    """Start the access point (hostapd + dnsmasq).

    Configures the static IP for wlan0 and starts the AP services.
    Requires hostapd and dnsmasq to be installed and configured
    (see ``scripts/setup_ap.sh``).
    """
    try:
        # Set static IP for the AP
        subprocess.run(
            ["ip", "addr", "add", "192.168.42.1/24", "dev", "wlan0"],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["ip", "link", "set", "wlan0", "up"],
            capture_output=True, timeout=5,
        )
        # Start services
        subprocess.run(
            ["systemctl", "start", HOSTAPD_UNIT, DNSMASQ_UNIT],
            capture_output=True, timeout=10,
        )
        logger.info("AP mode activated: SSID=Metixel-Setup, IP=192.168.42.1")
        return True
    except Exception:
        logger.exception("Failed to start AP mode")
        return False


def stop_ap_mode() -> bool:
    """Stop the access point and restore normal Wi-Fi operation."""
    try:
        subprocess.run(
            ["systemctl", "stop", HOSTAPD_UNIT, DNSMASQ_UNIT],
            capture_output=True, timeout=10,
        )
        # Remove static IP
        subprocess.run(
            ["ip", "addr", "del", "192.168.42.1/24", "dev", "wlan0"],
            capture_output=True, timeout=5,
        )
        logger.info("AP mode deactivated")
        return True
    except Exception:
        logger.exception("Failed to stop AP mode")
        return False


def is_ap_mode_active() -> bool:
    """Check whether the access point is currently active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", HOSTAPD_UNIT],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "active"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _friendly_error(raw: str) -> str:
    """Convert nmcli error messages into user-friendly strings."""
    raw_lower = raw.lower()
    if "secrets were required but not provided" in raw_lower:
        return "Incorrect password — please try again"
    if "no network with ssid" in raw_lower:
        return "Network not found — it may be out of range"
    if "timeout" in raw_lower or "timed out" in raw_lower:
        return "Connection timed out — please check the password and try again"
    if "already connected" in raw_lower:
        return "Already connected to a network"
    # Return first meaningful line of the error
    for line in raw.splitlines():
        line = line.strip()
        if line and "error" in line.lower():
            return line[:200]
    return raw[:200] if raw else "Unknown error"
