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
# Scan cache — populated before AP activation to avoid disconnecting clients
# ---------------------------------------------------------------------------

_cached_scan: list[dict[str, Any]] = []
_cached_scan_time: float = 0.0

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
    """Quick check: do we have a real network connection?

    Returns True if any non-loopback interface is connected AND has an IP
    that is NOT on the AP subnet (192.168.42.x).  The AP's own static IP
    is not a real upstream connection.

    On exception (e.g. nmcli timeout under heavy system load), returns
    ``True`` — assume connected rather than falsely activating AP fallback.
    The monitor re-checks every 10 s and will self-correct when nmcli
    becomes responsive again.
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
                    # Exclude the AP's own IP — 192.168.42.x is the
                    # captive portal subnet, not a real upstream link.
                    if _interface_has_real_ip(dev):
                        return True
        return False
    except Exception:
        logger.debug("is_connected() check failed — assuming connected", exc_info=True)
        return True


def _interface_has_real_ip(device: str) -> bool:
    """Check whether *device* has an IP outside the AP captive-portal subnet."""
    try:
        ip_result = subprocess.run(
            ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", device],
            capture_output=True, text=True, timeout=5,
        )
        for line in ip_result.stdout.strip().splitlines():
            if line.startswith("IP4.ADDRESS["):
                val = line.split(":", 1)[-1].split("/")[0].strip()
                if val and not val.startswith("192.168.42."):
                    return True
        return False
    except Exception:
        return True  # If we can't check, assume it's real (don't block)


def pre_scan_for_ap() -> None:
    """Scan for networks BEFORE activating AP mode.

    Call this while wlan0 is still in managed mode (before hostapd
    takes over).  Results are cached for 5 minutes and served to the
    captive portal without dropping connected clients.
    """
    global _cached_scan, _cached_scan_time
    try:
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan"],
            capture_output=True, timeout=10,
        )
        time.sleep(10.0)
        networks = _parse_scan_results()
        if networks:
            _cached_scan = networks
            _cached_scan_time = time.monotonic()
            logger.info("Pre-scan cached %d network(s) for captive portal", len(networks))
    except Exception:
        logger.warning("Pre-scan for AP failed", exc_info=True)


def scan_networks() -> list[dict[str, Any]]:
    """Scan for visible Wi-Fi networks.

    When the AP is active, returns cached pre-scan data (the Pi's WiFi
    chip can't scan while in AP mode).  When the AP is not active,
    performs a live scan.

    Returns a list of dicts with keys:
        ssid, signal (0-100), security (e.g. "WPA2"), freq (MHz)
    """
    global _cached_scan, _cached_scan_time

    # Serve cached results when AP is active (avoid disconnecting clients).
    # NEVER do a live scan while the AP is broadcasting — it tears down
    # hostapd, drops connected clients, and kills the captive portal.
    # Pre-scan data (captured before AP activation) is served indefinitely.
    if is_ap_mode_active():
        if _cached_scan:
            logger.debug("Serving %d cached scan result(s)", len(_cached_scan))
        return list(_cached_scan) if _cached_scan else []

    # AP is NOT active — safe to do a live scan
    try:
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan"],
            capture_output=True, timeout=10,
        )
        time.sleep(10.0)
        networks = _parse_scan_results()
        # Update cache
        if networks:
            _cached_scan = networks
            _cached_scan_time = time.monotonic()
        return networks
    except Exception:
        logger.warning("Wi-Fi scan failed", exc_info=True)
        return list(_cached_scan) if _cached_scan else []


def _parse_scan_results() -> list[dict[str, Any]]:
    """Parse nmcli wifi list output into a list of network dicts."""
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
    networks.sort(key=lambda n: n["signal"], reverse=True)
    return networks


def connect_to_network(ssid: str, password: str) -> tuple[bool, str]:
    """Connect to a Wi-Fi network.

    If the AP is active, stops it and returns wlan0 to NetworkManager
    control before attempting the connection.

    Args:
        ssid: The network SSID.
        password: The WPA2 passphrase (empty for open networks).

    Returns:
        (success, message) tuple.
    """
    if not ssid:
        return False, "SSID is required"

    # If AP is running, stop it so wlan0 can be used for client connection
    ap_was_active = is_ap_mode_active()
    if ap_was_active:
        stop_ap_mode()
        # After leaving AP mode, wlan0 needs a scan to discover networks.
        # Without this, nmcli fails with "No network with SSID 'X' found."
        time.sleep(2.0)
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan"],
            capture_output=True, timeout=15,
        )
        time.sleep(3.0)  # Wait for scan results to populate

    try:
        cmd = ["sudo", "nmcli", "-w", str(CONNECT_TIMEOUT), "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CONNECT_TIMEOUT + 10)
        if result.returncode == 0:
            logger.info("Connected to Wi-Fi network: %s", ssid)
            return True, f"Connected to {ssid}"

        err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
        logger.warning("Failed to connect to %s: %s", ssid, err)

        # Fallback: if one-shot connect failed with "key-mgmt", try creating
        # a connection profile explicitly (more reliable for mixed-mode routers)
        if password and "key-mgmt" in err.lower():
            logger.info("Retrying with explicit connection profile for %s", ssid)
            success, msg = _connect_with_profile(ssid, password)
            if success:
                return True, msg

        # Restart the AP if the connection failed — otherwise the
        # controller detects the missing AP as a crash, marks
        # AP_EXHAUSTED permanently, and the user is locked out.
        if ap_was_active:
            logger.info("Restarting AP after failed connection attempt")
            start_ap_mode()
        return False, _friendly_error(err)
    except subprocess.TimeoutExpired:
        logger.warning("Connection attempt to %s timed out", ssid)
        if ap_was_active:
            start_ap_mode()
        return False, "Connection timed out — please check the password and try again"
    except Exception:
        logger.exception("Connection attempt to %s failed", ssid)
        if ap_was_active:
            start_ap_mode()
        return False, "Connection failed — please try again"


def forget_network(ssid: str) -> bool:
    """Remove a saved Wi-Fi network from NetworkManager and disconnect."""
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
                ["sudo", "nmcli", "connection", "delete", uuid],
                capture_output=True, timeout=10,
            )
            # Also disconnect wlan0 to trigger AP fallback
            subprocess.run(
                ["sudo", "nmcli", "device", "disconnect", "wlan0"],
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

    Releases wlan0 from NetworkManager control, starts hostapd to create
    the AP, then starts dnsmasq for DHCP/DNS.  The services must be
    installed and configured (see ``scripts/setup_ap.sh``).

    Returns False if the services are not installed or fail to start.
    """
    try:
        # ── Wait for wlan0 to appear ─────────────────────────────────
        # On a cold boot the Wi-Fi driver may still be initialising when
        # the network monitor fires.  Poll for the interface so we don't
        # fail silently and get locked out by the retry guard.
        wlan_ready = False
        for _ in range(30):  # up to 30 seconds
            try:
                result = subprocess.run(
                    ["ip", "link", "show", "wlan0"],
                    capture_output=True, text=True, timeout=5,
                )
                if "wlan0:" in result.stdout:
                    wlan_ready = True
                    break
            except Exception:
                pass
            time.sleep(1.0)
        if not wlan_ready:
            logger.error("wlan0 interface not found — AP mode unavailable")
            return False

        # Verify services are installed before trying to start them
        for unit in (HOSTAPD_UNIT, DNSMASQ_UNIT):
            check = subprocess.run(
                ["systemctl", "list-unit-files", unit],
                capture_output=True, text=True, timeout=5,
            )
            # systemctl list-unit-files outputs "unit.service enabled" or
            # "unit.service masked" etc.  If the unit isn't found it prints
            # nothing for that line.
            if unit not in check.stdout:
                logger.error(
                    "%s not found — AP mode unavailable. "
                    "Run: sudo bash /opt/metixel/scripts/setup_ap.sh",
                    unit,
                )
                return False

        # Release wlan0 from NetworkManager so hostapd can take control.
        # Without this, NM keeps the interface in managed mode and
        # hostapd's AP-ENABLED has no effect (beacons aren't sent).
        subprocess.run(
            ["sudo", "nmcli", "device", "set", "wlan0", "managed", "no"],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["sudo", "ip", "link", "set", "wlan0", "down"],
            capture_output=True, timeout=5,
        )

        # Disable kernel-level WiFi power management BEFORE starting
        # hostapd.  The brcmfmac driver has its own power saving that
        # suppresses beacons when enabled — the AP shows AP-ENABLED
        # but NO-CARRIER and is invisible to phones.  Must happen
        # before hostapd starts so it initialises with beacons on.
        subprocess.run(
            ["sudo", "iw", "dev", "wlan0", "set", "power_save", "off"],
            capture_output=True, timeout=5,
        )

        # Start hostapd first — it creates the AP (sets interface to AP mode)
        subprocess.run(
            ["sudo", "systemctl", "start", HOSTAPD_UNIT],
            capture_output=True, timeout=10,
        )
        time.sleep(0.5)

        # Verify hostapd started
        result = subprocess.run(
            ["systemctl", "is-active", HOSTAPD_UNIT],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip() != "active":
            logger.error("hostapd failed to start — AP mode unavailable")
            subprocess.run(
                ["sudo", "systemctl", "stop", HOSTAPD_UNIT],
                capture_output=True, timeout=5,
            )
            return False

        # Bring up wlan0 with the AP static IP
        subprocess.run(
            ["sudo", "ip", "addr", "add", "192.168.42.1/24", "dev", "wlan0"],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["sudo", "ip", "link", "set", "wlan0", "up"],
            capture_output=True, timeout=5,
        )

        # Start dnsmasq after wlan0 is up (avoids "interface does not exist")
        subprocess.run(
            ["sudo", "systemctl", "start", DNSMASQ_UNIT],
            capture_output=True, timeout=10,
        )
        time.sleep(0.5)

        result = subprocess.run(
            ["systemctl", "is-active", DNSMASQ_UNIT],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip() != "active":
            logger.error("dnsmasq failed to start — AP mode unavailable")
            subprocess.run(
                ["sudo", "systemctl", "stop", HOSTAPD_UNIT, DNSMASQ_UNIT],
                capture_output=True, timeout=5,
            )
            return False

        logger.info("AP mode activated: SSID=Metixel-Setup, IP=192.168.42.1")
        return True
    except Exception:
        logger.exception("Failed to start AP mode")
        return False


def stop_ap_mode() -> bool:
    """Stop the access point and restore normal Wi-Fi operation."""
    try:
        subprocess.run(
            ["sudo", "systemctl", "stop", HOSTAPD_UNIT, DNSMASQ_UNIT],
            capture_output=True, timeout=10,
        )
        # Remove static IP
        subprocess.run(
            ["sudo", "ip", "addr", "del", "192.168.42.1/24", "dev", "wlan0"],
            capture_output=True, timeout=5,
        )
        # Return wlan0 to NetworkManager control
        subprocess.run(
            ["sudo", "nmcli", "device", "set", "wlan0", "managed", "yes"],
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


def _connect_with_profile(ssid: str, password: str) -> tuple[bool, str]:
    """Connect by creating an explicit connection profile.

    Some routers (mixed WPA2/WPA3, certain TP-Link/ASUS models) don't
    advertise key-mgmt in their beacon, causing the one-shot
    ``nmcli device wifi connect`` to fail with "key-mgmt property is
    missing".  Creating a profile with explicit WPA2-PSK settings
    avoids this.
    """
    con_name = f"Metixel-{ssid}"
    try:
        # Remove any stale profile from a previous attempt
        subprocess.run(
            ["sudo", "nmcli", "connection", "delete", con_name],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

    try:
        subprocess.run(
            [
                "sudo", "nmcli", "connection", "add",
                "type", "wifi",
                "con-name", con_name,
                "ifname", "wlan0",
                "ssid", ssid,
                "wifi-sec.key-mgmt", "wpa-psk",
                "wifi-sec.psk", password,
            ],
            capture_output=True, text=True, timeout=15,
        )
        result = subprocess.run(
            ["sudo", "nmcli", "connection", "up", con_name],
            capture_output=True, text=True, timeout=CONNECT_TIMEOUT + 10,
        )
        if result.returncode == 0:
            logger.info("Connected to %s via explicit profile", ssid)
            return True, f"Connected to {ssid}"
        else:
            err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            logger.warning("Profile connection to %s failed: %s", ssid, err)
            # Clean up the failed profile
            subprocess.run(
                ["sudo", "nmcli", "connection", "delete", con_name],
                capture_output=True, timeout=10,
            )
            return False, _friendly_error(err)
    except Exception:
        logger.exception("Profile connection to %s failed", ssid)
        return False, "Connection failed — please try again"


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
    if "key-mgmt" in raw_lower:
        return (
            "Network security type not recognised — the router may use "
            "an unsupported configuration such as WPA3-only"
        )
    # Return first meaningful line of the error
    for line in raw.splitlines():
        line = line.strip()
        if line and "error" in line.lower():
            return line[:200]
    return raw[:200] if raw else "Unknown error"
