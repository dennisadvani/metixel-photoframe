# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: Wi-Fi scan, connect, and forget on real hardware.

These run ON the Pi with ``METIXEL_NETWORK_TEST_MODE=1`` so the controller
ignores Ethernet for connectivity decisions — the Pi stays reachable over
Ethernet (SSH) while the Wi-Fi radio is exercised.  The test connects to the
network from ``functional/.env``, verifies connectivity, then forgets it so
the Pi returns to its prior state.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from metixel.backend import network_manager as nm

pytestmark = pytest.mark.functional

#: How long to wait for a Wi-Fi connection to come up.
_CONNECT_WAIT = 45


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _wait_for_connected(timeout: int = _CONNECT_WAIT) -> bool:
    """Poll nm.is_connected() until the Wi-Fi link is up or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if nm.is_connected():
            return True
        time.sleep(2)
    return False


def test_wifi_scan_finds_test_network(wifi_creds: dict[str, str]) -> None:
    """The configured test SSID must be visible in a live scan."""
    networks = nm.scan_networks()
    ssids = {n["ssid"] for n in networks}
    assert wifi_creds["ssid"] in ssids, (
        f"test SSID {wifi_creds['ssid']!r} not found in scan; found: {sorted(ssids)}"
    )


def test_wifi_connect_and_verify(wifi_creds: dict[str, str]) -> None:
    """Connect to the test network and confirm real connectivity.

    Runs with METIXEL_NETWORK_TEST_MODE=1 so Ethernet is ignored — a
    successful connect must come from Wi-Fi, not the Ethernet uplink.
    """
    assert os.environ.get("METIXEL_NETWORK_TEST_MODE") == "1", (
        "Wi-Fi functional tests must run with METIXEL_NETWORK_TEST_MODE=1 "
        "so Ethernet is ignored for connectivity."
    )

    ok, message = nm.connect_to_network(wifi_creds["ssid"], wifi_creds["password"])
    assert ok, f"connect_to_network failed: {message}"

    assert _wait_for_connected(), "Wi-Fi did not reach a connected state in time"

    status = nm.get_connection_status()
    assert status["connected"], "get_connection_status reports not connected"
    assert status["interface_type"] == "wifi", (
        f"expected wifi connection, got {status['interface_type']}"
    )


def test_wifi_forget_restores_state(wifi_creds: dict[str, str]) -> None:
    """Forgetting the test network must succeed and drop the connection."""
    assert nm.forget_network(wifi_creds["ssid"]), "forget_network failed"

    # Give NetworkManager a moment to drop the link.
    time.sleep(3)
    status = nm.get_connection_status()
    assert not status["connected"], "still connected after forgetting the network"
