# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: Access Point (AP) mode on real hardware.

These run ON the Pi and start/stop hostapd + dnsmasq.  Starting the AP takes
wlan0 out of client mode, so this file is run in a SEPARATE pytest invocation
from the Wi-Fi tests (see scripts/run_functional_tests.sh) — the AP teardown
can't strand a Wi-Fi client mid-run.  Ethernet control is unaffected.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from metixel.backend import network_manager as nm

pytestmark = pytest.mark.functional

#: How long to wait for hostapd to come up after start_ap_mode().
_AP_WAIT = 30


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _wait_for_ap(timeout: int = _AP_WAIT) -> bool:
    """Poll is_ap_mode_active() until hostapd is up or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if nm.is_ap_mode_active():
            return True
        time.sleep(2)
    return False


def test_ap_start_broadcasts() -> None:
    """Starting AP mode must bring up hostapd and the AP subnet."""
    assert nm.start_ap_mode(), "start_ap_mode() returned False"

    assert _wait_for_ap(), "hostapd did not become active in time"

    # hostapd must be running.
    result = _run(["systemctl", "is-active", nm.HOSTAPD_UNIT])
    assert result.stdout.strip() == "active", "hostapd is not active"

    # wlan0 must be in master (AP) mode.  iw lives in /usr/sbin, which is not
    # on the pi user's PATH — use the full path (read-only query, no sudo).
    result = _run(["/usr/sbin/iw", "dev", "wlan0", "info"])
    assert "type AP" in result.stdout or "type master" in result.stdout, (
        "wlan0 is not in AP/master mode"
    )

    # The AP subnet IP must be present.
    result = _run(["ip", "-4", "addr", "show", "wlan0"])
    assert "192.168.42.1" in result.stdout, "AP static IP 192.168.42.1 not assigned"


def test_ap_stop_cleans_up() -> None:
    """Stopping AP mode must stop hostapd and return wlan0 to managed mode."""
    assert nm.stop_ap_mode(), "stop_ap_mode() returned False"

    result = _run(["systemctl", "is-active", nm.HOSTAPD_UNIT])
    assert result.stdout.strip() != "active", "hostapd still active after stop"

    result = _run(["nmcli", "-t", "device", "status"])
    assert "wlan0" in result.stdout, "wlan0 missing after AP stop"
